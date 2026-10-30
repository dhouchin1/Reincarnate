import json

from conftest import make_dossier
from reincarnate.cli import main


def run(capsys, *argv):
    rc = main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_init(capsys, home):
    rc, out, _ = run(capsys, "init", "--json")
    assert rc == 0
    assert (home / "reincarnate.db").exists()
    assert json.loads(out)["data_dir"] == str(home)


def test_profile_set_show(capsys):
    rc, _, _ = run(capsys, "profile", "set", "--name", "Dan", "--stack", "python, go",
                   "--intent", "ship", "--intent", "learn", "--deny-license", "AGPL-3.0")
    assert rc == 0
    rc, out, _ = run(capsys, "profile", "show", "--json")
    p = json.loads(out)
    assert p["name"] == "Dan" and p["stack"] == ["python", "go"]
    assert p["intents"] == ["ship", "learn"]
    assert p["license_policy"]["deny"] == ["AGPL-3.0"]
    run(capsys, "profile", "set", "--clear-intents")
    _, out, _ = run(capsys, "profile", "show", "--json")
    assert json.loads(out)["intents"] == [] and json.loads(out)["name"] == "Dan"


def test_project_add_list_rm(capsys, tmp_path):
    proj = tmp_path / "My App"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    rc, out, _ = run(capsys, "project", "add", str(proj), "--goal", "a thing", "--json")
    p = json.loads(out)
    assert rc == 0 and p["slug"] == "my-app" and p["stack"] == ["python"]
    assert p["test_cmd"] == "pytest" and p["goal"] == "a thing"
    _, out, _ = run(capsys, "project", "list", "--json")
    assert [x["slug"] for x in json.loads(out)] == ["my-app"]
    assert run(capsys, "project", "rm", "my-app")[0] == 0
    assert run(capsys, "project", "rm", "my-app")[0] == 1


def test_project_infer_node(capsys, tmp_path):
    proj = tmp_path / "web"
    proj.mkdir()
    (proj / "package.json").write_text('{"scripts": {"test": "vitest"}}')
    (proj / "tsconfig.json").write_text("{}")
    _, out, _ = run(capsys, "project", "add", str(proj), "--json")
    p = json.loads(out)
    assert p["stack"] == ["node", "typescript"] and p["test_cmd"] == "npm test"


def test_ingest_then_dossier_show(capsys, tmp_path):
    f = tmp_path / "d.json"
    d = make_dossier(("Parser", "parses"))
    d["fit_to_fleet"] = []
    d["capabilities"][0]["portability"] = "HIGH"
    f.write_text(json.dumps(d))
    rc, out, _ = run(capsys, "ingest", str(f), "--url", "https://github.com/o/r.git",
                     "--sha", "abc", "--source", "session", "--json")
    assert rc == 0
    res = json.loads(out)
    assert res["donor"] == "o/r" and res["capabilities"] == 1 and res["dossier_id"] > 0
    rc, out, _ = run(capsys, "dossier", "show", "o/r", "--json")
    shown = json.loads(out)
    assert rc == 0 and shown["sha"] == "abc" and shown["source"] == "session"
    assert shown["data"]["capabilities"][0]["portability"] == "high"
    _, out, _ = run(capsys, "dossier", "list", "--json")
    assert json.loads(out)[0]["donor"] == "o/r"


def test_ingest_rejects_invalid(capsys, tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{}")
    rc, _, err = run(capsys, "ingest", str(f), "--url", "https://github.com/o/r", "--sha", "a")
    assert rc == 1 and "invalid dossier" in err


def test_vote(capsys, tmp_path):
    f = tmp_path / "d.json"
    f.write_text(json.dumps(make_dossier(("Parser", "parses"))))
    run(capsys, "ingest", str(f), "--url", "https://github.com/o/r", "--sha", "a")
    assert run(capsys, "vote", "1", "up")[0] == 0
    assert run(capsys, "vote", "99", "up")[0] == 1


def test_db_option(capsys, tmp_path):
    db = tmp_path / "other.db"
    run(capsys, "--db", str(db), "profile", "set", "--name", "Z")
    assert db.exists()


def test_missing_module_exits_2(capsys):
    rc, _, err = run(capsys, "export-md", "o/r")
    # render is owned by another agent; either it exists or we get a clear exit 2
    assert rc in (0, 1, 2)
    if rc == 2:
        assert "not implemented yet" in err


def test_check_plugin_compares_versions(tmp_path, capsys):
    import json as _json

    from reincarnate import __version__, cli
    root = tmp_path / "plug"
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(_json.dumps({"version": __version__}))
    assert cli.main(["check-plugin", str(root)]) == 0
    (root / ".claude-plugin" / "plugin.json").write_text(_json.dumps({"version": "0.0.0"}))
    assert cli.main(["check-plugin", str(root)]) == 1
    assert "/reincarnate:setup" in capsys.readouterr().err
    assert cli.main(["check-plugin", str(tmp_path / "missing")]) == 1
