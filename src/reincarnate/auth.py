"""Is `claude` logged in? One cheap `claude auth status --json`, cached for a minute."""

from __future__ import annotations

import json
import subprocess
import threading
import time

TIMEOUT = 10
CACHE_SECONDS = 60
_lock = threading.Lock()
_cache: tuple[float, dict] | None = None


def reset() -> None:
    global _cache
    with _lock:
        _cache = None


def _probe() -> dict:
    from . import claude_runner

    argv = [claude_runner.claude_argv(model="x", max_turns=1)[0], "auth", "status", "--json"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return {"logged_in": False, "error": f"claude auth status timed out after {TIMEOUT}s"}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"logged_in": False, "error": f"could not run claude: {exc}"}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict):
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        return {"logged_in": False,
                "error": why[0][:200] if why else f"claude auth status exited {proc.returncode}"}
    if data.get("loggedIn", data.get("logged_in")) is True:
        return {"logged_in": True,
                "method": str(data.get("authMethod") or data.get("method") or ""),
                "email": str(data.get("email") or "")}
    return {"logged_in": False, "error": str(data.get("error") or "not logged in")}


def auth_status() -> dict:
    """{logged_in, method, email} or {logged_in: False, error}. Never raises."""
    global _cache
    with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return dict(_cache[1])
    try:
        out = _probe()
    except Exception as exc:  # noqa: BLE001 - a status check must never break the page
        out = {"logged_in": False, "error": f"{type(exc).__name__}: {exc}"}
    with _lock:
        _cache = (time.monotonic(), out)
    return dict(out)


_refreshing = False


def cached_status() -> dict:
    """The last known status without waiting: a stale or missing cache starts one background
    probe and returns {"checking": True} until it lands, so the page never stalls on it."""
    global _refreshing
    with _lock:
        fresh = _cache and time.monotonic() - _cache[0] < CACHE_SECONDS
        if fresh:
            return dict(_cache[1])
        last = dict(_cache[1]) if _cache else {"logged_in": None, "checking": True}
        start = not _refreshing
        _refreshing = True

    def run() -> None:
        global _refreshing
        try:
            auth_status()
        finally:
            with _lock:
                _refreshing = False

    if start:
        threading.Thread(target=run, name="reincarnate-auth", daemon=True).start()
    return last
