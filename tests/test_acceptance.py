import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_dossier
from reincarnate import catalog, claude_runner, enrich, prompts, render
from reincarnate.models import Dossier
from reincarnate.schema import SCHEMA_HINT, normalize_dossier
from reincarnate.store import Store

REAL = Path(__file__).resolve().parent.parent / ".local-data" / "reincarnate.db"


def _cap(**kw):
    return normalize_dossier({"summary": "s", "capabilities": [{"name": "a", **kw}]})[
        "capabilities"][0]


def test_normalize_acceptance_limits():
    assert _cap()["acceptance"] == []
    assert _cap(acceptance="not a list")["acceptance"] == []
    got = _cap(acceptance=["  one  ", "", "   ", "x" * 900, "2", "3", "4", "5", "6"])
    assert got["acceptance"][0] == "one"
    assert len(got["acceptance"]) == 5 and all(len(a) <= 600 for a in got["acceptance"])
    assert len(got["acceptance"][1]) == 600 and got["acceptance"][1].endswith(" [truncated]")


def test_schema_hint_has_acceptance():
    assert '"acceptance"' in SCHEMA_HINT
    json.loads(SCHEMA_HINT.replace("|", "/"))


def test_both_prompts_have_acceptance_rules():
    for p in (prompts.build_comprehension_prompt("o", "r", None, []),
              prompts.build_excerpt_prompt("o", "r", "EXCERPT", None, [])):
        assert prompts.ACCEPTANCE_RULES in p
        assert "2-4 acceptance checks" in p and "expiry error" in p
        assert "never name the donor's third-party libraries" in p


def _real_copy(tmp_path):
    src = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    db = tmp_path / "copy.db"
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    n = dst.execute("SELECT count(*) FROM capabilities").fetchone()[0]
    dst.close()
    return db, n


@pytest.mark.skipif(not REAL.exists(), reason="no local real DB")
def test_real_db_copy_migrates_acceptance(tmp_path):
    db, n = _real_copy(tmp_path)
    s = Store(db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(capabilities)")}
    assert "acceptance_json" in cols
    caps = s.list_capabilities(current_only=False)
    # The live catalog's contents change (e.g. after a backfill); assert shape, not values.
    assert len(caps) == n and all(isinstance(c.acceptance, list) for c in caps)
    s.close()
    Store(db).close()  # second open is a no-op


def test_old_db_without_column_migrates(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    s.upsert_dossier("o", "r", "u", "s", make_dossier(("a", "b")))
    s.close()
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE capabilities DROP COLUMN acceptance_json")
    con.commit()
    con.close()
    s = Store(db)
    assert s.list_capabilities()[0].acceptance == []
    s.close()


def test_upsert_and_set_fields(store):
    d = make_dossier(("a", "does a"))
    d["capabilities"][0]["acceptance"] = ["Given x, y happens", "  ", "Given p, q"]
    store.upsert_dossier("o", "r", "u", "s", d)
    c = store.list_capabilities()[0]
    assert c.acceptance == ["Given x, y happens", "Given p, q"]
    store.set_capability_fields(c.id, novelty="pattern")
    assert store.get_capability(c.id).acceptance == c.acceptance  # None = unchanged
    store.set_capability_fields(c.id, acceptance=["new check"])
    got = store.get_capability(c.id)
    assert got.acceptance == ["new check"] and got.novelty == "pattern"
    store.set_capability_fields(c.id, acceptance=[])
    assert store.get_capability(c.id).acceptance == []


def _fake(reply, prompts_seen):
    def invoke(argv, cwd, env, timeout):
        prompts_seen.append(argv[argv.index("-p") + 1])
        return 0, json.dumps({"result": reply, "total_cost_usd": 0.01}).encode(), b""
    return invoke


def _seed(store, n):
    store.upsert_dossier("o", "r", "u", "s", make_dossier(*[(f"c{i}", f"w{i}") for i in range(n)]))
    return [c.id for c in store.list_capabilities()]


def test_enrich_acceptance_only(store, monkeypatch):
    ids = _seed(store, 2)
    store.set_capability_fields(ids[0], novelty="pattern", provides=["auth:session"])
    seen = []
    reply = json.dumps({str(ids[0]): {"acceptance": ["Given a, b"], "novelty": "distinctive",
                                      "provides": ["http:foo"]}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake(reply, seen))
    r = enrich.enrich(store, all_=True, fields=("acceptance",))
    assert r["fields"] == ["acceptance"] and r["updated"] == 1
    c = store.get_capability(ids[0])
    assert c.acceptance == ["Given a, b"]
    assert (c.novelty, c.provides) == ("pattern", ["auth:session"])  # untouched
    p = seen[0]
    assert prompts.CATALOG_ACCEPTANCE_RULES in p and "RUBRIC" not in p
    assert "commodity: a competent" not in p and "Canonical vocabulary" not in p
    assert '"acceptance"' in p and '"novelty"' not in p and '"provides"' not in p


def test_enrich_default_fields_prompt_has_no_acceptance(store, monkeypatch):
    _seed(store, 1)
    seen = []
    monkeypatch.setattr(claude_runner, "_invoke", _fake("{}", seen))
    r = enrich.enrich(store, all_=True)
    assert r["fields"] == ["novelty", "interfaces"]
    assert prompts.ACCEPTANCE_RULES not in seen[0] and "Canonical vocabulary" in seen[0]


def test_only_missing_with_acceptance(store):
    ids = _seed(store, 2)
    for i in ids:
        store.set_capability_fields(i, novelty="pattern", provides=["auth:session"])
    store.set_capability_fields(ids[1], acceptance=["x"])
    sel = enrich.select(store, all_=True, only_missing=True, fields=("acceptance",))
    assert [c.id for c in sel] == [ids[0]]
    assert enrich.select(store, all_=True, only_missing=True) == []  # default fields satisfied
    both = enrich.select(store, all_=True, only_missing=True, fields=("novelty", "acceptance"))
    assert [c.id for c in both] == [ids[0]]


def test_jev_sim_skips_acceptance_with_note(store):
    ids = _seed(store, 1)
    r = enrich.enrich(store, all_=True, engine="jev-sim", fields=("novelty", "acceptance"))
    assert r["notes"] == ["acceptance skipped: the jev engine cannot write text"]
    assert r["fields"] == ["novelty"] and r["updated"] == 1
    c = store.get_capability(ids[0])
    assert c.novelty in ("commodity", "pattern", "distinctive") and c.acceptance == []


def test_jev_acceptance_only_is_error(store):
    _seed(store, 1)
    r = enrich.enrich(store, all_=True, engine="jev-sim", fields=("acceptance",))
    assert "error" in r and "cannot write text" in r["error"]


def test_run_cli_fields(store, monkeypatch):
    _seed(store, 1)
    bad = enrich.run_cli(store, SimpleNamespace(repo=None, all=True, fields="novelty,bogus"))
    assert "error" in bad and "bogus" in bad["error"]
    assert "acceptance" in bad["error"] and "interfaces" in bad["error"]
    seen = []
    monkeypatch.setattr(claude_runner, "_invoke", _fake("{}", seen))
    ok = enrich.run_cli(store, SimpleNamespace(repo=None, all=True, fields="acceptance"))
    assert ok["fields"] == ["acceptance"]
    dflt = enrich.run_cli(store, SimpleNamespace(repo=None, all=True))
    assert dflt["fields"] == ["novelty", "interfaces"]


def test_render_acceptance_bullets():
    data = {"summary": "s", "capabilities": [
        {"name": "Q", "portability": "high", "acceptance": ["Given a, b", "Given c, d"]},
        {"name": "R", "portability": "high", "acceptance": []}]}
    d = Dossier(id=1, owner="o", repo="r", url="u", sha="a" * 40, comprehended_at="t",
                model="m", source="claude-p", license="MIT", data=data)
    md = render.render_dossier_markdown(d)
    assert "**Acceptance:**" in md and "  - Given a, b" in md and "  - Given c, d" in md
    assert md.count("**Acceptance:**") == 1


def test_catalog_entry_carries_acceptance(store):
    d = make_dossier(("pinger", "pings services"))
    d["capabilities"][0]["acceptance"] = ["Given a down host, status is red"]
    store.upsert_dossier("o", "r", "u", "s", d)
    e = catalog.build_catalog(store)[0]
    assert e.acceptance == ["Given a down host, status is red"]
    assert e.to_dict()["acceptance"] == ["Given a down host, status is red"]
