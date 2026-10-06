"""Redis checks. Every command goes through the read-only allowlist."""

from __future__ import annotations

from functools import lru_cache

import redis as redis_lib

from ..config import get_config, skipped
from ..safety import UnsafeError, check_redis_command, preview
from . import event

SOURCE = "redis"


class SafeRedis:
    """A Redis client that can only run allowlisted read-only commands."""

    def __init__(self, url: str):
        self._r = redis_lib.Redis.from_url(
            url, decode_responses=True, encoding_errors="replace",
            socket_timeout=5, socket_connect_timeout=3)

    def cmd(self, *args):
        check_redis_command(*args)
        return self._r.execute_command(*args)

    def scan_iter(self, match: str, limit: int, count: int = 500, max_rounds: int = 200):
        """SCAN (never KEYS) until `limit` keys or `max_rounds` round trips."""
        cursor, found, rounds = 0, [], 0
        while True:
            cursor, keys = self.cmd("SCAN", cursor, "MATCH", match, "COUNT", count)
            found.extend(keys)
            rounds += 1
            if int(cursor) == 0 or len(found) >= limit or rounds >= max_rounds:
                return found[:limit], int(cursor) == 0


@lru_cache(maxsize=4)
def client(url: str) -> SafeRedis:
    return SafeRedis(url)


def glob_escape(text: str) -> str:
    return "".join("\\" + c if c in "*?[]\\" else c for c in text)


def memory_and_clients() -> dict:
    cfg = get_config()
    if not cfg.redis_url:
        return skipped("REDIS_URL not set")
    r = client(cfg.redis_url)
    info = r.cmd("INFO")
    used, maxmem = info.get("used_memory", 0), info.get("maxmemory", 0)
    try:
        maxclients = int(r.cmd("CONFIG GET", "maxclients").get("maxclients", 0))
    except redis_lib.ResponseError:  # CONFIG is often disabled on managed Redis
        maxclients = None
    pct = round(used * 100 / maxmem, 1) if maxmem else None
    result = {
        "redis_version": info.get("redis_version"),
        "uptime_s": info.get("uptime_in_seconds"),
        "memory": {
            "used": info.get("used_memory_human"), "used_bytes": used,
            "peak": info.get("used_memory_peak_human"),
            "maxmemory_bytes": maxmem or None, "used_pct_of_max": pct,
            "maxmemory_policy": info.get("maxmemory_policy"),
            "fragmentation_ratio": info.get("mem_fragmentation_ratio"),
        },
        "clients": {
            "connected": info.get("connected_clients"), "blocked": info.get("blocked_clients"),
            "maxclients": maxclients, "rejected_connections": info.get("rejected_connections"),
        },
        "stats": {k: info.get(k) for k in ("evicted_keys", "expired_keys", "keyspace_hits",
                                           "keyspace_misses", "instantaneous_ops_per_sec")},
        "keyspace": {k: v for k, v in info.items() if k.startswith("db") and isinstance(v, dict)},
    }

    events = []
    if pct is not None and pct >= cfg.redis_mem_warn_pct:
        events.append(event(None, SOURCE, "memory_high",
                            f"Redis memory at {pct}% of maxmemory ({info.get('used_memory_human')}, "
                            f"policy {info.get('maxmemory_policy')})", "critical" if pct >= 95 else "warning"))
    if info.get("evicted_keys"):
        events.append(event(None, SOURCE, "evictions",
                            f"{info['evicted_keys']} keys evicted since start (policy {info.get('maxmemory_policy')})",
                            "warning"))
    if info.get("rejected_connections"):
        events.append(event(None, SOURCE, "rejected_connections",
                            f"{info['rejected_connections']} connections rejected (maxclients {maxclients})",
                            "warning"))
    try:
        slow = r.cmd("SLOWLOG GET", 5)
    except redis_lib.ResponseError:
        slow = []
    result["slowlog"] = [{"ts": s.get("start_time"), "duration_us": s.get("duration"),
                          "command": preview(s.get("command"), 120)} for s in slow]
    for s in result["slowlog"]:
        if (s["duration_us"] or 0) >= 100_000:
            events.append(event(s["ts"], SOURCE, "slow_command",
                                f"slow Redis command {s['duration_us'] / 1000:.0f}ms: {s['command']}", "info"))
    result["events"] = events
    return result


def scan_keys(pattern: str = "*", limit: int = 50) -> dict:
    cfg = get_config()
    if not cfg.redis_url:
        return skipped("REDIS_URL not set")
    limit = max(1, min(int(limit), 500))
    keys, complete = client(cfg.redis_url).scan_iter(pattern or "*", limit)
    return {"pattern": pattern, "count": len(keys), "keys": sorted(keys),
            "scan_complete": complete,
            "note": None if complete else "Stopped early (limit reached); more keys may match."}


def key_info(key: str) -> dict:
    cfg = get_config()
    if not cfg.redis_url:
        return skipped("REDIS_URL not set")
    return describe_key(client(cfg.redis_url), key)


def describe_key(r: SafeRedis, key: str) -> dict:
    ktype = r.cmd("TYPE", key)
    if ktype == "none":
        return {"key": key, "exists": False}
    out = {"key": key, "exists": True, "type": ktype, "ttl_ms": r.cmd("PTTL", key)}
    for name, args in (("memory_bytes", ("MEMORY USAGE", key)), ("encoding", ("OBJECT ENCODING", key))):
        try:
            out[name] = r.cmd(*args)
        except (redis_lib.ResponseError, UnsafeError):
            pass
    if ktype == "string":
        out["length"] = r.cmd("STRLEN", key)
        out["preview"] = preview(r.cmd("GETRANGE", key, 0, 300), 200)
    elif ktype == "list":
        out["length"] = r.cmd("LLEN", key)
        out["preview"] = [preview(v, 200) for v in r.cmd("LRANGE", key, 0, 2)]
    elif ktype == "hash":
        out["length"] = r.cmd("HLEN", key)
        _, items = r.cmd("HSCAN", key, 0, "COUNT", 10)
        out["preview"] = {preview(k, 60): preview(v, 120) for k, v in list(items.items())[:10]}
    elif ktype == "set":
        out["length"] = r.cmd("SCARD", key)
        _, members = r.cmd("SSCAN", key, 0, "COUNT", 10)
        out["preview"] = [preview(m, 120) for m in members[:10]]
    elif ktype == "zset":
        out["length"] = r.cmd("ZCARD", key)
        flat = r.cmd("ZRANGE", key, 0, 4, "WITHSCORES")
        out["preview"] = [[preview(m, 120), s] for m, s in zip(flat[::2], flat[1::2])]
    elif ktype == "stream":
        out["length"] = r.cmd("XLEN", key)
    return out
