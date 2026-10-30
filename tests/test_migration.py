"""Old-schema databases migrate in place. Fixtures are the real v0.1 / v0.3 schema SQL from git
history (tests/fixtures/schema_v0*.sql), populated with a small known catalog, so these tests
need no local data and no live database."""

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from reincarnate import interfaces
from reincarnate.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LIVE_DB = Path(__file__).resolve().parent.parent / ".local-data" / "reincarnate.db"
TABLES = ("profile", "projects", "dossiers", "capabilities", "fits", "votes", "compositions",
          "kits", "vocab_tokens", "vocab_aliases", "designs", "jobs")
V01_ONLY_COLUMNS = {"id", "dossier_id", "idx", "name", "what", "files_json", "deps_json",
                    "portability", "portability_notes", "security_notes"}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _build_old_db(path, schema_file):
    """A database exactly as an old release left it, holding one dossier with two capabilities
    plus a vote, a fit and a composition. Written with raw SQL, never with the current Store."""
    conn = sqlite3.connect(path)
    conn.executescript((FIXTURES / schema_file).read_text())
    conn.execute(
        "INSERT INTO dossiers(id, owner, repo, url, sha, comprehended_at, model, cost_usd, "
        "source, summary, license, data_json) VALUES (7, 'o', 'r', 'https://x/o/r', 's1', "
        "'2026-01-02T03:04:05Z', 'sonnet', 0.25, 'claude-p', 'A donor.', 'MIT', '{}')")
    for cid, idx, name, what in ((11, 0, "zebra herder", "herds zebras across fields"),
                                 (12, 1, "quokka groomer", "grooms quokkas daily")):
        conn.execute(
            "INSERT INTO capabilities(id, dossier_id, idx, name, what, files_json, deps_json, "
            "portability, portability_notes, security_notes) VALUES (?,7,?,?,?,?,?,?,?,?)",
            (cid, idx, name, what, '["a.py"]', '["dep"]', "high", "easy port", "no secrets"))
    conn.execute("INSERT INTO votes VALUES (11, 1, '2026-01-03T00:00:00Z')")
    conn.execute("INSERT INTO fits VALUES (12, 'homelab', 0.8, 'drop in', 'overlay')")
    conn.execute(
        "INSERT INTO compositions(id, slug, name, pitch, idea, target_kind, target_slug, "
        "proposal_json, status, created) VALUES (5, 'my-app', 'My App', 'pitch', 'an idea', "
        "'new', 'my-app', ?, 'proposed', '2026-01-04T00:00:00Z')",
        (json.dumps({"capabilities": [{"capability_id": 11, "order": 1}]}),))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("schema_file", ["schema_v01.sql", "schema_v03.sql"])
def test_old_schema_migrates_and_keeps_data(tmp_path, schema_file):
    fresh = Store(tmp_path / "fresh.db")
    expected = {t: _columns(fresh.conn, t) for t in TABLES}
    fresh.close()

    db = tmp_path / "old.db"
    _build_old_db(db, schema_file)
    raw = sqlite3.connect(db)
    old_cols = _columns(raw, "capabilities")
    raw.close()
    assert "data_shapes_json" not in old_cols and "deepened_at" not in old_cols
    assert V01_ONLY_COLUMNS <= old_cols  # the fixture really is an old shape

    s = Store(db)
    for t in TABLES:
        assert _columns(s.conn, t) >= expected[t], t
    assert "retired_at" in _columns(s.conn, "capabilities")
    assert "interfaces_rated_at" not in old_cols
    assert "interfaces_rated_at" in _columns(s.conn, "capabilities")
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert len(s.get_vocabulary().tokens) == len(interfaces.load_packaged_vocabulary()["tokens"])

    caps = {c.id: c for c in s.list_capabilities(current_only=False)}
    assert set(caps) == {11, 12}
    z, q = caps[11], caps[12]
    assert (z.name, z.what, z.files, z.portability) == (
        "zebra herder", "herds zebras across fields", ["a.py"], "high")
    assert (q.name, q.what, q.portability_notes, q.security_notes) == (
        "quokka groomer", "grooms quokkas daily", "easy port", "no secrets")
    assert (z.donor, z.license) == ("o/r", "MIT")
    # new columns take their defaults
    assert z.provides == [] and z.requires == [] and z.novelty == "" and z.acceptance == []
    assert z.interfaces_rated_at == ""  # old rows read as never rated
    assert z.data_shapes == [] and z.key_values == [] and z.deepened_at == ""
    assert z.confidence is None and z.needs_review is False

    assert s.conn.execute("SELECT vote FROM votes WHERE capability_id=11").fetchone()[0] == 1
    (f,) = s.list_fits()
    assert (f.capability_id, f.project_slug, f.score, f.how, f.source) == (
        12, "homelab", 0.8, "drop in", "overlay")
    comp = s.get_composition("my-app")
    assert comp.id == 5 and comp.name == "My App" and comp.status == "proposed"
    assert comp.proposal == {"capabilities": [{"capability_id": 11, "order": 1}]}
    assert [c.id for c in s.search_capabilities("zebras")] == [11]
    assert [c.id for c in s.search_capabilities("quokka")] == [12]
    assert s.fts is True
    s.close()

    # a second open is a no-op: same schema, same rows, still searchable
    before = sqlite3.connect(db)
    snapshot = {t: before.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                for t in ("dossiers", "capabilities", "votes", "fits", "compositions",
                          "vocab_tokens")}
    before.close()
    s = Store(db)
    s.migrate()
    assert {t: _columns(s.conn, t) for t in TABLES} == {
        t: _columns(sqlite3.connect(db), t) for t in TABLES}
    for t, rows in snapshot.items():
        assert [tuple(r) for r in s.conn.execute(f"SELECT * FROM {t} ORDER BY 1")] == [
            tuple(r) for r in rows], t
    assert [c.id for c in s.search_capabilities("zebras")] == [11]
    s.close()


@pytest.mark.skipif(os.environ.get("REINCARNATE_LIVE_DB_TESTS") != "1",
                    reason="opt-in: set REINCARNATE_LIVE_DB_TESTS=1 to smoke-test a copy of "
                           "the live .local-data DB")
def test_live_db_copy_smoke(tmp_path):
    if not LIVE_DB.exists():
        pytest.skip("no live DB at .local-data/reincarnate.db")
    # Copy the file and its WAL sidecars; never open the live file (even read-only, reading a
    # WAL database touches -shm).
    db = tmp_path / "copy.db"
    shutil.copy(LIVE_DB, db)
    for suffix in ("-wal", "-shm"):
        side = LIVE_DB.with_name(LIVE_DB.name + suffix)
        if side.exists():
            shutil.copy(side, tmp_path / ("copy.db" + suffix))
    s = Store(db)
    cols = _columns(s.conn, "capabilities")
    assert {"provides_json", "acceptance_json", "data_shapes_json", "deepened_model"} <= cols
    caps = s.list_capabilities(current_only=False)
    for c in caps:  # shapes and types only, never values
        assert isinstance(c.id, int) and isinstance(c.name, str)
        assert isinstance(c.provides, list) and isinstance(c.acceptance, list)
        assert isinstance(c.data_shapes, list) and isinstance(c.key_values, list)
        assert isinstance(c.deepened_at, str) and isinstance(c.deepened_model, str)
    assert isinstance(s.get_vocabulary().tokens, dict)
    s.migrate()  # idempotent
    s.close()


ANNOTATED = ("AGPL-3.0 (NOTE: package.json incorrectly states 'MIT'; the real LICENSE is "
             "GNU AGPL v3.)")


def _v1_db(path):
    _build_old_db(path, "schema_v03.sql")
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dossiers SET license=?, data_json=? WHERE id=7",
                 (ANNOTATED, json.dumps({"dependencies": {"license": ANNOTATED, "notable": []}})))
    conn.execute("INSERT INTO dossiers(id, owner, repo, url, sha, comprehended_at, license, "
                 "data_json) VALUES (8, 'o', 'q', 'u', 's1', 't', 'MIT License', 'not json')")
    conn.execute("INSERT INTO profile(id, name, stack_json, intents_json, license_policy_json) "
                 "VALUES (1, 'd', '[]', '[]', ?)",
                 (json.dumps({"deny": ["agpl-3.0-only", "GPL"], "warn": ["Unknown"]}),))
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()


def test_v1_licenses_normalized_with_note_kept_once(tmp_path):
    db = tmp_path / "v1.db"
    _v1_db(db)
    s = Store(db)
    d = s.get_dossier("o/r")
    assert d.license == "AGPL-3.0"
    assert d.data["dependencies"]["license"] == "AGPL-3.0"
    assert d.data["dependencies"]["license_note"] == ANNOTATED
    assert d.data["dependencies"]["notable"] == []
    assert s.conn.execute("SELECT license FROM dossiers WHERE id=8").fetchone()[0] == "MIT"
    assert s.get_profile().license_policy == {"deny": ["AGPL-3.0-only", "GPL"],
                                              "warn": ["unknown"]}
    assert [c.license for c in s.list_capabilities(current_only=False)] == ["AGPL-3.0"] * 2
    assert s._user_version() == 6
    s.close()

    # a v2 database is not touched again, even if a license there looks unnormalized
    raw = sqlite3.connect(db)
    raw.execute("UPDATE dossiers SET license='Custom prose' WHERE id=8")
    raw.commit()
    raw.close()
    s = Store(db)
    assert s.conn.execute("SELECT license FROM dossiers WHERE id=8").fetchone()[0] == "Custom prose"
    s.close()


def test_newer_than_engine_database_skips_license_migration(tmp_path):
    db = tmp_path / "v9.db"
    _v1_db(db)
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=9")
    raw.commit()
    raw.close()
    s = Store(db)
    assert s.conn.execute("SELECT license FROM dossiers WHERE id=7").fetchone()[0] == ANNOTATED
    assert s._user_version() == 9
    s.close()


def test_v4_strips_credentials_from_stored_project_remotes(tmp_path):
    db = tmp_path / "v3.db"
    _build_old_db(db, "schema_v03.sql")
    raw = sqlite3.connect(db)
    raw.execute("INSERT INTO projects(slug, path, repo_url) VALUES ('p', '/x', "
                "'https://me:tok3n@github.com/me/p.git'), ('q', '/y', 'git@github.com:me/q.git')")
    raw.execute("PRAGMA user_version=3")
    raw.commit()
    raw.close()
    s = Store(db)
    urls = dict(s.conn.execute("SELECT slug, repo_url FROM projects").fetchall())
    assert urls == {"p": "https://github.com/me/p.git", "q": "git@github.com:me/q.git"}
    assert s._user_version() == 6
    s.close()


def test_v5_adds_jobs_table_to_a_v4_database(tmp_path):
    db = tmp_path / "v4.db"
    _build_old_db(db, "schema_v03.sql")
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=4")
    raw.commit()
    assert not raw.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
    raw.close()
    s = Store(db)
    assert s._user_version() == 6
    jid = s.add_job("comprehend", "o/r", "{}", "[]", "queued")
    assert s.get_job(jid)["state"] == "queued"
    assert len(s.list_capabilities(current_only=False)) == 2
    s.close()


def test_v6_adds_quirks_column_to_a_v5_database(tmp_path):
    db = tmp_path / "v5.db"
    _build_old_db(db, "schema_v03.sql")
    raw = sqlite3.connect(db)
    raw.execute("PRAGMA user_version=5")
    raw.commit()
    assert "quirks_json" not in _columns(raw, "capabilities")
    raw.close()
    s = Store(db)
    assert s._user_version() == 6 and "quirks_json" in _columns(s.conn, "capabilities")
    assert [c.quirks for c in s.list_capabilities(current_only=False)] == [[], []]
    assert s.set_capability_fields(11, quirks=["drops data", " "])
    assert s.get_capability(11).quirks == ["drops data"]
    s.close()
    s = Store(db)  # idempotent
    assert s._user_version() == 6 and s.get_capability(11).quirks == ["drops data"]
    s.close()
