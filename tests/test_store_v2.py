import sqlite3
from pathlib import Path

import pytest

from reincarnate.models import Design
from reincarnate.store import Store

REAL = Path(__file__).resolve().parent.parent / ".local-data" / "reincarnate.db"


def test_migrate_old_db_in_place(tmp_path):
    db = tmp_path / "old.db"
    s = Store(db)
    s.upsert_dossier("o", "r", "u", "s", {"capabilities": [
        {"name": "zebra herder", "what": "herds zebras"}]})
    for col in ("provides_json", "requires_json", "novelty"):  # back to the v0.1 shape
        s.conn.execute(f"ALTER TABLE capabilities DROP COLUMN {col}")
    s.conn.execute("DROP TABLE vocab_tokens")
    s.conn.commit()
    s.close()
    s = Store(db)
    s.migrate()  # idempotent
    cap = s.list_capabilities()[0]
    assert cap.name == "zebra herder" and cap.provides == [] and cap.novelty == ""
    assert len(s.get_vocabulary().tokens) == 143
    assert [x.id for x in s.search_capabilities("zebra")] == [cap.id]
    s.close()


@pytest.mark.skipif(not REAL.exists(), reason="no local real DB")
def test_migrate_copy_of_real_db(tmp_path):
    src = sqlite3.connect(f"file:{REAL}?mode=ro", uri=True)
    db = tmp_path / "copy.db"
    dst = sqlite3.connect(db)
    src.backup(dst)
    src.close()
    n = dst.execute("SELECT count(*) FROM capabilities").fetchone()[0]
    dst.close()
    s = Store(db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(capabilities)")}
    assert {"provides_json", "requires_json", "novelty"} <= cols
    assert len(s.get_vocabulary().tokens) == 143
    assert s.conn.execute("SELECT count(*) FROM capabilities").fetchone()[0] == n
    first = s.list_capabilities()[0]
    assert s.search_capabilities(first.name.split()[0], current_only=False)
    s.close()


def test_upsert_canonicalizes_and_novelty(store):
    store.conn.execute("INSERT INTO vocab_aliases VALUES ('data:p2p', 'data:peer-connection')")
    store.conn.execute("INSERT INTO vocab_aliases VALUES ('config:junk', NULL)")
    store.upsert_dossier("o", "r", "u", "s1", {"capabilities": [
        {"name": "a", "what": "w", "provides": ["Data:P2P", "bad", "config:junk"],
         "requires": ["ext:docker"], "novelty": "Distinctive"},
        {"name": "b", "what": "w", "novelty": "weird"}]})
    a, b = store.list_capabilities()
    assert a.provides == ["data:peer-connection"] and a.requires == ["ext:docker"]
    assert a.novelty == "distinctive" and b.novelty == "" and b.provides == []


def test_set_capability_fields_keeps_fts(store):
    store.upsert_dossier("o", "r", "u", "s1", {"capabilities": [
        {"name": "zebra herder", "what": "herds zebras"}]})
    cid = store.list_capabilities()[0].id
    store.set_capability_fields(cid, provides=["auth:session", "junk"], novelty="pattern")
    c = store.get_capability(cid)
    assert c.provides == ["auth:session"] and c.requires == [] and c.novelty == "pattern"
    store.set_capability_fields(cid, requires=["ext:docker"], novelty="bogus")
    c = store.get_capability(cid)
    assert c.provides == ["auth:session"] and c.requires == ["ext:docker"] and c.novelty == ""
    assert [x.id for x in store.search_capabilities("zebra")] == [cid]
    assert store.search_capabilities("herds")


def test_designs_roundtrip(store):
    d = store.save_design(Design(slug="my-design", idea="x", title="T",
                                 requirements=[{"id": "r1"}], options=[{"slug": "lean"}],
                                 engine="offline", meta={"k": 1}))
    assert d.id
    assert store.get_design(d.id).slug == "my-design"
    assert store.get_design(str(d.id)).options == [{"slug": "lean"}]
    got = store.get_design("my-design")
    assert got.meta == {"k": 1} and got.requirements == [{"id": "r1"}]
    d2 = store.save_design(Design(slug="my-design", idea="y"))
    assert d2.id == d.id and store.get_design("my-design").idea == "y"
    assert len(store.list_designs()) == 1 and store.get_design("nope") is None


def test_migrate_adds_confidence_columns(tmp_path):
    db = tmp_path / "old2.db"
    s = Store(db)
    s.upsert_dossier("o", "r", "u", "s", {"capabilities": [{"name": "a", "what": "w"}]})
    for col in ("confidence", "needs_review"):
        s.conn.execute(f"ALTER TABLE capabilities DROP COLUMN {col}")
    s.conn.commit()
    s.close()
    s = Store(db)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(capabilities)")}
    assert {"confidence", "needs_review"} <= cols
    cap = s.list_capabilities()[0]
    assert cap.confidence is None and cap.needs_review is False
    s.set_capability_fields(cap.id, confidence=0.4, needs_review=True)
    cap = s.get_capability(cap.id)
    assert cap.confidence == 0.4 and cap.needs_review is True
    s.set_capability_fields(cap.id, confidence=-1, needs_review=False)
    cap = s.get_capability(cap.id)
    assert cap.confidence is None and cap.needs_review is False
    s.close()
