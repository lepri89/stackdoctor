"""Live checks against real services. Opt-in: set STACKDOCTOR_TEST_DATABASE_URL / STACKDOCTOR_TEST_REDIS_URL
(e.g. the demo: postgresql://shop:shop@localhost:55432/shop and redis://localhost:56379/0)."""

import os

import psycopg
import pytest

from stackdoctor.checks import postgres
from stackdoctor.config import Config

PG = os.environ.get("STACKDOCTOR_TEST_DATABASE_URL")
REDIS = os.environ.get("STACKDOCTOR_TEST_REDIS_URL")


@pytest.fixture
def pg_config(monkeypatch):
    if not PG:
        pytest.skip("STACKDOCTOR_TEST_DATABASE_URL not set")
    monkeypatch.setattr(postgres, "get_config", lambda: Config(database_url=PG))


def test_session_is_read_only_even_for_superuser(pg_config):
    with postgres.connect() as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "5s"
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute("CREATE TABLE stackdoctor_should_not_exist (a int)")


def test_statement_timeout_applies(pg_config):
    with postgres.connect() as conn, pytest.raises(psycopg.errors.QueryCanceled):
        conn.execute("SELECT count(*) FROM generate_series(1, 1e12)")


def test_run_select_limits_rows(pg_config):
    out = postgres.run_select("SELECT g FROM generate_series(1, 5000) g", limit=7)
    assert out["row_count"] == 7
    assert postgres.run_select("SELECT pg_sleep(1)")["rejected"]


def test_redis_wrapper_live():
    if not REDIS:
        pytest.skip("STACKDOCTOR_TEST_REDIS_URL not set")
    from stackdoctor.checks.redis import SafeRedis
    from stackdoctor.safety import UnsafeError

    r = SafeRedis(REDIS)
    assert r.cmd("PING")
    with pytest.raises(UnsafeError):
        r.cmd("SET", "stackdoctor_test", "x")
    assert not r.cmd("EXISTS", "stackdoctor_test")
