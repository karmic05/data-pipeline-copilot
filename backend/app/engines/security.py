"""Security engine: PII column detection and whole-pipeline security scanning.

``detect_pii_columns`` fingerprints column names from ``ir.tables`` and
``ir.column_lineage`` against tiered PII naming patterns (regex over column
*names* only — never over SQL structure). ``scan_security`` combines PII
detection, hardcoded-secret scanning and column-lineage flow analysis into the
:class:`app.schemas.report.SecurityReport` consumed by the API and frontend.

``classify_pii_name``, ``find_hardcoded_secrets`` and ``has_masking_marker``
are shared with ``app.rules.security_rules`` (same owner, allowed import).
"""
from __future__ import annotations

import logging
import re
from typing import NamedTuple

from app.schemas.ir import ParseResult
from app.schemas.report import PIIColumn, SecurityReport

logger = logging.getLogger(__name__)

__all__ = [
    "PIIMatch",
    "SecretFinding",
    "SECRET_KIND_LABELS",
    "classify_pii_name",
    "detect_pii_columns",
    "find_hardcoded_secrets",
    "has_masking_marker",
    "scan_security",
]


class PIIMatch(NamedTuple):
    """Result of classifying a single column name against the PII tiers."""

    pii_type: str
    confidence: float
    recommendation: str


class SecretFinding(NamedTuple):
    """A redacted hardcoded-secret hit. ``identifier`` NEVER holds the value."""

    line: int
    kind: str  # credential_assignment | aws_access_key | connection_url
    identifier: str


SECRET_KIND_LABELS: dict[str, str] = {
    "credential_assignment": "credential assignment",
    "aws_access_key": "AWS access key ID",
    "connection_url": "connection string with embedded password",
}

# --------------------------------------------------------------------------
# PII name classification (tiered patterns, matched on snake_case tokens)
# --------------------------------------------------------------------------

# Ordered: highest-confidence tiers first; within a tier, more specific types
# first (e.g. "email" must win over "address" for "email_address").
_PII_SPEC: tuple[tuple[str, float, str, str], ...] = (
    # -- high confidence (0.9): government / financial identifiers ----------
    ("ssn", 0.9, r"ssn|social_security(?:_number|_num|_no)?",
     "Hash (salted SHA-256) or tokenize SSNs; never store or select them in plaintext."),
    ("passport", 0.9, r"passport(?:_number|_num|_no)?",
     "Hash or tokenize passport numbers and restrict access to need-to-know roles."),
    ("credit_card", 0.9, r"credit_card|card_number|card_num|card_no|cc_number|cc_num",
     "Tokenize card numbers through a PCI-compliant vault; display at most the last 4 digits."),
    ("cvv", 0.9, r"cvv2?|cvc",
     "Drop this column entirely — persisting CVV/CVC values violates PCI DSS."),
    ("iban", 0.9, r"iban",
     "Tokenize or mask IBANs; expose only the trailing characters."),
    ("tax_id", 0.9, r"tax_id|taxid|tax_identifier|national_id",
     "Hash or tokenize tax identifiers; never propagate raw values downstream."),
    # -- strong (0.8): direct personal contact / sensitive attributes -------
    ("email", 0.8, r"email|e_mail",
     "Mask (e.g. j***@example.com) or hash emails before sharing downstream."),
    ("phone", 0.8, r"phone|mobile|telephone|msisdn",
     "Mask all but the last 2-4 digits, or hash phone numbers used as join keys."),
    ("date_of_birth", 0.8, r"dob|date_of_birth|birth_date|birthdate|birthday",
     "Generalize to birth year or an age band; avoid exposing the full date of birth."),
    ("salary", 0.8, r"salary|wage|compensation",
     "Restrict to need-to-know roles or bucket into salary bands for analytics."),
    ("password", 0.8, r"password|passwd|pwd",
     "Drop from pipeline outputs; store only strong KDF hashes (argon2/bcrypt)."),
    ("address", 0.8, r"address|street",
     "Mask or generalize addresses to city/region level for analytical use."),
    # -- medium (0.6): quasi-identifiers -------------------------------------
    ("first_name", 0.6, r"first_name|firstname|fname|given_name",
     "Pseudonymize or mask personal names outside production."),
    ("last_name", 0.6, r"last_name|lastname|lname|surname|family_name",
     "Pseudonymize or mask personal names outside production."),
    ("full_name", 0.6, r"full_name|fullname",
     "Pseudonymize or mask personal names outside production."),
    ("postal_code", 0.6, r"zip|zip_code|zipcode|postal_code|postcode",
     "Truncate postal codes to a coarse prefix or generalize to region."),
    ("ip_address", 0.6, r"ip|ip_address|ip_addr",
     "Truncate the final octet or hash IP addresses before storage."),
    ("geolocation", 0.6, r"latitude|longitude|lat|lng|lon",
     "Round coordinates to ~2 decimal places to reduce re-identification risk."),
    ("gender", 0.6, r"gender",
     "Drop unless strictly required; report gender only in aggregate."),
)

_PII_PATTERNS: list[tuple[str, float, re.Pattern[str], str]] = [
    (pii_type, confidence, re.compile(rf"(?:^|_)(?:{pattern})(?:_|$)"), rec)
    for pii_type, confidence, pattern, rec in _PII_SPEC
]

# Columns whose names indicate the value is already protected are NOT plaintext PII.
_PROTECTED_TOKENS = frozenset({
    "hash", "hashed", "sha", "sha1", "sha2", "sha256", "sha512", "md5", "hmac",
    "mask", "masked", "tokenized", "tokenised", "encrypted", "redacted",
    "anonymized", "anonymised", "pseudonymized", "pseudonymised", "digest",
    "obfuscated",
})

_MASKING_RE = re.compile(
    r"\b(?:hash\w*|sha\d*\w*|md5|hmac\w*|mask\w*|tokeni\w*|encrypt\w*|redact\w*"
    r"|anonymi\w*|pseudonymi\w*|digest|crypt\w*)\b",
    re.IGNORECASE,
)

_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_NON_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _normalize_name(name: object) -> tuple[str, frozenset[str]]:
    """Lowercase + snake_case a column name; return (joined_tokens, token set)."""
    snake = _CAMEL_BOUNDARY_RE.sub("_", str(name)).lower()
    tokens = [t for t in _NON_TOKEN_RE.split(snake) if t]
    return "_".join(tokens), frozenset(tokens)


def classify_pii_name(name: object) -> PIIMatch | None:
    """Classify a column name against the PII tiers.

    Returns the best :class:`PIIMatch` (first hit in confidence-ordered tiers)
    or ``None`` when the name does not look like PII, or already carries a
    protection marker (e.g. ``ssn_hash``, ``email_masked``).
    """
    if not name:
        return None
    joined, tokens = _normalize_name(name)
    if not joined or tokens & _PROTECTED_TOKENS:
        return None
    for pii_type, confidence, pattern, recommendation in _PII_PATTERNS:
        if pattern.search(joined):
            return PIIMatch(pii_type, confidence, recommendation)
    return None


def has_masking_marker(text: object) -> bool:
    """True when an expression / name contains evidence of masking or hashing."""
    return bool(text) and bool(_MASKING_RE.search(str(text)))


def detect_pii_columns(pr: ParseResult) -> list[PIIColumn]:
    """Detect likely-PII columns across the whole parse result.

    Scans every ``ir.tables[].columns`` entry plus both ends of every
    ``ir.column_lineage`` edge. Results are deduplicated per (table, column)
    keeping the highest confidence, and sorted by confidence (desc) then name
    for deterministic output.
    """
    found: dict[tuple[str, str], PIIColumn] = {}

    def add(table: object, column: object) -> None:
        if not column:
            return
        match = classify_pii_name(column)
        if match is None:
            return
        table_name = str(table or "").strip() or "unknown"
        key = (table_name.lower(), str(column).lower())
        existing = found.get(key)
        if existing is None or match.confidence > existing.confidence:
            found[key] = PIIColumn(
                table=table_name,
                column=str(column),
                pii_type=match.pii_type,
                confidence=match.confidence,
                recommendation=match.recommendation,
            )

    try:
        for table_ref in pr.ir.tables:
            for col in table_ref.columns or []:
                add(table_ref.name, col)
        for cl in pr.ir.column_lineage:
            add(cl.source_table, cl.source_column)
            add(cl.output_table, cl.output_column)
    except Exception:  # pragma: no cover - malformed IR must not kill analysis
        logger.exception("PII detection failed; returning partial results")

    return sorted(
        found.values(),
        key=lambda p: (-p.confidence, p.table.lower(), p.column.lower()),
    )


# --------------------------------------------------------------------------
# Hardcoded secret scanning (line-oriented fingerprinting over raw source)
# --------------------------------------------------------------------------

_CRED_WORDS = (
    r"password|passwd|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key"
    r"|secret[_\-]?key|auth[_\-]?token|client[_\-]?secret|private[_\-]?key|credentials?"
)
_ASSIGN_RE = re.compile(
    rf"(?P<name>[A-Za-z0-9_.\-]*(?:{_CRED_WORDS})[A-Za-z0-9_.\-]*)"
    rf"\s*[:=]\s*[\"'](?P<value>[^\"'\n]{{3,}})[\"']",
    re.IGNORECASE,
)
_OPTION_RE = re.compile(
    rf"[\"'](?P<name>[A-Za-z0-9_.\-]*(?:{_CRED_WORDS})[A-Za-z0-9_.\-]*)[\"']"
    rf"\s*[,:]\s*[\"'](?P<value>[^\"'\n]{{3,}})[\"']",
    re.IGNORECASE,
)
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_CRED_URL_RE = re.compile(
    r"\b(?P<scheme>postgres(?:ql)?|snowflake|mysql|mssql|mariadb|mongodb(?:\+srv)?"
    r"|redis|amqps?|ftp)://[^\s/:@\"']+:(?P<value>[^\s@\"']+)@",
    re.IGNORECASE,
)
_SAFE_LINE_RE = re.compile(
    r"os\.environ|getenv|dbutils\.secrets|secretsmanager|secrets_manager"
    r"|Variable\.get|EnvVar|env\(",
    re.IGNORECASE,
)
# Names whose suffix indicates metadata, not the secret itself.
_NON_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:type|name|file|path|url|uri|id|prefix|suffix|env|var|alias|label|kind)$",
    re.IGNORECASE,
)
_PLACEHOLDER_VALUES = frozenset({
    "", "none", "null", "true", "false", "changeme", "change_me", "example",
    "redacted", "placeholder", "dummy", "test", "sample", "your_password_here",
})


def _looks_templated(value: str) -> bool:
    """True when a quoted value is a template/placeholder, not a real secret."""
    v = value.strip()
    if not v or v.lower() in _PLACEHOLDER_VALUES:
        return True
    if v.startswith(("<", "$", "%", "{", "/", "./", "~", "file:")):
        return True
    if "{{" in v or "${" in v or "%(" in v:
        return True
    if set(v.lower()) <= {"*"} or set(v.lower()) <= {"x"}:
        return True
    return False


def find_hardcoded_secrets(source: str) -> list[SecretFinding]:
    """Scan raw source for hardcoded credentials, returning redacted findings.

    Detects credential-named assignments / option pairs with string-literal
    values, AWS access key IDs (``AKIA…``/``ASIA…``), and connection URLs with
    embedded passwords. Secret values are never included in the result — only
    the identifier (variable/option name, key prefix or URL scheme) and the
    1-based line number.
    """
    findings: list[SecretFinding] = []
    if not source:
        return findings
    seen: set[tuple[int, str]] = set()

    def emit(line_no: int, kind: str, identifier: str) -> None:
        key = (line_no, identifier.lower())
        if key not in seen:
            seen.add(key)
            findings.append(SecretFinding(line=line_no, kind=kind, identifier=identifier))

    for line_no, line in enumerate(source.splitlines(), start=1):
        if not line.strip() or _SAFE_LINE_RE.search(line):
            continue
        for pattern in (_ASSIGN_RE, _OPTION_RE):
            for m in pattern.finditer(line):
                name = m.group("name")
                if _NON_SECRET_NAME_RE.search(name) or _looks_templated(m.group("value")):
                    continue
                emit(line_no, "credential_assignment", name)
        for m in _AWS_KEY_RE.finditer(line):
            emit(line_no, "aws_access_key", m.group(0)[:4] + "…")
        for m in _CRED_URL_RE.finditer(line):
            if _looks_templated(m.group("value")):
                continue
            emit(line_no, "connection_url", m.group("scheme").lower() + "://…")
    return findings


# --------------------------------------------------------------------------
# Full security scan
# --------------------------------------------------------------------------


def scan_security(pr: ParseResult) -> SecurityReport:
    """Build the :class:`SecurityReport` for a parsed pipeline.

    Combines PII column detection, hardcoded-secret scanning and column-level
    lineage flow analysis. Risk level is ``HIGH`` when any secret is found or
    3+ distinct PII columns flow unmasked to an output, ``MEDIUM`` when any
    PII column is present, otherwise ``LOW``.
    """
    pii = detect_pii_columns(pr)
    findings: list[str] = []

    secrets = find_hardcoded_secrets(pr.source)
    for secret in secrets:
        label = SECRET_KIND_LABELS.get(secret.kind, secret.kind)
        findings.append(
            f"Hardcoded {label} ('{secret.identifier}') on line {secret.line} — "
            "value redacted."
        )

    # Index PII columns by (table, column), tolerating qualified table names.
    by_key: dict[tuple[str, str], PIIColumn] = {}
    for p in pii:
        col = p.column.lower()
        table = p.table.lower()
        by_key.setdefault((table, col), p)
        by_key.setdefault((table.split(".")[-1], col), p)

    reaching_output: set[tuple[str, str]] = set()
    seen_flows: set[tuple[str, str, str, str]] = set()
    for cl in pr.ir.column_lineage:
        src_table = (cl.source_table or "").lower()
        src_col = (cl.source_column or "").lower()
        match = by_key.get((src_table, src_col)) or by_key.get(
            (src_table.split(".")[-1], src_col)
        )
        if match is None:
            continue
        if has_masking_marker(cl.expression or ""):
            continue  # protected before reaching the output
        flow_key = (src_table, src_col, (cl.output_table or "").lower(),
                    (cl.output_column or "").lower())
        if flow_key in seen_flows:
            continue
        seen_flows.add(flow_key)
        reaching_output.add((src_table, src_col))
        findings.append(
            f"PII column {cl.source_table}.{cl.source_column} ({match.pii_type}, "
            f"confidence {match.confidence:.1f}) flows to output "
            f"{cl.output_table}.{cl.output_column} via {cl.transformation} "
            "transformation."
        )

    if pii:
        table_count = len({p.table.lower() for p in pii})
        findings.insert(
            0,
            f"Detected {len(pii)} potential PII column(s) across "
            f"{table_count} table(s).",
        )

    if secrets or len(reaching_output) >= 3:
        risk = "HIGH"
    elif pii:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    logger.debug(
        "Security scan: %d PII columns, %d secrets, %d unmasked output flows, risk=%s",
        len(pii), len(secrets), len(reaching_output), risk,
    )
    return SecurityReport(risk_level=risk, pii_columns=pii, findings=findings)
