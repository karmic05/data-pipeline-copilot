"""dbt model parser.

Accepts a dbt model file (Jinja-templated SQL) optionally bundled with a
``schema.yml`` block - either separated by a ``--- schema.yml`` marker line or
appended as a trailing YAML document starting with ``version: 2`` / a top-level
``models:`` key.

Pipeline:

1. Split the input into the SQL body and the schema.yml block.
2. Lightweight Jinja rendering (regex/scanner is used for Jinja extraction
   only, never for SQL): ``{{ ref("x") }}`` becomes the identifier ``x``,
   ``{{ source("a", "b") }}`` becomes ``a.b``, ``{{ config(...) }}`` is removed
   with its kwargs captured, ``{% ... %}`` tags are dropped (bodies kept), and
   any other ``{{ ... }}`` expression becomes the placeholder identifier
   ``jinja_expr``. Newline counts are preserved so line numbers stay accurate.
3. The rendered SQL is parsed with :func:`app.parsers.sql_parser.parse_sql`
   and the result is overlaid with dbt-specific facts (materialization,
   ref/source dependency edges, schema.yml metadata).

``extras`` keys produced (consumed by ``app.rules.dbt_rules``): ``schema_yml``,
``refs``, ``sources``, ``config``, ``tests``, ``raw_table_refs``,
``unique_key``, ``is_incremental_guard``.
"""
from __future__ import annotations

import ast as python_ast
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.schemas.ir import IR, Dependency, ParseError, ParseResult, TableRef

logger = logging.getLogger(__name__)

# --- Jinja / schema.yml detection patterns (Jinja extraction only) -----------
_SCHEMA_SEP_RE = re.compile(
    r"^\s*-{2,}\s*schema\.ya?ml\s*-*\s*$", re.IGNORECASE | re.MULTILINE
)
_YAML_DOC_LINE_RE = re.compile(r"^(version\s*:\s*['\"]?2['\"]?\s*|models\s*:\s*)$")
_JINJA_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_JINJA_STMT_RE = re.compile(r"\{%.*?%\}", re.DOTALL)
_IS_INCREMENTAL_RE = re.compile(r"\bis_incremental\s*\(\s*\)")
_REF_RE = re.compile(
    r"""^ref\s*\(\s*['"](?P<first>[^'"]+)['"]\s*(?:,\s*['"](?P<second>[^'"]+)['"]\s*)?\)$"""
)
_SOURCE_RE = re.compile(
    r"""^source\s*\(\s*['"](?P<src>[^'"]+)['"]\s*,\s*['"](?P<tbl>[^'"]+)['"]\s*\)$"""
)
_CONFIG_RE = re.compile(r"^config\s*\((?P<args>.*)\)$", re.DOTALL)
_SIMPLE_KWARG_RE = re.compile(r"""(\w+)\s*=\s*['"]([^'"]*)['"]""")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_MATERIALIZATION_TYPES = {"table", "view", "incremental", "ephemeral"}
_PLACEHOLDER = "jinja_expr"


@dataclass
class _RenderResult:
    """Outcome of the lightweight Jinja rendering pass."""

    sql: str = ""
    refs: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    config: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)


# --- schema.yml splitting -----------------------------------------------------


def _split_schema_yaml(source: str) -> Tuple[str, Optional[str]]:
    """Split raw input into ``(sql_text, yaml_text_or_None)``.

    Honors an explicit ``--- schema.yml`` separator line first, then falls back
    to detecting a trailing YAML document that starts at column 0 with
    ``version: 2`` or ``models:`` and actually parses as a YAML mapping.
    """
    match = _SCHEMA_SEP_RE.search(source)
    if match:
        return source[: match.start()], source[match.end() :]

    candidates: List[int] = []
    offset = 0
    for line in source.splitlines(keepends=True):
        if _YAML_DOC_LINE_RE.match(line.rstrip("\r\n")):
            candidates.append(offset)
        offset += len(line)

    for pos in candidates:
        tail = source[pos:]
        try:
            loaded = yaml.safe_load(tail)
        except yaml.YAMLError:
            continue
        if isinstance(loaded, dict) and ("models" in loaded or "version" in loaded):
            return source[:pos], tail
    return source, None


def _load_schema_yml(yaml_text: Optional[str]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Parse the schema.yml block, returning ``(dict_or_None, warnings)``."""
    if yaml_text is None or not yaml_text.strip():
        return None, []
    try:
        loaded = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        logger.warning("dbt schema.yml block failed to parse: %s", exc)
        return None, [f"schema.yml block is not valid YAML and was ignored: {exc}"]
    if isinstance(loaded, dict):
        return loaded, []
    return None, ["schema.yml block did not parse to a mapping and was ignored."]


def _first_model(schema_yml: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the first named model entry from schema.yml, if any."""
    if not schema_yml:
        return None
    models = schema_yml.get("models")
    if not isinstance(models, list):
        return None
    for entry in models:
        if isinstance(entry, dict) and entry.get("name"):
            return entry
    return None


# --- Jinja rendering ------------------------------------------------------------


def _find_jinja_expressions(text: str) -> List[Tuple[int, int, str]]:
    """Locate every ``{{ ... }}`` span, honoring quotes and nested braces.

    Returns ``(start, end, inner)`` tuples where ``inner`` excludes the
    delimiters. A character scanner is required (rather than a non-greedy
    regex) because config kwargs may contain nested dicts whose closing
    braces produce a literal ``}}``.
    """
    spans: List[Tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n - 1:
        if text[i] != "{" or text[i + 1] != "{":
            i += 1
            continue
        start = i
        j = i + 2
        depth = 0
        quote: Optional[str] = None
        end: Optional[int] = None
        while j < n:
            ch = text[j]
            if quote is not None:
                if ch == "\\":
                    j += 2
                    continue
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                elif j + 1 < n and text[j + 1] == "}":
                    end = j + 2
                    break
            j += 1
        if end is None:
            break  # unterminated expression; leave the remainder untouched
        spans.append((start, end, text[start + 2 : end - 2]))
        i = end
    return spans


def _pad(replacement: str, original: str) -> str:
    """Append the original span's newlines so line numbers are preserved."""
    return replacement + "\n" * original.count("\n")


def _safe_identifier(name: str) -> str:
    """Quote an identifier part when it is not a plain SQL identifier."""
    if _IDENTIFIER_RE.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _literal_value(node: python_ast.AST) -> Any:
    """Best-effort conversion of a config kwarg AST node to a Python value."""
    if isinstance(node, python_ast.Constant):
        return node.value
    if isinstance(node, (python_ast.List, python_ast.Tuple)):
        return [_literal_value(elt) for elt in node.elts]
    if isinstance(node, python_ast.Dict):
        return {
            _literal_value(key): _literal_value(value)
            for key, value in zip(node.keys, node.values)
            if key is not None
        }
    if isinstance(node, python_ast.Name):
        jinja_names = {
            "true": True,
            "false": False,
            "none": None,
            "True": True,
            "False": False,
            "None": None,
        }
        if node.id in jinja_names:
            return jinja_names[node.id]
        return node.id
    try:
        return python_ast.unparse(node)
    except Exception:  # pragma: no cover - unparse failure on exotic nodes
        return None


def _parse_config_kwargs(arg_src: str) -> Tuple[Dict[str, Any], List[str]]:
    """Parse the inside of ``config(...)`` into a kwargs dict.

    Jinja config kwargs are almost always valid Python keyword arguments, so we
    parse them with :mod:`ast`; on a syntax error we fall back to recovering
    simple ``key='value'`` pairs.
    """
    arg_src = arg_src.strip()
    if not arg_src:
        return {}, []
    try:
        tree = python_ast.parse(f"__cfg__({arg_src})", mode="eval")
        call = tree.body
        if isinstance(call, python_ast.Call):
            kwargs: Dict[str, Any] = {}
            for keyword in call.keywords:
                if keyword.arg is None:
                    continue
                kwargs[keyword.arg] = _literal_value(keyword.value)
            return kwargs, []
    except SyntaxError:
        pass
    recovered = {m.group(1): m.group(2) for m in _SIMPLE_KWARG_RE.finditer(arg_src)}
    if recovered:
        return recovered, [
            "Could not fully parse dbt config() kwargs; recovered simple "
            "key/value pairs only."
        ]
    return {}, ["Could not parse dbt config() kwargs; config was ignored."]


def _render_jinja(sql_text: str) -> _RenderResult:
    """Render dbt Jinja into plain SQL, capturing refs/sources/config.

    Newline counts of every removed or replaced span are preserved so that
    locations reported against the rendered SQL still match the original input.
    """
    result = _RenderResult()

    text = _JINJA_COMMENT_RE.sub(lambda m: _pad("", m.group(0)), sql_text)
    text = _JINJA_STMT_RE.sub(lambda m: _pad("", m.group(0)), text)

    parts: List[str] = []
    last = 0
    for start, end, inner in _find_jinja_expressions(text):
        parts.append(text[last:start])
        raw = text[start:end]
        expr = inner.strip()

        ref_match = _REF_RE.match(expr)
        source_match = _SOURCE_RE.match(expr)
        config_match = _CONFIG_RE.match(expr)
        if ref_match:
            name = ref_match.group("second") or ref_match.group("first")
            if name not in result.refs:
                result.refs.append(name)
            parts.append(_pad(_safe_identifier(name), raw))
        elif source_match:
            src, tbl = source_match.group("src"), source_match.group("tbl")
            qualified = f"{src}.{tbl}"
            if qualified not in result.sources:
                result.sources.append(qualified)
            rendered = f"{_safe_identifier(src)}.{_safe_identifier(tbl)}"
            parts.append(_pad(rendered, raw))
        elif config_match:
            kwargs, warnings = _parse_config_kwargs(config_match.group("args"))
            result.config.update(kwargs)
            result.warnings.extend(warnings)
            parts.append(_pad("", raw))
        else:
            parts.append(_pad(_PLACEHOLDER, raw))
        last = end
    parts.append(text[last:])
    result.sql = "".join(parts)

    if "{{" in result.sql:
        result.warnings.append(
            "Unterminated Jinja expression found; rendering may be incomplete."
        )
    return result


# --- schema.yml test flattening ------------------------------------------------


def _entry_tests(entry: Dict[str, Any]) -> List[Any]:
    """Combine the legacy ``tests`` and modern ``data_tests`` keys."""
    tests: List[Any] = []
    for key in ("tests", "data_tests"):
        value = entry.get(key)
        if isinstance(value, list):
            tests.extend(value)
    return tests


def _test_name(test: Any) -> str:
    """Normalize a schema.yml test entry to its name string."""
    if isinstance(test, str):
        return test
    if isinstance(test, dict) and test:
        return str(next(iter(test)))
    return str(test)


def _flatten_tests(schema_yml: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flatten schema.yml tests to ``{model, column, test}`` records.

    ``column`` is ``None`` for model-level tests.
    """
    flattened: List[Dict[str, Any]] = []
    if not schema_yml:
        return flattened
    models = schema_yml.get("models")
    if not isinstance(models, list):
        return flattened
    for model in models:
        if not isinstance(model, dict):
            continue
        model_name = str(model.get("name") or "model")
        for test in _entry_tests(model):
            flattened.append(
                {"model": model_name, "column": None, "test": _test_name(test)}
            )
        columns = model.get("columns")
        if not isinstance(columns, list):
            continue
        for column in columns:
            if not isinstance(column, dict):
                continue
            column_name = column.get("name")
            for test in _entry_tests(column):
                flattened.append(
                    {
                        "model": model_name,
                        "column": column_name,
                        "test": _test_name(test),
                    }
                )
    return flattened


# --- overlays --------------------------------------------------------------------


def _normalize_str_list(value: Any) -> List[str]:
    """Normalize partition_by / cluster_by config values to a string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        # BigQuery-style partition_by: {"field": "ds", "data_type": "date", ...}
        fieldname = value.get("field")
        return [str(fieldname)] if fieldname else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def _collect_tags(*values: Any) -> List[str]:
    """Merge tag values (strings or lists) into a deduplicated string list."""
    tags: List[str] = []
    for value in values:
        if value is None:
            continue
        items = [value] if isinstance(value, str) else (
            list(value) if isinstance(value, (list, tuple)) else []
        )
        for item in items:
            text = str(item).strip()
            if text and text not in tags:
                tags.append(text)
    return tags


def _overlay_materialization(ir: IR, config: Dict[str, Any], warnings: List[str]) -> None:
    """Apply config() materialization kwargs onto the IR."""
    materialized = config.get("materialized")
    if isinstance(materialized, str):
        mat = materialized.strip().lower()
        if mat in _MATERIALIZATION_TYPES:
            ir.materialization.type = mat  # type: ignore[assignment]
        elif mat == "materialized_view":
            ir.materialization.type = "view"
        elif mat:
            warnings.append(f"Unknown dbt materialization '{materialized}'.")
    strategy = config.get("incremental_strategy")
    if isinstance(strategy, str) and strategy.strip():
        ir.materialization.strategy = strategy.strip()
    partition_by = _normalize_str_list(config.get("partition_by"))
    if partition_by:
        ir.materialization.partition_by = partition_by
    cluster_by = _normalize_str_list(config.get("cluster_by"))
    if cluster_by:
        ir.materialization.cluster_by = cluster_by


def _raw_table_refs(
    ir: IR, refs: List[str], sources: List[str]
) -> List[str]:
    """Hardcoded ``schema.table`` reads that bypass ``ref()`` / ``source()``."""
    refs_set = set(refs)
    sources_set = set(sources)
    raw: List[str] = []
    for table in ir.tables:
        if table.access_type in ("write", "readwrite"):
            continue
        qualified = ".".join(
            part for part in (table.database, table.schema_name, table.name) if part
        )
        if table.name in refs_set or qualified in refs_set:
            continue
        if qualified in sources_set or table.name in sources_set:
            continue
        if table.name == _PLACEHOLDER or qualified == _PLACEHOLDER:
            continue
        if not (table.database or table.schema_name or "." in table.name):
            continue
        if qualified and qualified not in raw:
            raw.append(qualified)
    return raw


# --- public entry point ------------------------------------------------------------


def parse_dbt(source: str, dialect: Optional[str]) -> ParseResult:
    """Parse a dbt model (Jinja SQL plus optional schema.yml) into a ParseResult.

    Raises:
        ParseError: when the input is empty, or contains neither a parseable
            SQL body nor a parseable schema.yml block.
    """
    if not source or not source.strip():
        raise ParseError("Empty dbt input - provide a model SQL and/or schema.yml.")

    sql_text, yaml_text = _split_schema_yaml(source)
    schema_yml, schema_warnings = _load_schema_yml(yaml_text)
    render = _render_jinja(sql_text)
    is_incremental_guard = bool(_IS_INCREMENTAL_RE.search(sql_text))

    model = _first_model(schema_yml)
    model_name = str((model or {}).get("name") or "model")

    if render.sql.strip():
        from app.parsers.sql_parser import parse_sql

        try:
            base = parse_sql(render.sql, dialect)
        except ParseError as exc:
            raise ParseError(
                f"Failed to parse rendered dbt SQL: {exc.message}", line=exc.line
            ) from exc
    elif schema_yml is not None:
        base = ParseResult(
            ir=IR(format="dbt", dialect=dialect), source=source, ast=schema_yml
        )
        base.warnings.append("No SQL model body found; analyzed schema.yml only.")
        logger.info("dbt input contained schema.yml only (no SQL body)")
    else:
        raise ParseError(
            "dbt input contains no SQL model body and no parseable schema.yml."
        )

    ir = base.ir
    ir.format = "dbt"
    if dialect:
        ir.dialect = dialect

    _overlay_materialization(ir, render.config, render.warnings)

    # raw refs must be computed before the model's own write TableRef is added
    raw_refs = _raw_table_refs(ir, render.refs, render.sources)

    for upstream in [*render.refs, *render.sources]:
        ir.dependencies.append(
            Dependency(source=upstream, target=model_name, type="references")
        )
    if not any(t.access_type in ("write", "readwrite") for t in ir.tables):
        ir.tables.append(TableRef(name=model_name, access_type="write"))

    ir.metadata.name = model_name
    if model:
        description = model.get("description")
        if isinstance(description, str) and description.strip():
            ir.metadata.description = description.strip()
    model_config = (model or {}).get("config")
    ir.metadata.tags = _collect_tags(
        render.config.get("tags"),
        (model or {}).get("tags"),
        model_config.get("tags") if isinstance(model_config, dict) else None,
    )

    base.source = source
    base.extras.update(
        {
            "schema_yml": schema_yml,
            "refs": render.refs,
            "sources": render.sources,
            "config": render.config,
            "tests": _flatten_tests(schema_yml),
            "raw_table_refs": raw_refs,
            "unique_key": render.config.get("unique_key"),
            "is_incremental_guard": is_incremental_guard,
        }
    )
    base.warnings.extend(schema_warnings)
    base.warnings.extend(render.warnings)
    logger.debug(
        "Parsed dbt model '%s': %d refs, %d sources, %d tests, incremental_guard=%s",
        model_name,
        len(render.refs),
        len(render.sources),
        len(base.extras["tests"]),
        is_incremental_guard,
    )
    return base
