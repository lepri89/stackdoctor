import datetime as dt

from stackdoctor.checks import logs
from stackdoctor.config import Config


def test_parse_docker_timestamp():
    ts, text = logs._parse_ts("2026-10-06T21:39:03.123456789Z [INFO] hello")
    assert ts == dt.datetime(2026, 10, 6, 21, 39, 3, 123456, tzinfo=dt.timezone.utc)
    assert text == "[INFO] hello"


def test_parse_celery_and_postgres_timestamps():
    ts, _ = logs._parse_ts("[2026-10-06 21:39:03,500: ERROR/MainProcess] boom")
    assert ts is not None and ts.tzinfo is not None
    ts, _ = logs._parse_ts("2026-10-06 21:39:03.123 UTC [77] ERROR:  canceling statement due to lock timeout")
    assert ts == dt.datetime(2026, 10, 6, 21, 39, 3, 123000, tzinfo=dt.timezone.utc)
    ts, _ = logs._parse_ts("2026-10-06T23:39:03+02:00 something")
    assert ts == dt.datetime(2026, 10, 6, 21, 39, 3, tzinfo=dt.timezone.utc)
    assert logs._parse_ts("no timestamp here")[0] is None


def test_classify():
    assert logs.classify("worker: Warm shutdown (MainProcess)")[0] == "worker_shutdown"
    assert logs.classify("ERROR:  canceling statement due to lock timeout")[0] == "db_lock_error"
    assert logs.classify("Task app.tasks.x[1-2-3] raised unexpected: OperationalError()")[0] == "task_failed"
    assert logs.classify("billiard.exceptions.WorkerLostError: Worker exited")[0] == "worker_lost"
    assert logs.classify("redis.exceptions.ResponseError: OOM command not allowed")[0] == "redis_error"
    assert logs.classify("LOG:  process 81 still waiting for RowExclusiveLock on relation 16385")[0] == "db_lock_wait"
    assert logs.classify("Task t.x[ab-1] raised unexpected: LockNotAvailable('lock timeout')")[0] == "task_failed"
    assert logs.classify("2026-10-06 21:00:00.000 UTC [1] LOG:  shutting down") is None  # postgres, not a worker
    assert logs.classify("INFO all good") is None


def test_only_configured_sources_can_be_read(monkeypatch, tmp_path):
    f = tmp_path / "app.log"
    f.write_text("2026-10-06 21:00:00 ERROR one\n2026-10-06 21:00:01 INFO two\n")
    monkeypatch.setattr(logs, "get_config", lambda: Config(log_sources=[str(f)]))
    assert logs.tail_logs("/etc/passwd")["error"].startswith("Unknown log source")
    out = logs.tail_logs("app.log", 1)
    assert out["count"] == 1 and "two" in out["lines"][0]["line"]


def test_recent_problems_aggregates_and_spikes(monkeypatch, tmp_path):
    now = dt.datetime.now(dt.timezone.utc)
    lines = [f"{(now - dt.timedelta(seconds=i)).isoformat()} ERROR failure {i}" for i in range(12, 0, -1)]
    lines.append("Traceback continuation without timestamp")
    f = tmp_path / "w.log"
    f.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(logs, "get_config", lambda: Config(log_sources=[str(f)], log_error_spike=10))
    out = logs.recent_problems(30)
    kinds = {e["kind"] for e in out["events"]}
    assert kinds == {"error", "error_spike"}
    assert out["problems"][0]["count"] == 12


def test_search_logs_regex_and_literal_fallback(monkeypatch, tmp_path):
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    f = tmp_path / "a.log"
    f.write_text(f"{now} lock timeout on orders\n{now} fine [x\n")
    monkeypatch.setattr(logs, "get_config", lambda: Config(log_sources=[str(f)]))
    assert logs.search_logs("lock (timeout|wait)")["match_count"] == 1
    assert logs.search_logs("[x")["match_count"] == 1  # invalid regex -> literal


def test_shutdown_line_without_timestamp_gets_file_mtime(monkeypatch, tmp_path):
    """Celery writes 'worker: Warm shutdown' to stdout with no timestamp; when stdout is
    redirected to a file, the line must still land on the timeline at the right time."""
    import os

    now = dt.datetime.now(dt.timezone.utc)
    f = tmp_path / "worker.log"
    local_then = dt.datetime.now() - dt.timedelta(minutes=20)  # Celery log times are naive local time
    f.write_text(f"[{local_then.strftime('%Y-%m-%d %H:%M:%S')},000: INFO/MainProcess] "
                 "Task t.x[1] succeeded\n\nworker: Warm shutdown (MainProcess)\n")
    shutdown_at = (now - dt.timedelta(minutes=2)).timestamp()
    os.utime(f, (shutdown_at, shutdown_at))
    monkeypatch.setattr(logs, "get_config", lambda: Config(log_sources=[str(f)]))
    out = logs.recent_problems(30)
    ev = next(e for e in out["events"] if e["kind"] == "worker_shutdown")
    assert abs(ev["ts"].timestamp() - shutdown_at) < 1
    assert out["last_activity"]["worker.log"] == ev["ts"]


def test_shutdown_line_from_docker_logs(monkeypatch):
    """docker logs captures stdout and prefixes every line with a timestamp."""
    lines = ["2026-10-06T21:00:00.000000000Z [2026-10-06 21:00:00,000: INFO/MainProcess] celery@w ready.",
             "2026-10-06T21:05:00.123456789Z worker: Warm shutdown (MainProcess)"]
    monkeypatch.setattr(logs, "_read_docker", lambda *a: lines)
    entries = logs._read("docker", "worker", 100)
    assert entries[-1]["ts"] == dt.datetime(2026, 10, 6, 21, 5, 0, 123456, tzinfo=dt.timezone.utc)
    assert logs.classify(entries[-1]["line"])[0] == "worker_shutdown"
