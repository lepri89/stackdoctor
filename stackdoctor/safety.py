"""Safety layer: SQL validation, Redis command allowlist, secret redaction, output caps.

Everything that leaves stackdoctor goes through `safe_output()`.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import re
import uuid
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import TokenType


class UnsafeError(ValueError):
    """Raised when a request would break the read-only guarantee."""


# ---------------------------------------------------------------------------
# SQL validation
# ---------------------------------------------------------------------------

# Functions that have side effects or read server files, even inside a
# read-only transaction (or that execute arbitrary SQL from a string).
FORBIDDEN_FUNCTIONS = {
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "set_config", "pg_rotate_logfile", "pg_switch_wal", "pg_promote",
    "pg_create_restore_point", "pg_log_backend_memory_contexts",
    "pg_notify", "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "nextval", "setval",
    "query_to_xml", "query_to_xml_and_xmlschema", "query_to_xmlschema",
    "cursor_to_xml", "cursor_to_xmlschema",
    "pg_file_write", "pg_file_rename", "pg_file_unlink", "pg_logdir_ls",
}
FORBIDDEN_PREFIXES = (
    "lo_", "dblink", "pg_advisory_", "pg_try_advisory_", "pg_ls_",
    "pg_read_", "pg_replication_", "pg_create_", "pg_drop_",
)

# Nodes that must never appear anywhere in the tree (catches writable CTEs).
FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop,
    exp.Alter, exp.Command, exp.Into, exp.Lock, exp.Copy, exp.Set,
    exp.Transaction, exp.Commit, exp.Rollback, exp.TruncateTable,
)

_EXPLAIN_SAFE_OPTIONS = {"VERBOSE", "COSTS", "SETTINGS", "BUFFERS", "FORMAT",
                         "SUMMARY", "GENERIC_PLAN", "TEXT", "JSON", "YAML",
                         "XML", "TRUE", "FALSE", "ON", "OFF", "1", "0"}


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).lower()
    return node.sql_name().lower()


def _check_select_tree(tree: exp.Expression) -> None:
    if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
        raise UnsafeError(f"Only SELECT / WITH / EXPLAIN are allowed (got {tree.key.upper()}).")
    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise UnsafeError(f"Statement contains a forbidden clause: {node.key.upper()}.")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise UnsafeError("SELECT ... INTO is not allowed.")
        if isinstance(node, exp.Func):
            name = _function_name(node)
            if name in FORBIDDEN_FUNCTIONS or name.startswith(FORBIDDEN_PREFIXES):
                raise UnsafeError(f"Function {name}() is not allowed (side effects or file access).")


def _parse_single(sql: str) -> exp.Expression:
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="postgres")
                      if s is not None and not isinstance(s, exp.Semicolon)]
    except (ParseError, TokenError) as e:
        raise UnsafeError(f"Could not parse SQL: {str(e).splitlines()[0]}") from None
    if len(statements) != 1:
        raise UnsafeError("Exactly one statement is allowed.")
    return statements[0]


def _strip_trailing(sql: str) -> str:
    """Return the SQL without trailing semicolons/comments, using the tokenizer."""
    tokens = [t for t in sqlglot.tokenize(sql, dialect="postgres") if t.token_type != TokenType.SEMICOLON]
    if not tokens:
        raise UnsafeError("Empty SQL.")
    return sql[: tokens[-1].end + 1]


def validate_select(sql: str, limit: int) -> str:
    """Validate `sql` as a single read-only query and return the SQL to execute.

    SELECT / WITH queries are wrapped so a row LIMIT always applies.
    EXPLAIN (without ANALYZE) is returned unwrapped.
    """
    if not sql or not sql.strip():
        raise UnsafeError("Empty SQL.")
    try:
        tokens = sqlglot.tokenize(sql, dialect="postgres")
    except TokenError as e:
        raise UnsafeError(f"Could not tokenize SQL: {e}") from None
    if not tokens:
        raise UnsafeError("Empty SQL.")

    if tokens[0].text.upper() == "EXPLAIN":
        # The postgres tokenizer swallows everything after EXPLAIN as one string,
        # so tokenize the remainder on its own.
        sql = sql[tokens[0].end + 1:]
        tokens = sqlglot.tokenize(sql, dialect="postgres")
        i = 0
        if i < len(tokens) and tokens[i].token_type == TokenType.L_PAREN:
            depth = 0
            while i < len(tokens):
                t = tokens[i]
                if t.token_type == TokenType.L_PAREN:
                    depth += 1
                elif t.token_type == TokenType.R_PAREN:
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                elif t.text.upper() not in _EXPLAIN_SAFE_OPTIONS and t.token_type != TokenType.COMMA:
                    raise UnsafeError(f"EXPLAIN option {t.text.upper()} is not allowed (no ANALYZE).")
                i += 1
        else:
            while i < len(tokens) and tokens[i].text.upper() in {"VERBOSE", "ANALYZE", "ANALYSE"}:
                if tokens[i].text.upper() in {"ANALYZE", "ANALYSE"}:
                    raise UnsafeError("EXPLAIN ANALYZE executes the query and is not allowed.")
                i += 1
        if i >= len(tokens):
            raise UnsafeError("EXPLAIN needs a query.")
        inner = sql[tokens[i].start:]
        _check_select_tree(_parse_single(inner))
        return "EXPLAIN " + _strip_trailing(sql)

    _check_select_tree(_parse_single(sql))
    body = _strip_trailing(sql)
    # Newlines keep a trailing `-- comment` from swallowing the wrapper.
    return f"SELECT * FROM (\n{body}\n) AS sd_sub LIMIT {int(limit)}"


# ---------------------------------------------------------------------------
# Redis allowlist (subcommand-aware)
# ---------------------------------------------------------------------------

# None = no subcommand check; a set = only these subcommands are allowed.
REDIS_ALLOWED: dict[str, set[str] | None] = {
    "PING": None, "INFO": None, "DBSIZE": None, "SCAN": None, "TYPE": None,
    "TTL": None, "PTTL": None, "EXISTS": None, "GET": None, "MGET": None,
    "GETRANGE": None, "STRLEN": None, "LLEN": None, "LRANGE": None,
    "HLEN": None, "HSCAN": None, "SCARD": None, "SSCAN": None,
    "ZCARD": None, "ZRANGE": None, "XLEN": None,
    "CLIENT": {"LIST", "INFO"},
    "OBJECT": {"ENCODING", "IDLETIME", "FREQ"},
    "MEMORY": {"USAGE", "STATS"},
    "CONFIG": {"GET"},
    "SLOWLOG": {"GET", "LEN"},
}


def check_redis_command(*args: Any) -> None:
    """Raise UnsafeError unless the command is on the read-only allowlist.

    Accepts both ("CLIENT", "LIST") and ("CLIENT LIST",) forms, like redis-py.
    """
    if not args:
        raise UnsafeError("Empty Redis command.")
    parts = str(args[0]).split() + [str(a) for a in args[1:]]
    if not parts:
        raise UnsafeError("Empty Redis command.")
    cmd = parts[0].upper()
    if cmd not in REDIS_ALLOWED:
        raise UnsafeError(f"Redis command {cmd} is not on the read-only allowlist.")
    subs = REDIS_ALLOWED[cmd]
    if subs is not None:
        sub = parts[1].upper() if len(parts) > 1 else ""
        if sub not in subs:
            raise UnsafeError(f"Redis command {cmd} {sub} is not on the read-only allowlist.")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

MASK = "***"
_SECRET_WORDS = r"(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|auth|credential|session[_-]?id|cookie|dsn)"
SENSITIVE_KEY_RE = re.compile(rf"(?i)^[\w.-]*{_SECRET_WORDS}[\w.-]*$")

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # scheme://user:password@host  ->  scheme://user:***@host
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^:/\s@]*:)([^@\s/]+)@"), rf"\1{MASK}@"),
    # Authorization / Proxy-Authorization headers
    (re.compile(r"(?i)\b((?:proxy-)?authorization[\"']?\s*[:=]\s*[\"']?)(?:(bearer|basic|token|digest)\s+)?[^\s\"',}]+"),
     lambda m: f"{m.group(1)}{(m.group(2) + ' ') if m.group(2) else ''}{MASK}"),
    # Bare bearer tokens
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}"), rf"\1{MASK}"),
    # KEY=value, key: value, "key": "value" where key looks secret
    (re.compile(rf"(?i)([\"']?\b[\w.-]*{_SECRET_WORDS}[\w.-]*[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}}]+)"),
     lambda m: m.group(1) + (m.group(2)[0] + MASK + m.group(2)[0] if m.group(2)[:1] in "\"'" else MASK)),
    # Well-known token formats
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), MASK),                     # AWS access key id
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"), MASK),                    # GitHub
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"), MASK),                  # Slack
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"), MASK),   # Stripe
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), MASK),                         # OpenAI / Anthropic style
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"), MASK),                        # Google API key
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), MASK),  # JWT
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), MASK),
]


def redact_text(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact(obj: Any) -> Any:
    """Recursively redact secrets in strings and in values under secret-looking keys."""
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and SENSITIVE_KEY_RE.match(k) and isinstance(v, (str, int, float, bytes)):
                out[k] = MASK
            else:
                out[k] = redact(v)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(v) for v in obj]
    return obj


def preview(value: Any, max_chars: int = 120) -> str:
    """Short, redacted, single-line preview of any value (task args, Redis values...)."""
    text = value if isinstance(value, str) else repr(value)
    text = redact_text(" ".join(text.split()))
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


# ---------------------------------------------------------------------------
# JSON + output caps
# ---------------------------------------------------------------------------

def _json_default(o: Any) -> Any:
    if isinstance(o, dt.datetime):
        if o.tzinfo is None:
            o = o.astimezone()
        return o.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    if isinstance(o, (dt.date, dt.time)):
        return o.isoformat()
    if isinstance(o, dt.timedelta):
        return round(o.total_seconds(), 1)
    if isinstance(o, decimal.Decimal):
        return float(o)
    if isinstance(o, (uuid.UUID, memoryview)):
        return str(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    return str(o)


def to_jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, default=_json_default))


def _largest_list(obj: Any, best: tuple[int, list | None] = (0, None)) -> tuple[int, list | None]:
    if isinstance(obj, list):
        if len(obj) > best[0]:
            best = (len(obj), obj)
        for v in obj:
            best = _largest_list(v, best)
    elif isinstance(obj, dict):
        for v in obj.values():
            best = _largest_list(v, best)
    return best


def _shorten_strings(obj: Any, max_len: int) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= max_len else obj[:max_len] + "…"
    if isinstance(obj, dict):
        return {k: _shorten_strings(v, max_len) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shorten_strings(v, max_len) for v in obj]
    return obj


def cap_output(obj: Any, max_chars: int) -> Any:
    """Shrink a JSON-able object until it serializes under `max_chars`."""
    size = lambda o: len(json.dumps(o))  # noqa: E731
    if size(obj) <= max_chars:
        return obj
    truncated = False
    for _ in range(40):
        n, lst = _largest_list(obj)
        if lst is None or n <= 3:
            break
        dropped = n - n // 2
        del lst[n // 2:]
        lst.append({"_truncated": f"{dropped} more items omitted"})
        truncated = True
        if size(obj) <= max_chars:
            break
    for max_len in (1000, 300, 100):
        if size(obj) <= max_chars:
            break
        obj = _shorten_strings(obj, max_len)
        truncated = True
    if size(obj) > max_chars:
        text = json.dumps(obj)
        return {"_truncated": True, "preview": text[: max_chars - 100]}
    if truncated and isinstance(obj, dict):
        obj["_output_truncated"] = True
    return obj


def safe_output(obj: Any, max_chars: int) -> Any:
    """Final gate for every tool response: JSON-safe, redacted, size-capped."""
    return cap_output(redact(to_jsonable(obj)), max_chars)
