"""Batch 2a: stable identity. Capability ids, votes and paid enrichment survive re-comprehension."""

import json
import sqlite3
from pathlib import Path

import pytest

from conftest import make_dossier
from reincarnate import __version__, claude_runner, compose, comprehend, deepen, design, enrich, kit
from reincarnate.cli import main
from reincarnate.importers import vault
from reincarnate.models import Composition, Project
from reincarnate.store import SCHEMA_VERSION, Store, _name_key

FIXTURES = Path(__file__).parent / "fixtures" / "vault"
URL = "https://github.com/o/r"


def _cap(name, what, **extra):
    return {"name": name, "what": what, "portability": "high", "files": ["a.py"], **extra}


def _dossier(*caps, fit=None):
    return {"summary": "A tool.", "dependencies": {"license": "MIT"}, "capabilities": list(caps),
            "fit_to_projects": fit or []}


def _proj(store, slug="proj"):
    store.add_project(Project(slug=slug, path="/tmp/x", stack=["python"]))


# -- same-sha reconcile -----------------------------------------------------------------------
def test_same_sha_reconciles_in_place(store):
    _proj(store)
    store.upsert_dossier("o", "r", URL, "s1", _dossier(
        _cap("Alpha", "old alpha", novelty="pattern"), _cap("Beta", "old beta"),
        _cap("Gamma", "old gamma"),
        fit=[{"project": "proj", "capability": "Alpha", "how": "from dossier"}]))
    assert store.last_reconcile == {"new": 3, "updated": 0, "unchanged": 0, "retired": 0}
    a, b, g = (c.id for c in store.list_capabilities())
    store.set_vote(a, 1)
    store.set_fit(a, "proj", 0.3, "manual tweak", "manual")
    store.set_capability_fields(
        a, novelty="distinctive", acceptance=["it works"], provides=["auth:session"],
        requires=["ext:docker"], data_shapes=[{"name": "S", "fields": []}],
        key_values=[{"name": "k", "value": "v", "meaning": "m"}], confidence=0.4,
        needs_review=True, deepened={"at": "t", "sha": "x", "model": "m"})

    d = store.upsert_dossier("o", "r", URL, "s1", _dossier(
        _cap("alpha.", "fresh alpha", novelty="commodity", files=["z.py"]),
        _cap("Beta", "fresh beta"), _cap("Delta", "brand new"),
        fit=[{"project": "proj", "capability": "alpha", "how": "from dossier again"}]))
    assert store.last_reconcile == {"new": 1, "updated": 2, "unchanged": 0, "retired": 1}
    caps = {c.name: c for c in store.list_capabilities()}
    assert set(caps) == {"alpha.", "Beta", "Delta"} and len(d.capabilities) == 3
    assert caps["alpha."].id == a and caps["Beta"].id == b
    ca = caps["alpha."]
    # a deepened part keeps the behavior text deepen wrote from the code; others follow
    assert ca.what == "old alpha" and ca.files == ["z.py"]
    assert caps["Beta"].what == "fresh beta"
    # enrichment survives: same SHA means the donor code is unchanged
    assert ca.novelty == "distinctive" and ca.acceptance == ["it works"]
    assert ca.provides == ["auth:session"] and ca.requires == ["ext:docker"]
    assert ca.data_shapes[0]["name"] == "S" and ca.key_values[0]["value"] == "v"
    assert (ca.confidence, ca.needs_review) == (0.4, True)
    assert (ca.deepened_at, ca.deepened_sha, ca.deepened_model) == ("t", "x", "m")
    assert store.get_votes() == {a: 1}
    (fit,) = store.list_fits(capability_id=a)
    assert (fit.score, fit.how, fit.source) == (0.3, "manual tweak", "manual")
    # vanished: retired, still resolvable, hidden from current listings and search
    assert store.get_capability(g).name == "Gamma"
    assert g not in {c.id for c in store.list_capabilities()}
    assert g not in {c.id for c in store.list_capabilities(dossier_id=d.id)}
    assert [c.id for c in store.search_capabilities("gamma")] == []
    assert [c.id for c in store.search_capabilities("fresh")] == [b]  # FTS follows the update
    assert [c.id for c in store.search_capabilities("old")] == [a]

    # the name comes back: un-retired under its old id
    store.upsert_dossier("o", "r", URL, "s1", _dossier(_cap("GAMMA", "gamma again")))
    assert store.last_reconcile == {"new": 0, "updated": 1, "unchanged": 0, "retired": 3}
    back = store.get_capability(g)
    assert back.what == "gamma again" and back.id in {c.id for c in store.list_capabilities()}
    assert [c.id for c in store.search_capabilities("again")] == [g]
    # re-running changes nothing and retires nothing twice
    store.upsert_dossier("o", "r", URL, "s1", _dossier(_cap("GAMMA", "gamma again")))
    assert store.last_reconcile == {"new": 0, "updated": 0, "unchanged": 1, "retired": 0}


def test_name_key_normalization(store):
    assert _name_key("Board  API") == _name_key("  board api.  ") == "board api"
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("Board  API", "x")))
    (cid,) = [c.id for c in store.list_capabilities()]
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("board api.", "x")))
    assert [c.id for c in store.list_capabilities()] == [cid]
    assert store.last_reconcile["new"] == 0 and store.last_reconcile["retired"] == 0


def test_empty_enrichment_is_filled_from_new_extraction(store):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a", novelty="pattern",
                                                          acceptance=["does a thing"])))
    (c,) = store.list_capabilities()
    assert c.novelty == "pattern" and c.acceptance == ["does a thing"]


def test_new_sha_inserts_supersedes_and_carries_votes(store):
    store.upsert_dossier("o", "r", URL, "s1", _dossier(_cap("Alpha", "a"), _cap("Beta", "b")))
    a1, b1 = (c.id for c in store.list_capabilities())
    store.set_vote(a1, -1)
    d2 = store.upsert_dossier("o", "r", URL, "s2", _dossier(_cap("alpha", "a2"), _cap("New", "n")))
    assert store.last_reconcile["new"] == 2
    cur = {c.name: c for c in store.list_capabilities()}
    assert set(cur) == {"alpha", "New"} and {c.dossier_id for c in cur.values()} == {d2.id}
    assert cur["alpha"].id not in (a1, b1)
    assert store.get_votes() == {a1: -1, cur["alpha"].id: -1}
    assert store.get_capability(b1).name == "Beta"  # the old dossier's caps keep their ids
    assert [d.sha for d in store.list_dossiers()] == ["s2"]


# -- case-insensitive identity ----------------------------------------------------------------
def test_owner_repo_case_insensitive(store):
    d = store.upsert_dossier("pallets", "itsdangerous", "u", "abc123", _dossier(_cap("A", "a")))
    (cid,) = [c.id for c in store.list_capabilities()]
    d2 = store.upsert_dossier("Pallets", "ItsDangerous", "u", "abc123", _dossier(_cap("A", "a2")))
    assert d2.id == d.id and (d2.owner, d2.repo) == ("pallets", "itsdangerous")
    assert [c.id for c in store.list_capabilities()] == [cid] and len(store.list_dossiers()) == 1
    for ref in ("Pallets/ItsDangerous", "pallets/itsdangerous", "PALLETS/itsdangerous@abc"):
        assert store.get_dossier(ref).id == d.id
    # a new sha under another case still lands on the same repo (one display case, one current)
    d3 = store.upsert_dossier("PALLETS", "ITSDANGEROUS", "u", "def456", _dossier(_cap("A", "a3")))
    assert d3.donor == "pallets/itsdangerous"
    assert [x.id for x in store.list_dossiers()] == [d3.id]


def test_comprehend_skip_honors_case(store, monkeypatch):
    store.upsert_dossier("pallets", "itsdangerous", "u", "a" * 40, _dossier(_cap("A", "a")))
    monkeypatch.setattr(comprehend, "clone_repo", lambda url, **kw: {
        "path": "/nonexistent", "owner": "Pallets", "repo": "ItsDangerous", "sha": "a" * 40})
    res = comprehend.comprehend_url(store, "Pallets/ItsDangerous")
    assert res["skipped"] is True


def test_catalog_donor_filter_case_and_unknown(store, capsys):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    db = str(store.path)
    assert main(["--db", db, "catalog", "--donor", "O/R", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["total"] == 1
    assert main(["--db", db, "catalog", "--donor", "nope/x", "--json"]) == 1
    assert "no dossier for donor" in json.loads(capsys.readouterr().out)["error"]
    assert main(["--db", db, "catalog", "--fit", "ghost", "--json"]) == 1
    assert "no such project" in json.loads(capsys.readouterr().out)["error"]


# -- cli: ingest / import-vault / project add -------------------------------------------------
def test_ingest_reports_reconcile_counts(store, tmp_path, capsys):
    f = tmp_path / "d.json"
    f.write_text(json.dumps(make_dossier(("A", "does a"), ("B", "does b"))))
    argv = ["--db", str(store.path), "ingest", str(f), "--url", URL, "--sha", "s1", "--json"]
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["reconciled"]["new"] == 2
    assert main(argv) == 0
    assert json.loads(capsys.readouterr().out)["reconciled"] == {
        "new": 0, "updated": 0, "unchanged": 2, "retired": 0}


def test_comprehend_reports_reconcile(store, tmp_path, monkeypatch):
    monkeypatch.setattr(comprehend, "clone_repo", lambda url, **kw: {
        "path": str(tmp_path), "owner": "o", "repo": "r", "sha": "b" * 40})
    (tmp_path / "README.md").write_text("# hello\n" * 20)
    out = json.dumps({"type": "result", "total_cost_usd": 0.1,
                      "result": "```json\n" + json.dumps(make_dossier(("A", "a"))) + "\n```"})
    monkeypatch.setattr(claude_runner, "_invoke", lambda *a: (0, out.encode(), b""))
    monkeypatch.setenv("REINCARNATE_QUIET", "1")
    res = comprehend.comprehend_url(store, "o/r")
    assert res["reconciled"]["new"] == 1
    res = comprehend.comprehend_url(store, "o/r", force=True)
    assert res["reconciled"] == {"new": 0, "updated": 0, "unchanged": 1, "retired": 0}


def test_import_vault_rerun_skips_unless_force(store):
    first = vault.import_dir(store, FIXTURES)
    assert first["imported"] == 1 and first["reconciled"]["new"] == 8
    ids = {c.id for c in store.list_capabilities()}
    store.set_vote(next(iter(ids)), 1)
    second = vault.import_dir(store, FIXTURES)
    assert second["imported"] == 0 and second["skipped"] == 2
    assert second["already_imported"] == 1 and "reconciled" not in second
    forced = vault.import_dir(store, FIXTURES, force=True)
    assert forced["imported"] == 1 and forced["reconciled"] == {
        "new": 0, "updated": 0, "unchanged": 8, "retired": 0}
    assert {c.id for c in store.list_capabilities()} == ids and len(store.get_votes()) == 1


def test_import_vault_cli_force_flag(store, capsys):
    argv = ["--db", str(store.path), "import-vault", str(FIXTURES), "--json"]
    assert main(argv) == 0
    capsys.readouterr()
    assert main(argv) == 0 and json.loads(capsys.readouterr().out)["imported"] == 0
    assert main(argv + ["--force"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 1


def test_project_add_rerun_keeps_user_fields(store, tmp_path, capsys):
    proj = tmp_path / "app"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n")
    base = ["--db", str(store.path), "project", "add", str(proj), "--json"]
    assert main(base + ["--goal", "ship it", "--test-cmd", "make check"]) == 0
    store.conn.execute("UPDATE projects SET created='2020-01-01T00:00:00Z', "
                       "repo_url='https://x/app'")
    store.conn.commit()
    assert main(base) == 0
    p = store.get_project("app")
    assert (p.goal, p.test_cmd, p.created, p.repo_url) == (
        "ship it", "make check", "2020-01-01T00:00:00Z", "https://x/app")
    assert main(base + ["--goal", "new goal"]) == 0
    p = store.get_project("app")
    assert (p.goal, p.test_cmd) == ("new goal", "make check")
    assert main(base + ["--goal", ""]) == 0
    assert store.get_project("app").goal == ""


# -- all-digit slugs --------------------------------------------------------------------------
def test_all_digit_composition_and_design_slugs(store):
    slug = compose.unique_slug(store, compose.slug_base("2048", "a game"))
    assert slug == "c-2048"
    c = store.save_composition(Composition(slug=slug, name="2048", pitch="", idea="i",
                                           target_kind="new", target_slug="x", proposal={}))
    assert store.get_composition("c-2048").id == c.id and store.get_composition(c.id).slug == slug
    assert compose.unique_slug(store, "2048") == "c-2048-2"
    ds = design._unique_design_slug(store, compose.slug_base("2048", "a game"))
    assert ds == "d-2048"
    d = store.save_design(design.Design(slug=ds, idea="i", title="2048"))
    assert store.get_design("d-2048").id == d.id
    assert design._unique_design_slug(store, "2048") == "d-2048-2"


# -- referential integrity --------------------------------------------------------------------
def test_remove_project_drops_its_fits(store):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    (cid,) = [c.id for c in store.list_capabilities()]
    _proj(store, "p1")
    _proj(store, "p2")
    store.set_fit(cid, "p1", 0.5, "h", "manual")
    store.set_fit(cid, "p2", 0.6, "h", "manual")
    assert store.remove_project("p1")
    assert [f.project_slug for f in store.list_fits()] == ["p2"]


def test_set_fit_and_vote_reject_unknown_ids(store, capsys):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    (cid,) = [c.id for c in store.list_capabilities()]
    _proj(store)
    with pytest.raises(ValueError, match="capability"):
        store.set_fit(999, "proj", 0.5, "h", "manual")
    with pytest.raises(ValueError, match="project"):
        store.set_fit(cid, "ghost", 0.5, "h", "manual")
    with pytest.raises(ValueError, match="capability"):
        store.set_vote(999, 1)
    assert store.list_fits() == [] and store.get_votes() == {}
    assert main(["--db", str(store.path), "vote", "999", "up"]) == 1
    assert "no such capability" in capsys.readouterr().err


def test_set_capability_fields_reports_missing_row(store):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    (cid,) = [c.id for c in store.list_capabilities()]
    assert store.set_capability_fields(cid, novelty="pattern") is True
    assert store.set_capability_fields(cid) is True
    assert store.set_capability_fields(999, novelty="pattern") is False


def test_deepen_counts_vanished_capability_as_failed(store, tmp_path, monkeypatch):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a")))
    (cid,) = [c.id for c in store.list_capabilities()]

    def invoke(argv, cwd, env, timeout, stdin):
        store.conn.execute("DELETE FROM capabilities WHERE id=?", (cid,))  # vanishes mid-run
        reply = "```json\n" + json.dumps({"what": "w", "acceptance": ["x"]}) + "\n```"
        return 0, json.dumps({"result": reply, "total_cost_usd": 0.01}).encode(), b""

    monkeypatch.setattr(comprehend, "clone_repo", lambda url, **kw: {
        "path": str(tmp_path), "sha": "deadbeef"})
    monkeypatch.setattr(claude_runner, "_invoke", invoke)
    r = deepen.deepen(store, [cid])
    assert r["deepened"] == [] and [f["capability_id"] for f in r["failed"]] == [cid]


def test_enrich_counts_vanished_capability_as_failed(store, monkeypatch):
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "a"), _cap("B", "b")))
    a, b = (c.id for c in store.list_capabilities())

    def invoke(argv, cwd, env, timeout, stdin):
        store.conn.execute("DELETE FROM capabilities WHERE id=?", (b,))
        reply = json.dumps({str(a): {"novelty": "pattern"}, str(b): {"novelty": "pattern"}})
        return 0, json.dumps({"result": reply, "total_cost_usd": 0.01}).encode(), b""

    monkeypatch.setattr(claude_runner, "_invoke", invoke)
    r = enrich.enrich(store, all_=True)
    assert r["updated"] == 1 and len(r["failed_batches"]) == 1


# -- FTS over a populated table ---------------------------------------------------------------
def test_fts_rebuilt_when_index_is_created_over_existing_rows(tmp_path):
    db = tmp_path / "fts.db"
    s = Store(db)
    s.upsert_dossier("o", "r", URL, "s", _dossier(_cap("Zebra herder", "herds zebras")))
    s.close()
    raw = sqlite3.connect(db)
    for trg in ("capabilities_ai", "capabilities_ad", "capabilities_au"):
        raw.execute(f"DROP TRIGGER {trg}")
    raw.execute("DROP TABLE capabilities_fts")
    raw.commit()
    raw.close()
    s = Store(db)
    assert s.fts is True
    (c,) = s.search_capabilities("zebras")
    assert s.set_capability_fields(c.id, what="herds quokkas") is True
    assert [x.id for x in s.search_capabilities("quokkas")] == [c.id]
    assert s.search_capabilities("zebras") == []
    s.close()


# -- schema version ---------------------------------------------------------------------------
def test_user_version_stamped_and_retired_column_added(tmp_path):
    db = tmp_path / "v.db"
    Store(db).close()
    raw = sqlite3.connect(db)
    assert raw.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 6
    assert "retired_at" in {r[1] for r in raw.execute("PRAGMA table_info(capabilities)")}
    raw.close()


def test_newer_database_warns_once_and_still_opens(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("REINCARNATE_QUIET", raising=False)
    db = tmp_path / "new.db"
    Store(db).close()
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=9")
    raw.commit()
    raw.close()
    s = Store(db)
    err = capsys.readouterr().err
    assert err.count("warning:") == 1 and "schema 9" in err and f"knows {SCHEMA_VERSION}" in err
    assert s.list_capabilities() == []
    assert s._user_version() == 9  # never downgraded
    s.close()
    monkeypatch.setenv("REINCARNATE_QUIET", "1")
    Store(db).close()
    assert capsys.readouterr().err == ""


def test_blueprint_records_engine_version(store, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text("[project]\nname = 'proj'\n")
    store.add_project(Project(slug="proj", path=str(proj), stack=["python"]))
    store.upsert_dossier("o", "r", URL, "s", _dossier(_cap("A", "does a")))
    (cid,) = [c.id for c in store.list_capabilities()]
    comp = store.save_composition(Composition(
        slug="app", name="App", pitch="p", idea="i", target_kind="project", target_slug="proj",
        proposal={"capabilities": [{"capability_id": cid, "role": "r", "order": 1}]}))
    kit.export_kit(store, comp, "proj")
    bp = (proj / ".reincarnate" / "BLUEPRINT.md").read_text()
    assert f"**Engine:** reincarnate {__version__}" in bp
