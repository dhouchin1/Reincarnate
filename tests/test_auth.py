import json
import subprocess

import pytest

from reincarnate import auth


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    auth.reset()
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/fake/claude")
    yield
    auth.reset()


def fake_run(monkeypatch, stdout="", rc=0, stderr="", raises=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)
    monkeypatch.setattr(auth.subprocess, "run", run)
    return calls


def test_logged_in(monkeypatch):
    calls = fake_run(monkeypatch, json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "email": "me@x.io", "orgId": "secret"}))
    assert auth.auth_status() == {"logged_in": True, "method": "claude.ai", "email": "me@x.io"}
    argv, kw = calls[0]
    assert argv == ["/fake/claude", "auth", "status", "--json"] and kw["timeout"] == 10


def test_not_logged_in_even_with_nonzero_exit(monkeypatch):
    fake_run(monkeypatch, json.dumps({"loggedIn": False}), rc=1)
    out = auth.auth_status()
    assert out["logged_in"] is False and out["error"]


def test_timeout_and_missing_binary_never_raise(monkeypatch):
    fake_run(monkeypatch, raises=subprocess.TimeoutExpired("claude", 10))
    assert "timed out" in auth.auth_status()["error"]
    auth.reset()
    fake_run(monkeypatch, raises=FileNotFoundError("no claude"))
    assert auth.auth_status()["logged_in"] is False
    auth.reset()
    fake_run(monkeypatch, "not json", rc=2, stderr="boom\nmore")
    assert auth.auth_status() == {"logged_in": False, "error": "boom"}


def test_result_is_cached_for_a_minute(monkeypatch):
    calls = fake_run(monkeypatch, json.dumps({"loggedIn": True}))
    auth.auth_status()
    auth.auth_status()
    assert len(calls) == 1
    clock = [auth.time.monotonic() + 61]
    monkeypatch.setattr(auth.time, "monotonic", lambda: clock[0])
    auth.auth_status()
    assert len(calls) == 2
