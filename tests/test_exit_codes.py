"""Every failure is loud and accurate. Drives `cli.main` and asserts the exit code and where
the message went."""

import json

import pytest

from conftest import make_dossier
from reincarnate import claude_runner, cli, comprehend


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setenv("REINCARNATE_QUIET", "1")
    monkeypatch.delenv("REINCARNATE_DEBUG", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)


def run(capsys, *argv):
    rc = cli.main([str(a) for a in argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def handled(capsys, *argv) -> str:
    """Exit 1, nothing on stdout, exactly one `error: ` line on stderr (returned)."""
    rc, out, err = run(capsys, *argv)
    assert rc == 1, (argv, out, err)
    assert out == ""
    assert err.startswith("error: ") and "Traceback" not in err
    return err


def one_line(err: str) -> str:
    assert err.count("\n") == 1 and err.endswith("\n"), err
    return err


@pytest.fixture
def seeded(store):
    store.upsert_dossier("o", "r", "https://github.com/o/r", "abc",
                         make_dossier(("Session login", "log users in with sessions"),
                                      ("Status dashboard", "show service health checks")))
    return store


def _result(obj, rc=0):
    def fake(argv, cwd, env, timeout, stdin):
        return rc, json.dumps(obj).encode(), b""
    return fake


BAD_MODEL = {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 404,
             "terminal_reason": "api_error", "total_cost_usd": 0.5,
             "result": "There's an issue with the selected model (nope)."}
MAX_TURNS = {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "terminal_reason": "max_turns", "total_cost_usd": 0.0123,
             "errors": ["Reached maximum number of turns (1)"]}
NOT_LOGGED_IN = {"type": "result", "is_error": True, "total_cost_usd": 0.002,
                 "result": "Not logged in · Please run /login"}


# -- delegated not-found / bad-input paths ------------------------------------------------
@pytest.mark.parametrize("argv, needle", [
    (["salvage", "999", "--into", "x"], "capability 999 not found"),
    (["kit", "nosuch", "--into", "new:z"], "composition nosuch not found"),
    (["compose", "show", "nosuch"], "no such composition: nosuch"),
    (["design", "show", "nosuch"], "no such design: nosuch"),
    (["design", "pick", "nosuch", "lean"], "no such design: nosuch"),
    (["enrich", "nosuch/repo"], "no dossier for nosuch/repo"),
    (["enrich", "--all", "--fields", "bogus"], "unknown field bogus"),
    (["enrich", "--all", "--engine", "jev"], "TYPESAFE_API_KEY"),
    (["jev", "models"], "TYPESAFE_API_KEY"),
    (["overlay"], "give an owner/repo or --all"),
    (["export-md", "nosuch"], "no dossier found for nosuch"),
    (["reincarnate", "nosuch", "--into", "new:z"], "unknown dossier: nosuch"),
    (["import-interfaces", "/nonexistent/caps.json"], "not a file"),
    (["import-vault", "/nonexistent/vault-dir"], "imported 0 notes"),
    (["deepen", "999"], "capability 999: no such capability"),
    (["compose", ""], "an idea is required"),
    (["design", ""], "an idea is required"),
    (["dossier", "show", "nosuch"], "no such dossier: nosuch"),
    (["vote", "99", "up"], "no such capability: 99"),
    (["project", "rm", "nosuch"], "no such project: nosuch"),
])
def test_handled_failures_exit_1_on_stderr(capsys, store, argv, needle):
    err = handled(capsys, *argv)
    assert needle in err


@pytest.mark.parametrize("argv, needle", [
    (["compose", "show", "nosuch", "--json"], "no such composition"),
    (["enrich", "nosuch/repo", "--json"], "no dossier for nosuch/repo"),
    (["design", "show", "nosuch", "--json"], "no such design"),
    (["dossier", "show", "nosuch", "--json"], "no such dossier"),
    (["vote", "99", "up", "--json"], "no such capability"),
    (["clone", "https://gitlab.com/a/b", "--json"], "not a github repo reference"),
])
def test_json_failures_print_json_with_error_on_stdout(capsys, store, argv, needle):
    rc, out, err = run(capsys, *argv)
    assert rc == 1 and err == ""
    assert needle in json.loads(out)["error"]


# -- would-be tracebacks ------------------------------------------------------------------
def test_gitlab_url_is_one_line_exit_1(capsys, store):
    for cmd in ("clone", "comprehend"):
        err = one_line(handled(capsys, cmd, "https://gitlab.com/a/b"))
        assert "not a github repo reference" in err


def test_export_md_out_existing_directory(capsys, seeded, tmp_path):
    one_line(handled(capsys, "export-md", "o/r", "--out", tmp_path))


def test_db_pointing_at_a_directory(capsys, tmp_path):
    err = one_line(handled(capsys, "--db", tmp_path, "dossier", "list"))
    assert f"cannot open database {tmp_path}" in err


def test_huge_id_is_exit_1(capsys, store):
    err = one_line(handled(capsys, "vote", "99999999999999999999", "up"))
    assert "number out of range" in err
    one_line(handled(capsys, "salvage", "99999999999999999999", "--into", "x"))


@pytest.fixture
def composition(capsys, seeded):
    rc, out, _ = run(capsys, "compose", "dashboard", "health", "--engine", "offline", "--json")
    assert rc == 0
    return json.loads(out)["slug"]


def test_kit_notes_unreadable_is_exit_1(capsys, composition, tmp_path):
    binary = tmp_path / "notes.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80 not utf-8")
    for notes, why in ((tmp_path / "missing.md", "No such file or directory"),
                       (tmp_path, "Is a directory"), (binary, "not UTF-8 text")):
        err = one_line(handled(capsys, "kit", composition, "--into", tmp_path / "out",
                               "--notes", notes))
        assert f"cannot read --notes file {notes}: {why}" in err


# -- unexpected exceptions -----------------------------------------------------------------
def _boom(store, args):
    raise KeyError("boom")


def test_unexpected_exception_is_exit_3_one_line(capsys, store, monkeypatch):
    from reincarnate import catalog
    monkeypatch.setattr(catalog, "run_cli", _boom)
    rc, out, err = run(capsys, "catalog")
    assert rc == 3 and out == ""
    assert err == ("error: internal error: KeyError: 'boom' "
                   "(set REINCARNATE_DEBUG=1 for the traceback)\n")


def test_unexpected_exception_json_and_debug(capsys, store, monkeypatch):
    from reincarnate import catalog
    monkeypatch.setattr(catalog, "run_cli", _boom)
    rc, out, _ = run(capsys, "catalog", "--json")
    assert rc == 3 and json.loads(out)["error"].startswith("internal error: KeyError")
    monkeypatch.setenv("REINCARNATE_DEBUG", "1")
    with pytest.raises(KeyError):
        cli.main(["catalog"])


def test_handled_exception_from_a_handler_is_exit_1(capsys, store, monkeypatch):
    def raises(store, args):
        raise RuntimeError("disk\nfull")
    monkeypatch.setattr(cli, "cmd_init", raises)
    assert one_line(handled(capsys, "init")) == "error: disk full\n"


# -- credential redaction -------------------------------------------------------------------
def test_redact():
    assert cli.redact("https://user:hunter2@gitlab.com/a/b") == "https://user:***@gitlab.com/a/b"
    assert cli.redact("x https://ghp_tok@github.com/a y") == "x https://***@github.com/a y"
    assert cli.redact("git@github.com:a/b.git and https://github.com/a") == (
        "git@github.com:a/b.git and https://github.com/a")


def test_credentials_redacted_in_every_channel(capsys, store, monkeypatch):
    err = handled(capsys, "clone", "https://user:hunter2@gitlab.com/a/b.git")
    assert "hunter2" not in err and "user:***@gitlab.com" in err
    from reincarnate import catalog
    monkeypatch.setattr(catalog, "run_cli",
                        lambda s, a: {"error": "fetch https://u:pw1@h.example/x failed"})
    rc, out, _ = run(capsys, "catalog", "--json")
    assert rc == 1 and json.loads(out)["error"] == "fetch https://u:***@h.example/x failed"

    def leak(store, args):
        raise ValueError("cannot reach https://u:pw2@h.example")
    monkeypatch.setattr(catalog, "run_cli", leak)
    assert "pw2" not in handled(capsys, "catalog")


# -- success paths still exit 0 -------------------------------------------------------------
def test_success_paths_exit_0(capsys, seeded, tmp_path):
    for argv in (["init"], ["dossier", "list"], ["dossier", "show", "o/r"], ["catalog"],
                 ["vote", "1", "up"], ["export-md", "o/r", "--out", tmp_path / "o.md"],
                 ["compose", "dashboard", "health", "--engine", "offline"],
                 ["design", "dashboard", "health", "--engine", "offline"],
                 ["overlay", "--all"], ["import-vault", tmp_path]):
        rc, out, err = run(capsys, *argv)
        assert rc == 0 and out and "error" not in err, (argv, err)


def test_vote_clear_says_cleared(capsys, seeded):
    rc, out, _ = run(capsys, "vote", "1", "clear")
    assert rc == 0 and out.strip().endswith(": vote cleared")
    rc, out, _ = run(capsys, "vote", "1", "down")
    assert out.strip().endswith(": vote -1")


# -- model failures: real cause and cost, exit 1 ----------------------------------------------
def test_comprehend_model_failure_reports_cause_and_cost(capsys, store, monkeypatch, tmp_path,
                                                         home):
    (tmp_path / "README.md").write_text("# hi\n" * 20)
    (tmp_path / "app.py").write_text("print('hi')\n" * 20)
    monkeypatch.setattr(comprehend, "clone_repo", lambda url, **kw: {
        "path": str(tmp_path), "owner": "o", "repo": "r", "sha": "b" * 40, "reused": False})
    monkeypatch.setattr(claude_runner, "_invoke", _result(BAD_MODEL, rc=1))
    rc, out, err = run(capsys, "comprehend", "o/r", "--json")
    res = json.loads(out)
    assert rc == 1 and err == "" and res["parsed"] is False and res["cost_usd"] == 0.5
    assert res["message"].startswith(
        "model call failed: There's an issue with the selected model (nope). (api status 404)")
    raw = home / "dossiers" / "o-r.raw.txt"
    assert f"raw saved to {raw}" in res["message"] and raw.exists()
    err = handled(capsys, "comprehend", "o/r", "--force")
    assert "issue with the selected model" in err and "no dossier JSON" not in err


def test_enrich_all_batches_failed_exit_1_with_cause_and_cost(capsys, seeded, monkeypatch):
    monkeypatch.setattr(claude_runner, "_invoke", _result(MAX_TURNS, rc=1))
    err = handled(capsys, "enrich", "o/r")
    assert "($0.0123)" in err
    assert "batch 0: model call failed: Reached maximum number of turns (1)" in err


def test_review_all_batches_failed_exit_1(capsys, seeded, monkeypatch):
    seeded.set_capability_fields(1, needs_review=True)
    monkeypatch.setattr(claude_runner, "_invoke", _result(NOT_LOGGED_IN))
    err = handled(capsys, "review")
    assert "model call failed: Not logged in" in err and "($0.0020)" in err


def _deepen_setup(monkeypatch, tmp_path, replies):
    monkeypatch.setattr(comprehend, "clone_repo",
                        lambda url, **kw: {"path": str(tmp_path), "sha": "s", "owner": "o",
                                           "repo": "r"})
    queue = list(replies)

    def fake(argv, cwd, env, timeout, stdin):
        return 0, json.dumps(queue.pop(0)).encode(), b""
    monkeypatch.setattr(claude_runner, "_invoke", fake)


def test_deepen_all_failed_exit_1_partial_exit_0(capsys, seeded, monkeypatch, tmp_path):
    _deepen_setup(monkeypatch, tmp_path, [NOT_LOGGED_IN, NOT_LOGGED_IN])
    err = handled(capsys, "deepen", "1", "2")
    assert "deepened 0 of 2 capabilities ($0.0040); 2 failed" in err
    assert "capability 1: model call failed: Not logged in · Please run /login" in err
    ok = {"type": "result", "total_cost_usd": 0.001, "result": '{"what": "Pinned."}'}
    _deepen_setup(monkeypatch, tmp_path, [ok, NOT_LOGGED_IN])
    rc, out, _ = run(capsys, "deepen", "1", "2", "--json")
    res = json.loads(out)
    assert rc == 0 and res["deepened"] == [1] and res["cost_usd"] == 0.003
    assert res["failed"][0]["reason"].startswith("model call failed: Not logged in")


def test_design_model_failure_has_cost_and_saves_raw(capsys, seeded, monkeypatch, home):
    monkeypatch.setattr(claude_runner, "_invoke", _result(BAD_MODEL, rc=1))
    rc, out, err = run(capsys, "design", "a dashboard with sessions", "--json")
    res = json.loads(out)
    assert rc == 1 and res["cost_usd"] == 0.5
    assert res["error"].startswith(
        "claude-p model call failed: There's an issue with the selected model (nope).")
    assert "($0.5000 spent; raw reply saved to " in res["error"]
    saved = list((home / "designs").glob("*.raw.txt"))
    assert len(saved) == 1 and str(saved[0]) in res["error"]
    assert "issue with the selected model" in saved[0].read_text()
    assert seeded.list_designs() == []


def test_design_text_shows_cost(capsys, seeded, monkeypatch):
    rc, out, _ = run(capsys, "design", "dashboard", "health", "--engine", "offline")
    assert rc == 0 and "cost: $0.0000" in out


def test_compose_model_failure_saves_nothing(capsys, seeded, monkeypatch):
    monkeypatch.setattr(claude_runner, "_invoke", _result(NOT_LOGGED_IN))
    err = handled(capsys, "compose", "dashboard", "health")
    assert "claude-p model call failed: Not logged in · Please run /login" in err
    assert "$0.0020 spent, nothing saved; use --engine offline" in err
    rc, out, _ = run(capsys, "compose", "dashboard", "health", "--json")
    assert rc == 1 and json.loads(out)["cost_usd"] == 0.002
    assert seeded.list_compositions() == []


def test_compose_success_records_cost(capsys, seeded, monkeypatch):
    reply = {"type": "result", "total_cost_usd": 0.0412345,
             "result": json.dumps({"name": "Dash", "capabilities": [
                 {"capability_id": 1, "role": "auth"}, {"capability_id": 2, "role": "ui"}]})}
    monkeypatch.setattr(claude_runner, "_invoke", _result(reply))
    rc, out, _ = run(capsys, "compose", "dashboard", "health", "--json")
    comp = json.loads(out)["composition"]
    assert rc == 0 and comp["proposal"]["cost_usd"] == 0.0412
    assert comp["proposal"]["engine"] == "claude-p"
    assert "cost: $0.0412" in json.loads(out)["text"]


def test_overlay_claude_failure_reports_cause_and_cost(capsys, seeded, monkeypatch):
    monkeypatch.setattr(claude_runner, "_invoke", _result(NOT_LOGGED_IN))
    rc, out, _ = run(capsys, "overlay", "o/r", "--engine", "claude-p", "--json")
    res = json.loads(out)
    assert rc == 0 and res["cost_usd"] == 0.002 and res["results"][0]["cost_usd"] == 0.002
    assert res["results"][0]["message"] == (
        "claude -p failed: Not logged in · Please run /login; fell back to heuristic")
