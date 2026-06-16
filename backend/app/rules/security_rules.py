"""Security rules.

Deterministic security checks over the parsed IR and raw source: hardcoded
secrets (always redacted in messages), plaintext government/payment identifiers,
PII propagation through SELECT * and column lineage, sensitive join keys,
public cloud paths and over-broad SQL grants. PII detection is delegated to
``app.engines.security`` (same owner) so the rule engine and the security
report stay consistent.
"""
from __future__ import annotations

import logging
import re

from app.engines.security import (
    SECRET_KIND_LABELS,
    classify_pii_name,
    detect_pii_columns,
    find_hardcoded_secrets,
    has_masking_marker,
)
from app.rules import ALL_FORMATS, Rule, register
from app.schemas.ir import ColumnLineage, Operation, ParseResult
from app.schemas.report import Issue, PIIColumn

logger = logging.getLogger(__name__)

_GOVERNMENT_ID_TYPES = frozenset({"ssn", "passport", "tax_id"})
_PAYMENT_TYPES = frozenset({"credit_card", "cvv", "iban"})

_SELECT_STAR_RE = re.compile(r"\bselect\s+(?:distinct\s+|all\s+)?\*", re.IGNORECASE)
_GRANT_ALL_RE = re.compile(r"\bgrant\s+all\b", re.IGNORECASE)
_GRANT_TO_PUBLIC_RE = re.compile(
    r"\bgrant\b[^;]{0,300}?\bto\s+(?:group\s+|role\s+)?public\b",
    re.IGNORECASE | re.DOTALL,
)
_CLOUD_URL_RE = re.compile(
    r"\b(?:s3[an]?|gs|wasbs?|abfss?)://(?P<bucket>[A-Za-z0-9._\-]+)(?P<path>[^\s\"'),;]*)",
    re.IGNORECASE,
)
_PUBLIC_PATH_TOKENS = ("public", "opendata", "open-data", "world", "anonymous", "everyone")
_PUBLIC_ACL_RE = re.compile(
    r"public-read(?:-write)?|\ballUsers\b|allAuthenticatedUsers"
    r"|acl\s*[:=]\s*[\"']?public",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _op_line(op: Operation) -> int | None:
    """1-based line of an operation, or None when the parser had no location."""
    line = op.location.line if op.location else 0
    return line if line and line > 0 else None


def _pii_index(pii: list[PIIColumn]) -> dict[tuple[str, str], PIIColumn]:
    """Index PII columns by (table, column), tolerating qualified table names."""
    index: dict[tuple[str, str], PIIColumn] = {}
    for p in pii:
        table = p.table.lower()
        column = p.column.lower()
        index.setdefault((table, column), p)
        index.setdefault((table.split(".")[-1], column), p)
    return index


def _lookup(
    index: dict[tuple[str, str], PIIColumn], table: str | None, column: str | None
) -> PIIColumn | None:
    """Find a PII column by table+column, falling back to the last table segment."""
    t = (table or "").lower()
    c = (column or "").lower()
    if not c:
        return None
    return index.get((t, c)) or index.get((t.split(".")[-1], c))


def _pii_by_table(pii: list[PIIColumn]) -> dict[str, list[PIIColumn]]:
    """Group PII columns by lowercased table name (full and last segment)."""
    grouped: dict[str, list[PIIColumn]] = {}
    for p in pii:
        table = p.table.lower()
        grouped.setdefault(table, []).append(p)
        segment = table.split(".")[-1]
        if segment != table:
            grouped.setdefault(segment, []).append(p)
    return grouped


def _table_pii(grouped: dict[str, list[PIIColumn]], name: str | None) -> list[PIIColumn]:
    """PII columns of a table, matching the full name then its last segment."""
    n = (name or "").lower()
    if not n:
        return []
    return grouped.get(n) or grouped.get(n.split(".")[-1]) or []


def _find_line(pr: ParseResult, needle: str) -> int | None:
    """First 1-based source line containing ``needle`` as a whole word."""
    if not needle:
        return None
    pattern = re.compile(rf"\b{re.escape(needle)}\b", re.IGNORECASE)
    for line_no, text in enumerate(pr.lines, start=1):
        if pattern.search(text):
            return line_no
    return None


def _pattern_line(source: str, match_start: int) -> int:
    """1-based line number of a regex match offset within ``source``."""
    return source.count("\n", 0, match_start) + 1


def _flows_from(pr: ParseResult, table: str, column: str) -> list[ColumnLineage]:
    """All lineage edges whose source matches (table, column)."""
    t = table.lower()
    segment = t.split(".")[-1]
    c = column.lower()
    flows: list[ColumnLineage] = []
    for cl in pr.ir.column_lineage:
        if (cl.source_column or "").lower() != c:
            continue
        source_table = (cl.source_table or "").lower()
        if source_table in (t, segment) or source_table.split(".")[-1] == segment:
            flows.append(cl)
    return flows


def _flow_is_protected(cl: ColumnLineage) -> bool:
    """True when a lineage edge shows the value is masked/hashed in transit."""
    return has_masking_marker(cl.expression or "") or has_masking_marker(cl.output_column)


# ---------------------------------------------------------------------------
# CRITICAL
# ---------------------------------------------------------------------------


@register
class HardcodedCredentialsRule(Rule):
    """Secrets committed in pipeline code leak through every copy of the repo."""

    id = "HARDCODED_CREDENTIALS"
    severity = "CRITICAL"
    category = "security"
    formats = ALL_FORMATS
    title = "Hardcoded credentials"
    description = (
        "Passwords, API keys, tokens or connection strings with embedded secrets "
        "are committed in the pipeline source instead of a secrets manager."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Report each hardcoded secret with its line; values are never echoed."""
        issues: list[Issue] = []
        for secret in find_hardcoded_secrets(pr.source):
            label = SECRET_KIND_LABELS.get(secret.kind, secret.kind)
            issues.append(
                self.issue(
                    f"Hardcoded {label} ('{secret.identifier}') on line "
                    f"{secret.line}; the secret value has been redacted from this "
                    "report. Anyone with read access to this code holds the credential.",
                    line=secret.line,
                    fix_suggestion=(
                        "Move the secret to an environment variable or a secrets "
                        "manager (Vault, AWS Secrets Manager, Airflow Connections) "
                        "and rotate the exposed credential immediately."
                    ),
                )
            )
        return issues


@register
class SsnPlaintextRule(Rule):
    """Government identifiers selected raw are a severe compliance exposure."""

    id = "SSN_PLAINTEXT"
    severity = "CRITICAL"
    category = "security"
    formats = ALL_FORMATS
    title = "Government ID in plaintext"
    description = (
        "A high-confidence government identifier column (SSN, passport, tax ID) is "
        "selected without any hashing, tokenization or masking."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag government-ID PII columns with no masking evidence in any flow."""
        issues: list[Issue] = []
        for p in detect_pii_columns(pr):
            if p.pii_type not in _GOVERNMENT_ID_TYPES or p.confidence < 0.9:
                continue
            flows = _flows_from(pr, p.table, p.column)
            if flows and all(_flow_is_protected(cl) for cl in flows):
                continue
            issues.append(
                self.issue(
                    f"Government identifier column '{p.table}.{p.column}' "
                    f"({p.pii_type}, confidence {p.confidence:.1f}) is used in "
                    "plaintext - no hashing, tokenization or masking detected.",
                    line=_find_line(pr, p.column),
                    fix_suggestion=p.recommendation,
                )
            )
        return issues


@register
class CreditCardPlaintextRule(Rule):
    """Raw payment data in a pipeline is a direct PCI DSS violation."""

    id = "CREDIT_CARD_PLAINTEXT"
    severity = "CRITICAL"
    category = "security"
    formats = ALL_FORMATS
    title = "Payment data in plaintext"
    description = (
        "A payment-data column (card number, CVV, IBAN) is selected without "
        "tokenization or masking, violating PCI DSS handling requirements."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag payment PII columns with no masking evidence in any flow."""
        issues: list[Issue] = []
        for p in detect_pii_columns(pr):
            if p.pii_type not in _PAYMENT_TYPES or p.confidence < 0.9:
                continue
            flows = _flows_from(pr, p.table, p.column)
            if flows and all(_flow_is_protected(cl) for cl in flows):
                continue
            issues.append(
                self.issue(
                    f"Payment data column '{p.table}.{p.column}' ({p.pii_type}, "
                    f"confidence {p.confidence:.1f}) is processed in plaintext - "
                    "no tokenization or masking detected.",
                    line=_find_line(pr, p.column),
                    fix_suggestion=p.recommendation,
                )
            )
        return issues


# ---------------------------------------------------------------------------
# WARNING
# ---------------------------------------------------------------------------


@register
class PiiInSelectStarRule(Rule):
    """SELECT * silently drags every PII column into the result."""

    id = "PII_IN_SELECT_STAR"
    severity = "WARNING"
    category = "security"
    formats = ALL_FORMATS
    title = "PII swept up by SELECT *"
    description = (
        "SELECT * reads a table containing detected PII columns, propagating "
        "sensitive data downstream implicitly - including columns added later."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag star-projections over tables that contain detected PII."""
        pii = detect_pii_columns(pr)
        if not pii:
            return []
        grouped = _pii_by_table(pii)
        candidates = self._op_candidates(pr)
        if not candidates:
            candidates = self._ast_candidates(pr)
        issues: list[Issue] = []
        emitted: set[tuple[str, int | None]] = set()
        unresolved = False
        for line, tables in candidates:
            if not tables:
                unresolved = True
                continue
            for table_name in tables:
                hits = _table_pii(grouped, table_name)
                if not hits:
                    continue
                key = (table_name.lower(), line)
                if key in emitted:
                    continue
                emitted.add(key)
                cols = ", ".join(f"{p.column} ({p.pii_type})" for p in hits[:6])
                issues.append(
                    self.issue(
                        f"SELECT * reads table '{table_name}' which contains PII "
                        f"column(s): {cols}. Every consumer of this query receives "
                        "them implicitly.",
                        line=line if line is not None else self._star_line(pr),
                        fix_suggestion=(
                            "List the needed columns explicitly and exclude or "
                            "mask PII columns (e.g. SELECT id, sha2(email, 256) AS "
                            "email_hash, ...)."
                        ),
                    )
                )
        if unresolved and not issues:
            sample = ", ".join(f"{p.table}.{p.column}" for p in pii[:5])
            issues.append(
                self.issue(
                    "SELECT * is used while the pipeline references PII columns "
                    f"({sample}); the star projection may propagate them downstream.",
                    line=self._star_line(pr),
                    fix_suggestion=(
                        "Replace SELECT * with an explicit, PII-aware column list."
                    ),
                )
            )
        return issues

    def _op_candidates(self, pr: ParseResult) -> list[tuple[int | None, list[str]]]:
        """Star-select candidates from parser-emitted SELECT ops."""
        candidates: list[tuple[int | None, list[str]]] = []
        for op in pr.ir.ops("SELECT"):
            details = op.details or {}
            star = bool(
                details.get("star") or details.get("select_star") or details.get("is_star")
            )
            columns = details.get("columns")
            if not star and isinstance(columns, (list, tuple)):
                star = any(str(c).strip() == "*" for c in columns)
            if not star and str(details.get("projection", "")).strip() == "*":
                star = True
            if not star:
                continue
            raw_tables = details.get("tables")
            tables: list[str] = []
            if isinstance(raw_tables, (list, tuple)):
                tables = [str(t) for t in raw_tables if t]
            else:
                single = details.get("table") or details.get("from") or details.get("source")
                if single:
                    tables = [str(single)]
            candidates.append((_op_line(op), tables))
        return candidates

    def _ast_candidates(self, pr: ParseResult) -> list[tuple[int | None, list[str]]]:
        """Star-select candidates from the sqlglot AST (SQL-family formats)."""
        if not isinstance(pr.ast, list):
            return []
        try:
            from sqlglot import exp
        except Exception:  # pragma: no cover - sqlglot is an installed dep
            logger.exception("sqlglot unavailable for SELECT * analysis")
            return []
        candidates: list[tuple[int | None, list[str]]] = []
        try:
            for tree in pr.ast:
                if not isinstance(tree, exp.Expression):
                    continue
                for select in tree.find_all(exp.Select):
                    star = any(
                        isinstance(projection, exp.Star)
                        or (
                            isinstance(projection, exp.Column)
                            and isinstance(projection.this, exp.Star)
                        )
                        for projection in select.expressions
                    )
                    if not star:
                        continue
                    tables = [t.name for t in select.find_all(exp.Table) if t.name]
                    candidates.append((None, tables))
        except Exception:  # pragma: no cover - defensive against odd ASTs
            logger.exception("AST walk for SELECT * failed")
        return candidates

    def _star_line(self, pr: ParseResult) -> int | None:
        """Best-effort line of the first ``SELECT *`` in the raw source."""
        match = _SELECT_STAR_RE.search(pr.source)
        return _pattern_line(pr.source, match.start()) if match else None


@register
class UnmaskedPiiToOutputRule(Rule):
    """Direct copies of PII into outputs spread sensitive data downstream."""

    id = "UNMASKED_PII_TO_OUTPUT"
    severity = "WARNING"
    category = "security"
    formats = ALL_FORMATS
    title = "Unmasked PII flows to output"
    description = (
        "Column lineage shows a detected PII column copied verbatim (direct "
        "transformation) into an output table, exporting raw PII downstream."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag direct lineage edges whose source is a detected PII column."""
        index = _pii_index(detect_pii_columns(pr))
        if not index:
            return []
        issues: list[Issue] = []
        seen: set[tuple[str, str, str, str]] = set()
        for cl in pr.ir.column_lineage:
            if cl.transformation != "direct":
                continue
            p = _lookup(index, cl.source_table, cl.source_column)
            if p is None or _flow_is_protected(cl):
                continue
            key = (
                (cl.source_table or "").lower(),
                (cl.source_column or "").lower(),
                (cl.output_table or "").lower(),
                (cl.output_column or "").lower(),
            )
            if key in seen:
                continue
            seen.add(key)
            issues.append(
                self.issue(
                    f"PII column {cl.source_table}.{cl.source_column} "
                    f"({p.pii_type}) is copied unmodified to output "
                    f"{cl.output_table}.{cl.output_column}.",
                    line=_find_line(pr, cl.output_column),
                    fix_suggestion=p.recommendation,
                )
            )
        return issues


@register
class PiiInJoinKeyRule(Rule):
    """Joining on raw PII spreads it across execution plans, spills and logs."""

    id = "PII_IN_JOIN_KEY"
    severity = "WARNING"
    category = "security"
    formats = ALL_FORMATS
    title = "PII used as join key"
    description = (
        "A join condition uses email/phone/SSN-like columns; raw PII then appears "
        "in shuffle files, spill data, query plans and engine logs."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag JOIN ops whose condition references high-confidence PII names."""
        issues: list[Issue] = []
        for op in pr.ir.ops("JOIN"):
            details = op.details or {}
            condition = details.get("condition") or details.get("on") or ""
            text = condition if isinstance(condition, str) else str(condition)
            keys = details.get("keys") or details.get("using")
            if isinstance(keys, (list, tuple)):
                text += " " + " ".join(str(k) for k in keys)
            if not text.strip():
                continue
            matched: dict[str, str] = {}
            for identifier in _IDENTIFIER_RE.findall(text):
                match = classify_pii_name(identifier)
                if match is not None and match.confidence >= 0.8:
                    matched[identifier.lower()] = match.pii_type
            if not matched:
                continue
            described = ", ".join(f"{name} ({ptype})" for name, ptype in sorted(matched.items()))
            issues.append(
                self.issue(
                    f"Join condition uses PII column(s) as keys: {described}. Raw "
                    "values will be shuffled across the cluster and may persist in "
                    "spill files and logs.",
                    line=_op_line(op),
                    fix_suggestion=(
                        "Join on a surrogate key, or hash both sides first (e.g. "
                        "sha2(email, 256)) so equality still holds without exposing "
                        "raw PII."
                    ),
                )
            )
        return issues


@register
class PublicCloudPathRule(Rule):
    """World-readable buckets turn pipeline data into a public download."""

    id = "PUBLIC_CLOUD_PATH"
    severity = "WARNING"
    category = "security"
    formats = ALL_FORMATS
    title = "Public cloud storage path"
    description = (
        "The pipeline reads or writes an s3://, gs:// or azure path that looks "
        "public/world-readable, or sets an explicitly public ACL."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag public-looking bucket paths and explicit public ACL markers."""
        issues: list[Issue] = []
        flagged_lines: set[int] = set()
        for line_no, text in enumerate(pr.lines, start=1):
            for m in _CLOUD_URL_RE.finditer(text):
                target = (m.group("bucket") + m.group("path")).lower()
                if not any(token in target for token in _PUBLIC_PATH_TOKENS):
                    continue
                if line_no in flagged_lines:
                    continue
                flagged_lines.add(line_no)
                issues.append(
                    self.issue(
                        f"Cloud storage path on line {line_no} references a "
                        f"public-looking bucket ('{m.group('bucket')}'); data "
                        "written there may be world-readable.",
                        line=line_no,
                        fix_suggestion=(
                            "Use a private bucket with block-public-access enabled "
                            "and grant access via IAM roles instead of bucket ACLs."
                        ),
                    )
                )
            if line_no not in flagged_lines and _PUBLIC_ACL_RE.search(text):
                flagged_lines.add(line_no)
                issues.append(
                    self.issue(
                        f"Explicit public-access ACL on line {line_no} (e.g. "
                        "public-read / allUsers) makes the object world-readable.",
                        line=line_no,
                        fix_suggestion=(
                            "Remove the public ACL; share data through signed URLs "
                            "or IAM-scoped access instead."
                        ),
                    )
                )
        return issues


@register
class GrantBroadAccessRule(Rule):
    """GRANT ALL / TO PUBLIC hands the dataset to every account."""

    id = "GRANT_BROAD_ACCESS"
    severity = "WARNING"
    category = "security"
    formats = ALL_FORMATS
    title = "Over-broad privilege grant"
    description = (
        "A GRANT ALL or GRANT ... TO PUBLIC statement gives blanket privileges, "
        "violating least-privilege and exposing data to every database user."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Flag GRANT ALL and GRANT ... TO PUBLIC statements (fingerprint scan)."""
        issues: list[Issue] = []
        flagged_lines: set[int] = set()
        for m in _GRANT_ALL_RE.finditer(pr.source):
            line = _pattern_line(pr.source, m.start())
            if line in flagged_lines:
                continue
            flagged_lines.add(line)
            issues.append(
                self.issue(
                    f"GRANT ALL on line {line} hands every privilege (including "
                    "write/ownership-adjacent rights) instead of the minimum needed.",
                    line=line,
                    fix_suggestion=(
                        "Grant only the specific privileges required (e.g. GRANT "
                        "SELECT) to a dedicated role."
                    ),
                )
            )
        for m in _GRANT_TO_PUBLIC_RE.finditer(pr.source):
            line = _pattern_line(pr.source, m.start())
            if line in flagged_lines:
                continue
            flagged_lines.add(line)
            issues.append(
                self.issue(
                    f"GRANT ... TO PUBLIC starting on line {line} exposes the "
                    "object to every account in the database.",
                    line=line,
                    fix_suggestion=(
                        "Grant to a named, purpose-specific role and add users to "
                        "that role instead of PUBLIC."
                    ),
                )
            )
        return issues


# ---------------------------------------------------------------------------
# INFO
# ---------------------------------------------------------------------------


@register
class PiiColumnNamedRule(Rule):
    """A PII inventory is the prerequisite for every masking policy."""

    id = "PII_COLUMN_NAMED"
    severity = "INFO"
    category = "security"
    formats = ALL_FORMATS
    title = "PII columns present"
    description = (
        "Columns with PII-indicating names are referenced by this pipeline. This is "
        "an inventory notice so owners can confirm classification and handling."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Emit one inventory issue listing detected PII columns."""
        pii = detect_pii_columns(pr)
        if not pii:
            return []
        listed = ", ".join(f"{p.table}.{p.column} ({p.pii_type})" for p in pii[:8])
        extra = f" and {len(pii) - 8} more" if len(pii) > 8 else ""
        return [
            self.issue(
                f"{len(pii)} potential PII column(s) detected: {listed}{extra}. "
                "Treat these as sensitive in every downstream consumer.",
                fix_suggestion=(
                    "Record these columns in your PII inventory and apply "
                    "warehouse-level tags/masking policies so handling is enforced "
                    "centrally."
                ),
            )
        ]


@register
class MissingMaskingHintRule(Rule):
    """PII landing in outputs without masking deserves an explicit decision."""

    id = "MISSING_MASKING_HINT"
    severity = "INFO"
    category = "security"
    formats = ALL_FORMATS
    title = "PII output without masking"
    description = (
        "An output table receives PII-named columns with no mask/hash function "
        "applied anywhere in the producing expressions."
    )

    def check(self, pr: ParseResult) -> list[Issue]:
        """Emit one issue per output table writing unmasked PII-named columns."""
        index = _pii_index(detect_pii_columns(pr))
        per_table: dict[str, set[str]] = {}
        for cl in pr.ir.column_lineage:
            match = classify_pii_name(cl.output_column)
            if match is None or has_masking_marker(cl.expression or ""):
                continue
            source_pii = _lookup(index, cl.source_table, cl.source_column)
            if cl.transformation == "direct" and source_pii is not None:
                continue  # already covered by UNMASKED_PII_TO_OUTPUT
            per_table.setdefault(cl.output_table or "unknown", set()).add(cl.output_column)
        for table_ref in pr.ir.tables_by_access("write"):
            for col in table_ref.columns or []:
                if classify_pii_name(col) is not None:
                    per_table.setdefault(table_ref.name, set()).add(col)
        issues: list[Issue] = []
        for table_name in sorted(per_table):
            columns = sorted(per_table[table_name])
            listed = ", ".join(columns[:6])
            extra = f" and {len(columns) - 6} more" if len(columns) > 6 else ""
            issues.append(
                self.issue(
                    f"Output '{table_name}' receives PII column(s) {listed}{extra} "
                    "with no apparent mask or hash function applied.",
                    line=_find_line(pr, columns[0]),
                    fix_suggestion=(
                        "Apply masking at write time - e.g. sha2(email, 256), "
                        "regexp_replace for partial masking, or a warehouse dynamic "
                        "masking policy - so raw PII never lands in outputs."
                    ),
                )
            )
        return issues
