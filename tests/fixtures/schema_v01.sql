-- v0.1 store schema, verbatim from src/reincarnate/store.py at commit 5469cfc (SCHEMA + FTS_SCHEMA).
-- Fixture for migration tests; do not edit.
CREATE TABLE IF NOT EXISTS profile(
  id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, stack_json TEXT, intents_json TEXT,
  license_policy_json TEXT, created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS projects(
  slug TEXT PRIMARY KEY, path TEXT, repo_url TEXT, stack_json TEXT, goal TEXT,
  test_cmd TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS dossiers(
  id INTEGER PRIMARY KEY, owner TEXT, repo TEXT, url TEXT, sha TEXT,
  comprehended_at TEXT, model TEXT, cost_usd REAL, source TEXT,
  summary TEXT, license TEXT, data_json TEXT, UNIQUE(owner, repo, sha));
CREATE TABLE IF NOT EXISTS capabilities(
  id INTEGER PRIMARY KEY, dossier_id INTEGER REFERENCES dossiers(id) ON DELETE CASCADE,
  idx INTEGER, name TEXT, what TEXT, files_json TEXT, deps_json TEXT,
  portability TEXT, portability_notes TEXT, security_notes TEXT);
CREATE INDEX IF NOT EXISTS idx_caps_dossier ON capabilities(dossier_id);
CREATE TABLE IF NOT EXISTS fits(
  capability_id INTEGER, project_slug TEXT, score REAL, how TEXT, source TEXT,
  PRIMARY KEY(capability_id, project_slug));
CREATE TABLE IF NOT EXISTS votes(
  capability_id INTEGER PRIMARY KEY, vote INTEGER, updated TEXT);
CREATE TABLE IF NOT EXISTS compositions(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, pitch TEXT, idea TEXT,
  target_kind TEXT, target_slug TEXT, proposal_json TEXT, status TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS kits(
  id INTEGER PRIMARY KEY, composition_id INTEGER, path TEXT, created TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS capabilities_fts USING fts5(
  name, what, portability_notes, content='capabilities', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS capabilities_ai AFTER INSERT ON capabilities BEGIN
  INSERT INTO capabilities_fts(rowid, name, what, portability_notes)
  VALUES (new.id, new.name, new.what, new.portability_notes);
END;
CREATE TRIGGER IF NOT EXISTS capabilities_ad AFTER DELETE ON capabilities BEGIN
  INSERT INTO capabilities_fts(capabilities_fts, rowid, name, what, portability_notes)
  VALUES ('delete', old.id, old.name, old.what, old.portability_notes);
END;
CREATE TRIGGER IF NOT EXISTS capabilities_au AFTER UPDATE ON capabilities BEGIN
  INSERT INTO capabilities_fts(capabilities_fts, rowid, name, what, portability_notes)
  VALUES ('delete', old.id, old.name, old.what, old.portability_notes);
  INSERT INTO capabilities_fts(rowid, name, what, portability_notes)
  VALUES (new.id, new.name, new.what, new.portability_notes);
END;
