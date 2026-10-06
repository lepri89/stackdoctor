import json

import pytest

from stackdoctor.safety import (
    MASK, UnsafeError, cap_output, check_redis_command, preview, redact, redact_text, validate_select,
)


# --- SQL ----------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from users where email like 'a%'",
    "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
    "SELECT 1 UNION ALL SELECT 2",
    "SELECT 1;",
    "SELECT pg_blocking_pids(123)",
    "SELECT * FROM pg_stat_activity -- trailing comment",
    "SELECT 1; -- comment after semicolon",
])
def test_select_allowed_and_wrapped(sql):
    out = validate_select(sql, 100)
    assert out.startswith("SELECT * FROM (\n")
    assert out.endswith(") AS sd_sub LIMIT 100")
    assert ";" not in out


def test_trailing_comment_cannot_swallow_limit():
    out = validate_select("SELECT 1 -- hi", 5)
    assert out.splitlines()[-1] == ") AS sd_sub LIMIT 5"


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "DROP TABLE t",
    "TRUNCATE t",
    "CREATE TABLE x (a int)",
    "ALTER TABLE t ADD COLUMN b int",
    "GRANT ALL ON t TO bob",
    "COPY t TO '/tmp/x'",
    "VACUUM",
    "SET statement_timeout = 0",
    "BEGIN",
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE t",
    "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",
    "WITH x AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM x",
    "SELECT * INTO newtable FROM t",
    "SELECT * FROM t FOR UPDATE",
    "SELECT * FROM t FOR SHARE",
    "",
    "   ",
    "not sql at all",
])
def test_non_select_rejected(sql):
    with pytest.raises(UnsafeError):
        validate_select(sql, 100)


@pytest.mark.parametrize("sql", [
    "SELECT pg_terminate_backend(123)",
    "SELECT pg_cancel_backend(123)",
    "SELECT pg_catalog.pg_terminate_backend(pid) FROM pg_stat_activity",
    'SELECT "pg_terminate_backend"(1)',
    "SELECT PG_RELOAD_CONF()",
    "SELECT pg_read_file('/etc/passwd')",
    "SELECT pg_read_binary_file('/etc/passwd')",
    "SELECT pg_ls_dir('.')",
    "SELECT lo_import('/etc/passwd')",
    "SELECT lo_export(1, '/tmp/x')",
    "SELECT lo_unlink(1)",
    "SELECT * FROM dblink('host=x', 'select 1') AS t(a int)",
    "SELECT dblink_exec('host=x', 'drop table t')",
    "SELECT set_config('statement_timeout', '0', false)",
    "SELECT pg_advisory_lock(1)",
    "SELECT pg_advisory_xact_lock(1)",
    "SELECT pg_try_advisory_lock(1)",
    "SELECT query_to_xml('select pg_terminate_backend(1)', true, true, '')",
    "SELECT nextval('seq')",
    "SELECT pg_sleep(100)",
    "WITH x AS (SELECT pg_cancel_backend(1)) SELECT * FROM x",
    "SELECT * FROM t WHERE id IN (SELECT pg_terminate_backend(1))",
    "SELECT 1 UNION SELECT pg_reload_conf()::int",
])
def test_side_effect_functions_rejected(sql):
    with pytest.raises(UnsafeError, match="not allowed"):
        validate_select(sql, 100)


@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT * FROM t",
    "explain (format json) select 1",
    "EXPLAIN (VERBOSE, COSTS OFF) SELECT 1",
    "EXPLAIN VERBOSE SELECT 1",
    "EXPLAIN WITH x AS (SELECT 1) SELECT * FROM x;",
    "EXPLAIN (FORMAT JSON) SELECT 1; -- comment",
])
def test_explain_allowed_unwrapped(sql):
    out = validate_select(sql, 100)
    assert out.upper().startswith("EXPLAIN")
    assert "sd_sub" not in out and ";" not in out


@pytest.mark.parametrize("sql", [
    "EXPLAIN ANALYZE SELECT 1",
    "EXPLAIN ANALYSE SELECT 1",
    "EXPLAIN VERBOSE ANALYZE SELECT 1",
    "EXPLAIN (ANALYZE) SELECT 1",
    "EXPLAIN (analyze true, format json) SELECT 1",
    "EXPLAIN (FORMAT JSON, ANALYZE) SELECT 1",
    "EXPLAIN DELETE FROM t",
    "EXPLAIN INSERT INTO t VALUES (1)",
    "EXPLAIN SELECT pg_terminate_backend(1)",
    "EXPLAIN SELECT 1; DROP TABLE t",
    "EXPLAIN",
])
def test_bad_explain_rejected(sql):
    with pytest.raises(UnsafeError):
        validate_select(sql, 100)


# --- Redis --------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ("PING",), ("INFO",), ("info", "memory"), ("SCAN", 0, "MATCH", "*", "COUNT", 100),
    ("TYPE", "k"), ("PTTL", "k"), ("LLEN", "celery"), ("GET", "k"), ("MGET", "a", "b"),
    ("CLIENT", "LIST"), ("CLIENT LIST",), ("client", "info"),
    ("OBJECT", "ENCODING", "k"), ("OBJECT ENCODING", "k"),
    ("MEMORY", "USAGE", "k"), ("MEMORY USAGE", "k"),
    ("CONFIG", "GET", "maxclients"), ("CONFIG GET", "maxmemory"),
    ("SLOWLOG GET", 5),
])
def test_redis_allowed(args):
    check_redis_command(*args)


@pytest.mark.parametrize("args", [
    ("KEYS", "*"), ("keys", "*"),
    ("SET", "k", "v"), ("DEL", "k"), ("UNLINK", "k"), ("EXPIRE", "k", 1),
    ("FLUSHALL",), ("FLUSHDB",), ("LPUSH", "q", "x"), ("RPOP", "q"), ("BLPOP", "q", 0),
    ("EVAL", "return 1", 0), ("EVALSHA", "abc", 0), ("SCRIPT", "LOAD", "x"), ("FUNCTION", "LIST"),
    ("SHUTDOWN",), ("DEBUG", "SLEEP", 1), ("MIGRATE",), ("RESTORE",), ("SAVE",), ("BGSAVE",),
    ("REPLICAOF", "x", 1), ("PUBLISH", "c", "m"), ("MULTI",), ("SELECT", 1),
    ("CLIENT", "KILL", "ID", 1), ("CLIENT KILL", "ID", 1), ("CLIENT", "PAUSE", 1000),
    ("CLIENT", "SETNAME", "x"), ("CLIENT", "NO-EVICT", "on"), ("CLIENT",),
    ("OBJECT", "HELP"), ("MEMORY", "PURGE"), ("MEMORY", "DOCTOR"),
    ("CONFIG", "SET", "maxmemory", "1"), ("CONFIG SET", "save", ""), ("CONFIG", "REWRITE"),
    ("CONFIG", "RESETSTAT"), ("SLOWLOG", "RESET"), ("",), (),
])
def test_redis_blocked(args):
    with pytest.raises(UnsafeError):
        check_redis_command(*args)


def test_redis_client_wrapper_blocks_before_sending(monkeypatch):
    from stackdoctor.checks.redis import SafeRedis

    r = SafeRedis("redis://localhost:1/0")
    sent = []
    monkeypatch.setattr(r._r, "execute_command", lambda *a: sent.append(a))
    with pytest.raises(UnsafeError):
        r.cmd("FLUSHALL")
    r.cmd("PING")
    assert sent == [("PING",)]


# --- Redaction ----------------------------------------------------------------

@pytest.mark.parametrize("text,secret", [
    ("postgresql://app:s3cretpw@db:5432/app", "s3cretpw"),
    ("redis://:hunter2@redis:6379/0", "hunter2"),
    ("amqp://guest:guestpass@rabbit//", "guestpass"),
    ("Authorization: Bearer abc.def.ghi123456", "abc.def.ghi123456"),
    ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
    ('{"Authorization": "Token zzzsecretzzz"}', "zzzsecretzzz"),
    ("calling with bearer tok_1234567890abcdef", "tok_1234567890abcdef"),
    ("DB_PASSWORD=supersecret", "supersecret"),
    ("export AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG", "wJalrXUtnFEMI/K7MDENG"),
    ("api_key: 'abcd1234'", "abcd1234"),
    ('{"password": "p@ss w0rd"}', "p@ss w0rd"),
    ("STRIPE_KEY sk_live_51Habcdefghijklmnop", "sk_live_51Habcdefghijklmnop"),
    ("key AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("xoxb-123456789012-abcdefghij", "xoxb-123456789012-abcdefghij"),
    ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"),
    ("jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
     "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
    ("session_id=abc123xyz; path=/", "abc123xyz"),
])
def test_redact_text(text, secret):
    out = redact_text(text)
    assert secret not in out
    assert MASK in out


def test_redact_keeps_normal_text():
    text = "Task app.tasks.process[abc-123] succeeded in 0.5s: user 42 logged in"
    assert redact_text(text) == text
    assert redact_text("postgresql://app@db/app") == "postgresql://app@db/app"


def test_redact_nested_structures_and_sensitive_keys():
    data = {
        "url": "redis://:pw123@r:6379",
        "headers": {"Authorization": "Bearer abcdefghijkl", "X-Api-Key": "k-1234"},
        "kwargs": {"password": "pw", "token": 12345, "name": "bob"},
        "list": ["SECRET_KEY=django-insecure-xyz", "fine"],
    }
    out = redact(data)
    dumped = json.dumps(out)
    for s in ("pw123", "abcdefghijkl", "k-1234", '"pw"', "12345", "django-insecure-xyz"):
        assert s not in dumped
    assert out["kwargs"]["name"] == "bob" and out["list"][1] == "fine"


def test_preview_redacts_and_truncates():
    p = preview({"password": "hunter2", "data": "x" * 500}, 80)
    assert "hunter2" not in p and len(p) <= 80


def test_task_args_preview_redacts():
    args = "('user@example.com', 'postgres://u:topsecret@db/x')"
    assert "topsecret" not in preview(args)


# --- Output caps --------------------------------------------------------------

def test_cap_output_shrinks_large_lists():
    data = {"rows": [{"i": i, "v": "x" * 50} for i in range(5000)]}
    out = cap_output(data, 5000)
    assert len(json.dumps(out)) <= 5000
    assert out["_output_truncated"] is True
    assert "_truncated" in json.dumps(out["rows"][-1])


def test_cap_output_long_string():
    out = cap_output({"blob": "y" * 100_000}, 2000)
    assert len(json.dumps(out)) <= 2000


def test_cap_output_small_unchanged():
    data = {"a": [1, 2, 3]}
    assert cap_output(data, 1000) == data
