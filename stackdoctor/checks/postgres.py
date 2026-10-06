"""Light Postgres checks. For deep health checks use Postgres MCP Pro.

Every connection is forced read-only with a statement timeout, at the server level.
"""

from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from ..config import get_config, skipped
from ..safety import UnsafeError, preview, validate_select
from . import event, utcnow

SOURCE = "postgres"
MAX_LIMIT = 1000
_OPTIONS = "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=2000"


@contextmanager
def connect():
    cfg = get_config()
    # Keyword args override anything in DATABASE_URL, so these always apply.
    conn = psycopg.connect(cfg.database_url, options=_OPTIONS, connect_timeout=5,
                           application_name="stackdoctor", row_factory=dict_row)
    try:
        conn.read_only = True
        ro = conn.execute("SHOW default_transaction_read_only").fetchone()
        if ro["default_transaction_read_only"] != "on":
            raise UnsafeError("Could not force a read-only session; refusing to run queries.")
        yield conn
    finally:
        conn.close()


def _not_configured():
    return skipped("DATABASE_URL not set") if not get_config().database_url else None


def active_queries(min_duration_s: float = 0) -> dict:
    if (s := _not_configured()):
        return s
    cfg = get_config()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pid, usename, application_name, client_addr::text, state,
                   wait_event_type, wait_event, backend_type,
                   xact_start, query_start, state_change,
                   EXTRACT(EPOCH FROM now() - query_start)::float AS duration_s,
                   EXTRACT(EPOCH FROM now() - xact_start)::float AS xact_age_s,
                   query
            FROM pg_stat_activity
            WHERE state IS DISTINCT FROM 'idle'
              AND pid <> pg_backend_pid()
              AND backend_type = 'client backend'
              AND COALESCE(EXTRACT(EPOCH FROM now() - query_start), 0) >= %s
            ORDER BY query_start NULLS LAST
            LIMIT 50
            """,
            (min_duration_s,),
        ).fetchall()

    events, queries = [], []
    for r in rows:
        r["query"] = preview(r["query"], 300)
        r["duration_s"] = round(r["duration_s"] or 0, 1)
        r["xact_age_s"] = round(r["xact_age_s"] or 0, 1)
        queries.append(r)
        if r["state"] == "idle in transaction" and r["xact_age_s"] >= cfg.long_query_s:
            events.append(event(r["xact_start"], SOURCE, "idle_in_transaction",
                                f"pid {r['pid']} idle in transaction for {r['xact_age_s']}s: {r['query']}",
                                "warning", pid=r["pid"]))
        elif r["state"] == "active" and r["duration_s"] >= cfg.long_query_s and r["wait_event_type"] != "Lock":
            events.append(event(r["query_start"], SOURCE, "long_query",
                                f"pid {r['pid']} running {r['duration_s']}s: {r['query']}",
                                "warning", pid=r["pid"]))
    return {"count": len(queries), "queries": queries, "events": events}


def blocking_locks() -> dict:
    if (s := _not_configured()):
        return s
    with connect() as conn:
        blocked = conn.execute(
            """
            SELECT a.pid, a.usename, a.application_name, a.query_start,
                   EXTRACT(EPOCH FROM now() - a.query_start)::float AS waiting_s,
                   pg_blocking_pids(a.pid) AS blocked_by,
                   (SELECT string_agg(DISTINCT l.relation::regclass::text, ', ')
                      FROM pg_locks l WHERE l.pid = a.pid AND NOT l.granted
                       AND l.relation IS NOT NULL) AS relation,
                   a.query
            FROM pg_stat_activity a
            WHERE cardinality(pg_blocking_pids(a.pid)) > 0
            ORDER BY a.query_start
            LIMIT 50
            """
        ).fetchall()
        blocker_pids = sorted({p for r in blocked for p in r["blocked_by"]})
        blockers = conn.execute(
            """
            SELECT pid, usename, application_name, state, xact_start, state_change,
                   EXTRACT(EPOCH FROM now() - xact_start)::float AS xact_age_s, query
            FROM pg_stat_activity WHERE pid = ANY(%s)
            """,
            (blocker_pids,),
        ).fetchall() if blocker_pids else []

    events = []
    for b in blockers:
        b["query"] = preview(b["query"], 300)
        b["xact_age_s"] = round(b["xact_age_s"] or 0, 1)
        waiting = [r["pid"] for r in blocked if b["pid"] in r["blocked_by"]]
        events.append(event(b["xact_start"], SOURCE, "lock_held",
                            f"pid {b['pid']} ({b['state']}) holds a lock blocking {len(waiting)} "
                            f"session(s) since transaction start: {b['query']}",
                            "critical", pid=b["pid"]))
    for r in blocked:
        r["query"] = preview(r["query"], 300)
        r["waiting_s"] = round(r["waiting_s"] or 0, 1)
        events.append(event(r["query_start"], SOURCE, "query_blocked",
                            f"pid {r['pid']} waiting {r['waiting_s']}s on {r['relation'] or 'a lock'} "
                            f"held by {r['blocked_by']}: {r['query']}",
                            "warning", pid=r["pid"]))
    return {"blocked_count": len(blocked), "blocked": blocked, "blockers": blockers, "events": events}


_PGSS_HELP = (
    "pg_stat_statements is not installed in this database. To enable it (needs a superuser, "
    "stackdoctor will not do it for you): 1) add `shared_preload_libraries = 'pg_stat_statements'` "
    "to postgresql.conf (or `-c shared_preload_libraries=pg_stat_statements` in docker), "
    "2) restart Postgres, 3) run `CREATE EXTENSION pg_stat_statements;` in this database. "
    "On RDS/Cloud SQL, enable it via the parameter group / database flags."
)


def slow_queries(limit: int = 10) -> dict:
    if (s := _not_configured()):
        return s
    limit = max(1, min(int(limit), 50))
    with connect() as conn:
        has_ext = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone()
        seq = conn.execute(
            """
            SELECT schemaname || '.' || relname AS table, seq_scan, seq_tup_read,
                   idx_scan, n_live_tup
            FROM pg_stat_user_tables
            WHERE seq_scan > 0
            ORDER BY seq_tup_read DESC LIMIT 5
            """
        ).fetchall()
        result: dict = {"seq_scan_heavy_tables": seq}
        if not has_ext:
            result.update(available=False, message=_PGSS_HELP)
            return result
        new_cols = conn.info.server_version >= 130000
        mean, total = ("mean_exec_time", "total_exec_time") if new_cols else ("mean_time", "total_time")
        try:
            rows = conn.execute(
                f"""
                SELECT queryid, calls, round({mean}::numeric, 1) AS mean_ms,
                       round({total}::numeric, 1) AS total_ms, rows,
                       shared_blks_read, query
                FROM pg_stat_statements
                WHERE query NOT ILIKE '%%pg_stat_statements%%'
                ORDER BY {mean} DESC LIMIT %s
                """,
                (limit,),
            ).fetchall()
        except psycopg.errors.ObjectNotInPrerequisiteState:
            result.update(available=False, message=_PGSS_HELP)
            return result
    events = []
    for r in rows:
        r["query"] = preview(r["query"], 300)
        if r["mean_ms"] >= 1000:
            events.append(event(None, SOURCE, "slow_statement",
                                f"{r['calls']} call(s) averaging {r['mean_ms']:.0f}ms: {r['query']}",
                                "warning", queryid=r["queryid"]))
    result.update(available=True, queries=rows, events=events,
                  note="pg_stat_statements is cumulative since the last reset; it has no timestamps.")
    return result


def run_select(sql: str, limit: int = 100) -> dict:
    if (s := _not_configured()):
        return s
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        final_sql = validate_select(sql, limit)
    except UnsafeError as e:
        return {"rejected": True, "reason": str(e)}
    with connect() as conn:
        cur = conn.execute(final_sql)
        rows = cur.fetchmany(limit) if cur.description else []
        columns = [d.name for d in cur.description or []]
    return {"columns": columns, "row_count": len(rows), "limit": limit, "rows": rows,
            "executed_at": utcnow()}
