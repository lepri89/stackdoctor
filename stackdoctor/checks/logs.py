"""Log checks for files and docker containers listed in LOG_SOURCES.

Only configured sources can be read, so the AI can't use this to read arbitrary files.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import subprocess
from collections import defaultdict

from ..config import get_config, skipped
from . import event, to_utc, utcnow

SOURCE = "logs"
MAX_LINE = 2000
MAX_FILE_BYTES = 5_000_000

_DOCKER_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z\s?")
_GENERIC_TS = re.compile(
    r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(?:[.,](\d+))?\s*(Z|UTC|[+-]\d\d:?\d\d)?")

# (kind, pattern, severity) — first match wins, so specific rules come first.
RULES = [
    ("worker_lost", r"WorkerLostError|worker lost|exited with 'signal 9|exited prematurely", "critical"),
    ("worker_shutdown", r"\b(?:warm|cold) shutdown\b", "warning"),  # Celery: "worker: Warm shutdown (MainProcess)"
    ("task_timeout", r"SoftTimeLimitExceeded|TimeLimitExceeded|(?:hard|soft) time limit", "warning"),
    ("task_failed", r"Task [\w.]+\[[\w-]+\] raised unexpected|Task [\w.]+\[[\w-]+\] failed", "warning"),
    ("db_lock_wait", r"still waiting for \w*Lock", "warning"),
    ("db_lock_error", r"lock timeout|deadlock detected|could not obtain lock|LockNotAvailable", "warning"),
    ("db_timeout", r"statement timeout|QueryCanceled|canceling statement", "warning"),
    ("db_connection_error", r"could not connect to server|connection to server .* failed|too many clients|"
                            r"remaining connection slots|server closed the connection", "warning"),
    ("redis_error", r"OOM command not allowed|MISCONF|Error \d+ connecting to|redis\.exceptions\.\w*Error", "warning"),
    ("timeout", r"\b504\b|Gateway Time-?out|ReadTimeout|timed out", "warning"),
    ("error", r"\b(?:ERROR|CRITICAL|FATAL|PANIC)\b|Traceback \(most recent call last\)", "warning"),
]
_RULES = [(k, re.compile(p, re.IGNORECASE), s) for k, p, s in RULES]
PROBLEM_KINDS = {k for k, _, _ in RULES}


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _parse_source(spec: str) -> tuple[str, str]:
    """'docker:api' / 'file:/x.log' / '/var/log/x.log' / 'api' -> (kind, target)."""
    if spec.startswith("docker:"):
        return "docker", spec[7:]
    if spec.startswith("file:"):
        return "file", spec[5:]
    looks_like_path = any(c in spec for c in "/\\") or spec.endswith((".log", ".txt")) or os.path.exists(spec)
    return ("file", os.path.expanduser(spec)) if looks_like_path else ("docker", spec)


def _label(spec: str) -> str:
    kind, target = _parse_source(spec)
    return os.path.basename(target) if kind == "file" else f"docker:{target}"


def _resolve(source: str) -> tuple[str, str, str] | None:
    """Match a user-provided name against configured sources. Returns (label, kind, target)."""
    for spec in get_config().log_sources:
        kind, target = _parse_source(spec)
        if source in (spec, target, os.path.basename(target)):
            return spec, kind, target
    return None


def _parse_ts(line: str) -> tuple[dt.datetime | None, str]:
    """Return (timestamp, line without docker's timestamp prefix)."""
    m = _DOCKER_TS.match(line)
    if m:
        frac = (m.group(2) or "0")[:6].ljust(6, "0")
        ts = dt.datetime.fromisoformat(f"{m.group(1)}.{frac}+00:00")
        return ts, line[m.end():]
    m = _GENERIC_TS.search(line[:80])
    if m:
        date, clock, frac, tz = m.groups()
        frac = (frac or "0")[:6].ljust(6, "0")
        tz = "+00:00" if tz in ("Z", "UTC") else (tz if tz and ":" in tz else (f"{tz[:3]}:{tz[3:]}" if tz else ""))
        try:
            return to_utc(dt.datetime.fromisoformat(f"{date}T{clock}.{frac}{tz}")), line
        except ValueError:
            pass
    return None, line


def _read_file(path: str, max_lines: int) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - MAX_FILE_BYTES)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0:
        lines = lines[1:]  # first line is probably partial
    return lines[-max_lines:]


def _read_docker(container: str, max_lines: int, since_minutes: float | None) -> list[str]:
    if not shutil.which("docker"):
        raise RuntimeError("docker CLI not found on PATH")
    cmd = ["docker", "logs", "--timestamps", "--tail", str(max_lines)]
    if since_minutes:
        cmd += ["--since", f"{int(since_minutes * 60)}s"]
    proc = subprocess.run(cmd + [container], capture_output=True, timeout=6,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300] or f"docker logs exited {proc.returncode}")
    # Container stdout and stderr arrive separately; timestamps let us re-interleave them.
    return sorted(proc.stdout.splitlines() + proc.stderr.splitlines())


def _read(kind: str, target: str, max_lines: int, since_minutes: float | None = None) -> list[dict]:
    raw = _read_file(target, max_lines) if kind == "file" else _read_docker(target, max_lines, since_minutes)
    out, last_ts, trailing = [], None, []
    for line in raw:
        ts, text = _parse_ts(line)
        if ts:
            last_ts, trailing = ts, []
        entry = {"ts": ts or last_ts, "line": text[:MAX_LINE]}  # tracebacks inherit the last timestamp
        if not ts:
            trailing.append(entry)
        out.append(entry)
    if kind == "file" and trailing:
        # Lines after the last timestamp were written by the file's last modification at the latest.
        # This dates lines printed without a timestamp, like Celery's "worker: Warm shutdown (MainProcess)",
        # which Celery writes straight to stdout (it never goes through logging or --logfile).
        mtime = to_utc(os.path.getmtime(target))
        for entry in trailing:
            entry["ts"] = max(entry["ts"], mtime) if entry["ts"] else mtime
    return out


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def tail_logs(source: str, lines: int = 100) -> dict:
    cfg = get_config()
    if not cfg.log_sources:
        return skipped("LOG_SOURCES not set")
    resolved = _resolve(source)
    if not resolved:
        return {"error": f"Unknown log source '{source}'.", "configured_sources": cfg.log_sources}
    label, kind, target = resolved
    entries = _read(kind, target, max(1, min(int(lines), 500)))
    return {"source": label, "count": len(entries), "lines": entries}


def search_logs(pattern: str, since_minutes: int = 30, source: str | None = None) -> dict:
    cfg = get_config()
    if not cfg.log_sources:
        return skipped("LOG_SOURCES not set")
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(re.escape(pattern), re.IGNORECASE)
    since = utcnow() - dt.timedelta(minutes=max(1, min(int(since_minutes), 24 * 60)))
    targets = [_resolve(source)] if source else [(s, *_parse_source(s)) for s in cfg.log_sources]
    if source and not targets[0]:
        return {"error": f"Unknown log source '{source}'.", "configured_sources": cfg.log_sources}

    matches, errors = [], {}
    for label, kind, target in targets:
        try:
            entries = _read(kind, target, 20_000, since_minutes)
        except Exception as e:
            errors[label] = f"{type(e).__name__}: {e}"
            continue
        for e in entries:
            if (e["ts"] is None or e["ts"] >= since) and regex.search(e["line"]):
                matches.append({"source": label, **e})
    matches.sort(key=lambda m: m["ts"] or since)
    return {"pattern": pattern, "since": since, "match_count": len(matches),
            "matches": matches[-200:], "errors": errors or None}


def classify(line: str) -> tuple[str, str] | None:
    for kind, rx, severity in _RULES:
        if rx.search(line):
            return kind, severity
    return None


def recent_problems(since_minutes: int = 30) -> dict:
    """Classify recent log lines into problem kinds, aggregated per source and kind."""
    cfg = get_config()
    if not cfg.log_sources:
        return skipped("LOG_SOURCES not set")
    now = utcnow()
    since = now - dt.timedelta(minutes=since_minutes)
    spike_since = now - dt.timedelta(minutes=5)
    groups: dict[tuple[str, str], dict] = {}
    recent_errors: dict[str, list] = defaultdict(list)
    errors, last_activity = {}, {}
    for spec in cfg.log_sources:
        kind, target = _parse_source(spec)
        try:
            entries = _read(kind, target, 20_000, since_minutes)
        except Exception as e:
            errors[spec] = f"{type(e).__name__}: {e}"
            continue
        last_activity[_label(spec)] = max((e["ts"] for e in entries if e["ts"]), default=None)
        for e in entries:
            if e["ts"] and e["ts"] < since:
                continue
            hit = classify(e["line"])
            if not hit:
                continue
            k, sev = hit
            g = groups.setdefault((spec, k), {"source": spec, "kind": k, "severity": sev, "count": 0,
                                              "first": e["ts"], "last": e["ts"], "sample": e["line"][:300]})
            g["count"] += 1
            g["last"] = e["ts"] or g["last"]
            if e["ts"] and e["ts"] >= spike_since:
                recent_errors[spec].append(e["ts"])

    events = []
    for g in groups.values():
        events.append(event(g["first"], SOURCE, g["kind"],
                            f"[{_label(g['source'])}] {g['count']}x {g['kind']} (last {g['last'] and g['last'].isoformat(timespec='seconds')}): {g['sample']}",
                            g["severity"], log_source=g["source"], count=g["count"], last=g["last"]))
    for spec, stamps in recent_errors.items():
        if len(stamps) >= cfg.log_error_spike:
            events.append(event(min(stamps), SOURCE, "error_spike",
                                f"[{_label(spec)}] {len(stamps)} problem lines in the last 5 minutes "
                                f"(threshold {cfg.log_error_spike})", "warning", log_source=spec))
    summary = sorted(groups.values(), key=lambda g: (g["first"] or now))
    return {"since_minutes": since_minutes, "problems": summary, "last_activity": last_activity,
            "errors": errors or None, "events": events}
