"""Celery checks without Flower: the inspect API plus reading the broker directly.

Only ping/active/reserved/active_queues/stats/query_task are used. Nothing here
sends, revokes, retries or shuts down tasks or workers.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import json
import os
import re
import sys
import threading
import time

from celery import Celery

from ..config import get_config, skipped
from ..safety import preview
from . import event, to_utc
from .redis import client as redis_client, glob_escape

SOURCE = "celery"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_META_PREFIX = "celery-task-meta-"
_HISTORY: dict[str, list[tuple[float, int]]] = {}  # queue -> recent (time, length) samples


_app = None
_app_lock = threading.Lock()  # diagnose() calls checks from several threads at once


def get_app():
    global _app
    with _app_lock:
        if _app is None:
            _app = _load_app()
        return _app


def _load_app():
    """Load CELERY_APP (`pkg.module:app` or `pkg.module`), else a bare app on the broker."""
    cfg = get_config()
    if cfg.celery_app:
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())
        mod_name, _, attr = cfg.celery_app.partition(":")
        # Importing user code must never print to stdout: that is the MCP channel.
        with contextlib.redirect_stdout(sys.stderr):
            module = importlib.import_module(mod_name)
        if attr:
            return getattr(module, attr)
        for name in ("app", "celery", "celery_app"):
            if hasattr(module, name):
                return getattr(module, name)
        raise ImportError(f"No Celery app found in {mod_name} (use module:attr)")
    if cfg.celery_broker_url:
        return Celery("stackdoctor", broker=cfg.celery_broker_url,
                      backend=cfg.celery_result_backend)
    return None


def _not_configured():
    cfg = get_config()
    if not (cfg.celery_app or cfg.celery_broker_url):
        return skipped("CELERY_APP / CELERY_BROKER_URL not set")
    return None


def _inspect(app, destination=None):
    timeout = get_config().celery_inspect_timeout_s
    return app.control.inspect(timeout=timeout, destination=destination,
                               limit=len(destination) if destination else None)


def _task_summary(t: dict) -> dict:
    started = t.get("time_start")
    return {
        "id": t.get("id"), "name": t.get("name"),
        "args": preview(t.get("args"), 120), "kwargs": preview(t.get("kwargs"), 120),
        "started": to_utc(started),
        "runtime_s": round(time.time() - started, 1) if started else None,
        "queue": (t.get("delivery_info") or {}).get("routing_key"),
    }


def workers() -> dict:
    if (s := _not_configured()):
        return s
    cfg, app = get_config(), get_app()
    ping = _inspect(app).ping() or {}
    events = []
    if not ping:
        events.append(event(None, SOURCE, "no_workers",
                            "No Celery workers replied to ping", "critical"))
        return {"alive_count": 0, "workers": [], "events": events}

    names = sorted(ping)
    insp = _inspect(app, destination=names)
    active, reserved = insp.active() or {}, insp.reserved() or {}
    queues, stats = insp.active_queues() or {}, insp.stats() or {}

    out = []
    for name in names:
        st = stats.get(name, {})
        concurrency = (st.get("pool") or {}).get("max-concurrency")
        act = [_task_summary(t) for t in active.get(name, [])]
        res = [_task_summary(t) for t in reserved.get(name, [])]
        out.append({
            "name": name, "alive": True,
            "queues": [q.get("name") for q in queues.get(name, [])],
            "concurrency": concurrency,
            "active_count": len(act), "reserved_count": len(res),
            "active": act[:20], "reserved": res[:10],
            "processed_total": sum((st.get("total") or {}).values()),
        })
        for t in act:
            if t["runtime_s"] and t["runtime_s"] >= cfg.long_task_s:
                events.append(event(t["started"], SOURCE, "task_long_running",
                                    f"{t['name']}[{t['id']}] running {t['runtime_s']}s on {name}",
                                    "warning", task_id=t["id"]))
        if concurrency and len(act) >= concurrency:
            events.append(event(None, SOURCE, "workers_saturated",
                                f"{name}: all {concurrency} slots busy, {len(res)} reserved", "warning"))

    if cfg.expected_workers and len(names) < cfg.expected_workers:
        events.append(event(None, SOURCE, "worker_missing",
                            f"Only {len(names)} of {cfg.expected_workers} expected workers replied",
                            "critical"))
    return {"alive_count": len(names), "workers": out, "events": events}


def _queue_names(app, given: list[str] | None) -> list[str]:
    if given:
        return given
    names = set(get_config().celery_queues)
    names.add(app.conf.task_default_queue or "celery")
    for q in app.conf.task_queues or []:
        names.add(getattr(q, "name", q))
    for route in (app.conf.task_routes or {}).values() if isinstance(app.conf.task_routes, dict) else []:
        if isinstance(route, dict) and route.get("queue"):
            names.add(route["queue"])
    return sorted(names)


def _redis_queue_lengths(app, url: str, names: list[str]) -> dict:
    opts = app.conf.broker_transport_options or {}
    steps = opts.get("priority_steps") or [0, 3, 6, 9]
    sep = opts.get("sep", "\x06\x16")
    prefix = opts.get("global_keyprefix", "")
    r = redis_client(url)
    result = {}
    for q in names:
        by_priority = {}
        for step in steps:
            key = prefix + (f"{q}{sep}{step}" if step else q)
            n = r.cmd("LLEN", key)
            if n:
                by_priority[str(step)] = n
        # Steps we don't know about (custom priority_steps on the producer side).
        extra, _ = r.scan_iter(glob_escape(prefix + q + sep) + "*", 50)
        for key in extra:
            step = key.rsplit(sep, 1)[-1]
            if step.isdigit() and int(step) not in steps:
                by_priority[step] = r.cmd("LLEN", key)
        result[q] = {"messages": sum(by_priority.values()),
                     "by_priority": by_priority if len(by_priority) > 1 else None}
    unacked = r.cmd("HLEN", prefix + "unacked")
    return {"queues": result, "unacked_total": unacked}


def _amqp_queue_lengths(app, names: list[str]) -> dict:
    result = {}
    for q in names:
        with app.connection_for_read() as conn:
            try:
                # passive=True only checks the queue; it never creates anything.
                _, messages, consumers = conn.default_channel.queue_declare(queue=q, passive=True)
                result[q] = {"messages": messages, "consumers": consumers}
            except conn.channel_errors:
                result[q] = {"error": "queue does not exist"}
    return {"queues": result}


def queue_lengths(queues: list[str] | None = None) -> dict:
    if (s := _not_configured()):
        return s
    cfg, app = get_config(), get_app()
    url = app.conf.broker_url or cfg.celery_broker_url or ""
    names = _queue_names(app, queues)
    if url.startswith(("redis://", "rediss://", "unix://")):
        out = _redis_queue_lengths(app, url, names)
    elif url.startswith(("amqp://", "amqps://", "pyamqp://")):
        out = _amqp_queue_lengths(app, names)
    else:
        return {"error": f"Queue lengths are supported for Redis and RabbitMQ brokers, not {url.split(':')[0]}"}

    now, events = time.time(), []
    for q, info in out["queues"].items():
        n = info.get("messages")
        if n is None:
            continue
        hist = _HISTORY.setdefault(q, [])
        hist.append((now, n))
        del hist[:-10]
        older = [(t, m) for t, m in hist if now - t >= 5]
        if older:
            t0, m0 = older[-1]
            info["change_since_last_check"] = {"seconds_ago": round(now - t0), "delta": n - m0}
        if n >= cfg.queue_threshold:
            events.append(event(None, SOURCE, "queue_backlog",
                                f"Queue '{q}' has {n} waiting messages (threshold {cfg.queue_threshold})",
                                "warning", queue=q, messages=n))
        if info.get("consumers") == 0 and n:
            events.append(event(None, SOURCE, "queue_no_consumer",
                                f"Queue '{q}' has {n} messages and no consumers", "critical", queue=q))
    out["events"] = events
    return out


def _backend(app) -> tuple[str | None, str | None]:
    """Return (redis_url, None) if results are readable, else (None, reason)."""
    cfg = get_config()
    url = cfg.celery_result_backend or app.conf.result_backend
    if not url:
        return None, "no result backend is configured (CELERY_RESULT_BACKEND / app.conf.result_backend)"
    if not str(url).startswith(("redis://", "rediss://")):
        return None, f"the result backend is '{str(url).split(':')[0]}'; stackdoctor only reads Redis result backends"
    return url, None


def failed_tasks(limit: int = 20) -> dict:
    if (s := _not_configured()):
        return s
    app = get_app()
    url, reason = _backend(app)
    if reason:
        return {"visible": False, "reason": f"Task failures are not visible because {reason}. "
                                             "Check logs for task errors instead."}
    r = redis_client(url)
    keys, complete = r.scan_iter(_META_PREFIX + "*", 2000)
    if not keys:
        why = ("task_ignore_result is on" if app.conf.task_ignore_result
               else f"no results are stored: results may have expired (result_expires="
                    f"{app.conf.result_expires}) or tasks use ignore_result=True")
        return {"visible": False, "reason": f"Task failures are not visible because {why}. "
                                             "Check logs for task errors instead."}
    failures = []
    for i in range(0, len(keys), 200):
        for raw in r.cmd("MGET", *keys[i:i + 200]):
            try:
                meta = json.loads(raw) if raw else None
            except ValueError:
                continue  # pickle or other serializer
            if meta and meta.get("status") == "FAILURE":
                failures.append(meta)
    failures.sort(key=lambda m: m.get("date_done") or "", reverse=True)
    events, out = [], []
    for m in failures[: max(1, min(int(limit), 100))]:
        res = m.get("result") or {}
        tb_tail = (m.get("traceback") or "").strip().splitlines()[-3:]
        item = {"task_id": m.get("task_id"), "name": m.get("name"),
                "date_done": m.get("date_done"),
                "exception": preview(f"{res.get('exc_type')}: {res.get('exc_message')}", 300)
                if isinstance(res, dict) else preview(res, 300),
                "traceback_tail": [preview(l, 200) for l in tb_tail]}
        out.append(item)
        events.append(event(_parse_iso(m.get("date_done")), SOURCE, "task_failed",
                            f"{item['name'] or 'task'}[{item['task_id']}] failed: {item['exception']}",
                            "warning", task_id=item["task_id"]))
    return {"visible": True, "results_scanned": len(keys), "scan_complete": complete,
            "failed_count": len(failures), "failed": out, "events": events}


def _parse_iso(value):
    try:
        return dt.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def task_details(task_id: str) -> dict:
    if (s := _not_configured()):
        return s
    if not _TASK_ID_RE.match(task_id or ""):
        return {"error": "Invalid task id"}
    app = get_app()
    out: dict = {"task_id": task_id}
    url, reason = _backend(app)
    if url:
        raw = redis_client(url).cmd("GET", _META_PREFIX + task_id)
        if raw:
            try:
                meta = json.loads(raw)
                out["result_backend"] = {
                    "status": meta.get("status"), "name": meta.get("name"),
                    "date_done": meta.get("date_done"), "retries": meta.get("retries"),
                    "worker": meta.get("worker"),
                    "args": preview(meta.get("args"), 200), "kwargs": preview(meta.get("kwargs"), 200),
                    "result": preview(meta.get("result"), 300),
                    "traceback_tail": [preview(l, 200) for l in
                                       (meta.get("traceback") or "").strip().splitlines()[-5:]],
                }
            except ValueError:
                out["result_backend"] = {"note": "result is not JSON-encoded"}
        else:
            out["result_backend"] = {"note": "no stored result (pending, expired, or ignore_result)"}
    else:
        out["result_backend"] = {"note": f"not readable: {reason}"}
    # Ask workers whether they currently hold the task (active/reserved/scheduled).
    found = _inspect(app).query_task(task_id) or {}
    out["on_workers"] = {w: {tid: {"state": state, "name": info.get("name"),
                                   "args": preview(info.get("args"), 120)}
                             for tid, (state, info) in tasks.items()}
                         for w, tasks in found.items() if tasks}
    return out
