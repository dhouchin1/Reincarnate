import json
import subprocess

import pytest

from reincarnate import claude_runner as cr


def test_argv(monkeypatch):
    monkeypatch.setenv("REINCARNATE_CLAUDE_BIN", "/x/claude")
    argv = cr.claude_argv(model="m", max_turns=3, tools="Read", disallowed="Bash")
    assert argv[0] == "/x/claude"
    # print mode with no positional prompt: the prompt is sent on stdin
    assert argv[argv.index("-p") + 1] == "--output-format"
    for flag, val in (("--output-format", "json"), ("--model", "m"), ("--max-turns", "3"),
                      ("--tools", "Read"), ("--disallowedTools", "Bash")):
        assert argv[argv.index(flag) + 1] == val


def test_argv_default_binary(monkeypatch):
    monkeypatch.delenv("REINCARNATE_CLAUDE_BIN", raising=False)
    monkeypatch.setattr(cr.shutil, "which", lambda n: "/usr/bin/claude")
    assert cr.claude_argv(model="m", max_turns=1)[0] == "/usr/bin/claude"


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

    def fake(argv, cwd, env, timeout, stdin):
        seen.update(argv=argv, env=env)
        return 2, b"oops", b"bad"

    monkeypatch.setattr(cr, "_invoke", fake)
    res = cr.run("p", env_extra={"FOO": "1"})
    assert res.rc == 2 and res.text == "oops" and res.stderr == "bad"
    assert res.failed and res.error_message() == "bad"
    assert seen["env"]["FOO"] == "1"


def test_run_timeout(monkeypatch):
    import subprocess

    def fake(argv, cwd, env, timeout, stdin):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cr, "_invoke", fake)
    res = cr.run("p", timeout=1)
    assert res.rc == 124 and res.failed
    assert res.error_message() == "claude timed out after 1s"


def test_argv_is_isolated_by_default():
    """A donor repo's CLAUDE.md / .claude/settings.json must never load: the clone is cwd."""
    argv = cr.claude_argv(model="m", max_turns=1)
    i = argv.index("--setting-sources")
    assert argv[i + 1] == ""
    assert "--strict-mcp-config" in argv and "--disable-slash-commands" in argv
    assert "--setting-sources" not in cr.claude_argv(model="m", max_turns=1, isolated=False)


# -- v0.5: failures, stdin, progress ----------------------------------------------------------
# Shapes captured live from claude 2.1.301 (2026-10-24).
BAD_MODEL = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
             "terminal_reason": "api_error", "total_cost_usd": 0,
             "result": "There's an issue with the selected model (not-a-real-model). It may not "
                       "exist or you may not have access to it."}
MAX_TURNS = {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "terminal_reason": "max_turns", "total_cost_usd": 0.0123,
             "errors": ["Reached maximum number of turns (1)"]}
NOT_LOGGED_IN = {"type": "result", "subtype": "success", "is_error": True,
                 "result": "Not logged in · Please run /login"}
SUCCESS = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
           "total_cost_usd": 0.0004}


def _capture(monkeypatch, rc=0, out=b"", err=b""):
    seen = {}

    def fake(argv, cwd, env, timeout, stdin):
        seen.update(argv=argv, stdin=stdin)
        return rc, out, err

    monkeypatch.setattr(cr, "_invoke", fake)
    return seen


def _shape(obj, as_array=False):
    return json.dumps([{"type": "system"}, obj] if as_array else obj).encode()


@pytest.mark.parametrize("as_array", [False, True])
def test_bad_model_404_fails_despite_subtype_success(monkeypatch, as_array):
    _capture(monkeypatch, rc=1, out=_shape(BAD_MODEL, as_array))
    res = cr.run("p", model="not-a-real-model")
    assert res.failed and res.is_error and res.subtype == "success"
    assert res.api_error_status == 404 and res.terminal_reason == "api_error"
    assert res.error_message() == (
        "There's an issue with the selected model (not-a-real-model). It may not exist or you "
        "may not have access to it. (api status 404)")


@pytest.mark.parametrize("as_array", [False, True])
def test_max_turns_without_result_uses_errors(monkeypatch, as_array):
    _capture(monkeypatch, rc=1, out=_shape(MAX_TURNS, as_array))
    res = cr.run("p")
    assert res.failed and res.text == "" and res.subtype == "error_max_turns"
    assert res.errors == ["Reached maximum number of turns (1)"]
    assert res.error_message() == "Reached maximum number of turns (1)"
    assert res.cost_usd == 0.0123


def test_is_error_with_rc_zero_still_fails(monkeypatch):
    _capture(monkeypatch, rc=0, out=_shape(NOT_LOGGED_IN))
    res = cr.run("p")
    assert res.failed and res.error_message() == "Not logged in · Please run /login"


def test_rc_127_and_124_meanings(monkeypatch):
    _capture(monkeypatch, rc=127)
    res = cr.run("p")
    assert res.failed and res.error_message().startswith("claude not found")
    _capture(monkeypatch, rc=124)
    assert cr.run("p").error_message() == "claude timed out"


def test_spawn_failure_is_rc_127(monkeypatch):
    def fake(argv, cwd, env, timeout, stdin):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(cr, "_invoke", fake)
    res = cr.run("p")
    assert res.rc == 127 and res.failed
    assert res.error_message().startswith("could not spawn claude:")


def test_success_is_not_failed(monkeypatch):
    _capture(monkeypatch, rc=0, out=_shape(SUCCESS))
    res = cr.run("p")
    assert not res.failed and res.error_message() == "" and res.text == "ok"
    assert res.cost_usd == 0.0004 and res.errors == [] and res.api_error_status is None


def test_prompt_goes_on_stdin_never_argv(monkeypatch):
    seen = _capture(monkeypatch, out=_shape(SUCCESS))
    prompt = "UNIQUE-PROMPT-MARKER " + "x" * 300_000  # far past a safe argv entry
    cr.run(prompt, model="m")
    assert seen["stdin"] == prompt.encode("utf-8")
    assert not any("UNIQUE-PROMPT-MARKER" in a for a in seen["argv"])


def test_invoke_feeds_stdin_to_the_child(monkeypatch):
    """The real _invoke passes `input=`, so the child never reads the caller's stdin."""
    calls = {}

    def fake_run(argv, **kw):
        calls.update(kw)
        return subprocess.CompletedProcess(argv, 0, b"out", b"")

    monkeypatch.setattr(cr.subprocess, "run", fake_run)
    assert cr._invoke(["claude"], None, {}, 5, b"the prompt") == (0, b"out", b"")
    assert calls["input"] == b"the prompt" and "stdin" not in calls


def test_progress_line_and_quiet(monkeypatch, capsys):
    _capture(monkeypatch, out=_shape(SUCCESS))
    monkeypatch.delenv("REINCARNATE_QUIET", raising=False)
    cr.run("p", model="claude-opus-5-5", max_turns=1, label="design 3 options")
    err = capsys.readouterr().err
    assert err == "reincarnate: design 3 options with claude-opus-5-5 (about 2-3 min)...\n"
    monkeypatch.setenv("REINCARNATE_QUIET", "1")
    cr.run("p", model="m", label="x")
    assert capsys.readouterr().err == ""


def _split_events():
    return [{"type": "system"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": '{"a": '}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "1}"}]}},
            {"type": "result", "result": "1}", "total_cost_usd": 0.7}]


def test_join_turns_adds_verbose_and_joins_a_split_reply(monkeypatch):
    seen = _capture(monkeypatch, out=json.dumps(_split_events()).encode())
    res = cr.run("p", join_turns=True)
    assert seen["argv"][-1] == "--verbose" and res.text == '{"a": 1}'
    assert res.cost_usd == 0.7 and res.raw["type"] == "result" and not res.failed


def test_join_turns_off_keeps_the_result_text_and_argv(monkeypatch):
    seen = _capture(monkeypatch, out=json.dumps(_split_events()).encode())
    res = cr.run("p")
    assert "--verbose" not in seen["argv"] and res.text == "1}"


def test_join_turns_falls_back_to_the_result_text(monkeypatch):
    _capture(monkeypatch, out=json.dumps({"type": "result", "result": "whole"}).encode())
    assert cr.run("p", join_turns=True).text == "whole"
    _capture(monkeypatch, out=b"bare words")
    assert cr.run("p", join_turns=True).text == "bare words"
