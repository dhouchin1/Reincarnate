import json

from reincarnate import claude_runner as cr


def test_argv(monkeypatch):
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/x/claude")
    argv = cr.claude_argv("hi", model="m", max_turns=3, tools="Read", disallowed="Bash")
    assert argv[0] == "/x/claude"
    assert argv[argv.index("-p") + 1] == "hi"
    for flag, val in (("--output-format", "json"), ("--model", "m"), ("--max-turns", "3"),
                      ("--tools", "Read"), ("--disallowedTools", "Bash")):
        assert argv[argv.index(flag) + 1] == val


def test_argv_default_binary(monkeypatch):
    monkeypatch.delenv("REINCARNATE_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/usr/bin/claude")
    assert cr.claude_argv("p", model="m", max_turns=1)[0] == "/usr/bin/claude"


def test_parse_single_object():
    out = json.dumps({"type": "result", "result": "hello", "total_cost_usd": 0.12}).encode()
    text, cost, raw = cr.parse_output(out)
    assert (text, cost) == ("hello", 0.12)
    assert raw["type"] == "result"


def test_parse_event_array():
    events = [{"type": "system"}, {"type": "result", "result": "old", "total_cost_usd": 1},
              {"type": "assistant"}, {"type": "result", "result": "final", "total_cost_usd": 0.5}]
    text, cost, _ = cr.parse_output(json.dumps(events).encode())
    assert (text, cost) == ("final", 0.5)


def test_parse_bare_text_and_empty():
    assert cr.parse_output(b"just words") == ("just words", 0.0, {})
    assert cr.parse_output(b"") == ("", 0.0, {})


def test_run_nonzero_rc_does_not_raise(monkeypatch):
    seen = {}

    def fake(argv, cwd, env, timeout):
        seen.update(argv=argv, env=env)
        return 2, b"oops", b"bad"

    monkeypatch.setattr(cr, "_invoke", fake)
    res = cr.run("p", env_extra={"FOO": "1"})
    assert res.rc == 2 and res.text == "oops" and res.stderr == "bad"
    assert seen["env"]["FOO"] == "1"


def test_run_timeout(monkeypatch):
    import subprocess

    def fake(argv, cwd, env, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cr, "_invoke", fake)
    assert cr.run("p", timeout=1).rc == 124


def test_argv_is_isolated_by_default():
    """A donor repo's CLAUDE.md / .claude/settings.json must never load: the clone is cwd."""
    argv = cr.claude_argv("p", model="m", max_turns=1)
    i = argv.index("--setting-sources")
    assert argv[i + 1] == ""
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert "--setting-sources" not in cr.claude_argv(
        "p", model="m", max_turns=1, isolated=False)
