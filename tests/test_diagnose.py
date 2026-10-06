import datetime as dt

import pytest

from stackdoctor import diagnose as diag
from stackdoctor.checks import event
from stackdoctor.config import Config


def at(minutes_ago: float) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)


def test_select_checks_routes_and_defaults():
    assert diag.select_checks("") == sorted(diag.CHECKS)
    jobs = diag.select_checks("why are my jobs stuck?")
    assert "celery.workers" in jobs and "postgres.blocking_locks" in jobs
    assert "postgres.slow_queries" not in jobs
    assert "postgres.slow_queries" in diag.select_checks("API is slow")


def test_worker_down_chain():
    events = [
        event(at(10), "logs", "worker_shutdown", "worker: Warm shutdown"),
        event(at(0), "celery", "no_workers", "No workers", "critical"),
        event(at(0), "celery", "queue_no_consumer", "Queue 'celery' has 40 messages", "critical"),
    ]
    chains = diag.find_chains(events, 30)
    assert chains[0]["label"] == "possible cause"
    assert "worker" in chains[0]["hypothesis"].lower()
    assert "worker_shutdown" in chains[0]["cause"]
    assert any("queue_no_consumer" in e for e in chains[0]["effects"])
    assert chains[0]["confidence"] in ("low", "medium")
    assert "caveat" in chains[0]


def test_lock_chain_with_ongoing_cause_across_sources():
    events = [
        event(at(120), "postgres", "lock_held", "pid 1 holds lock", "critical"),  # long ago but ongoing
        event(at(5), "postgres", "query_blocked", "pid 2 waiting"),
        event(at(4), "celery", "task_long_running", "task running 240s"),
        event(at(1), "logs", "db_lock_error", "lock timeout"),
    ]
    chains = diag.find_chains(events, 30)
    lock = next(c for c in chains if "lock" in c["hypothesis"].lower())
    assert lock["confidence"] == "medium"
    assert len(lock["effects"]) == 3


def test_effects_before_cause_are_ignored():
    events = [
        event(at(30), "logs", "task_failed", "failed"),
        event(at(5), "logs", "db_connection_error", "could not connect"),
    ]
    assert diag.find_chains(events, 30) == []


def test_non_ongoing_cause_outside_window_ignored():
    events = [
        event(at(120), "logs", "worker_shutdown", "Warm shutdown"),
        event(at(1), "logs", "timeout", "504"),
    ]
    assert diag.find_chains(events, 30) == []


async def test_diagnose_runs_concurrently_with_timeouts_and_errors(monkeypatch):
    import time

    def slow():
        time.sleep(3)
        return {}

    def boom():
        raise RuntimeError("connection refused to redis://:pw@x")

    def ok():
        return {"events": [event(None, "celery", "no_workers", "No workers", "critical")]}

    monkeypatch.setattr(diag, "CHECKS", {"a.slow": (slow, "default"), "b.boom": (boom, "default"),
                                         "c.ok": (ok, "default"), "d.skip": (lambda: {"skipped": "x"}, "default")})
    monkeypatch.setattr(diag, "get_config", lambda: Config(check_timeout_s=0.5))
    t0 = time.perf_counter()
    out = await diag.diagnose("")
    assert time.perf_counter() - t0 < 2
    assert out["checks_run"] == {"a.slow": "timeout", "b.boom": "error", "c.ok": "ok", "d.skip": "skipped"}
    assert out["findings"][0]["kind"] == "no_workers"
    assert out["timeline"][0]["kind"] == "no_workers"
    assert out["skipped"] == {"d.skip": "x"}


def test_failed_tasks_fallback_note():
    results = {"celery.failed_tasks": {"result": {"visible": False, "reason": "Not visible because X."}}}
    events = [event(at(1), "logs", "task_failed", "Task x raised unexpected")]
    _, notes = diag._findings(events, results)
    assert "Falling back to logs: 1" in notes[0]


@pytest.mark.parametrize("consumers,expected", [(set(), 1), ({"celery"}, 0)])
def test_queue_cross_check(consumers, expected):
    results = {
        "celery.workers": {"result": {"alive_count": 1, "workers": [{"queues": sorted(consumers)}]}},
        "celery.queue_lengths": {"result": {"queues": {"celery": {"messages": 5}}}},
    }
    assert len(diag._cross_check_queues(results)) == expected


def test_note_when_workers_missing_without_shutdown_line():
    results = {"logs.recent_problems": {"result": {"last_activity": {"worker.log": at(3)}}}}
    _, notes = diag._findings([event(at(0), "celery", "no_workers", "none", "critical")], results)
    assert "stdout" in notes[0] and "worker.log last written" in notes[0]
    _, notes = diag._findings([event(at(0), "celery", "no_workers", "none", "critical"),
                               event(at(2), "logs", "worker_shutdown", "Warm shutdown")], results)
    assert notes == []
