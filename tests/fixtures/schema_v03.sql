-- v0.3 store schema, verbatim from src/reincarnate/store.py at commit d1de854 (SCHEMA + FTS_SCHEMA).
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
CREATE TABLE IF NOT EXISTS vocab_tokens(
  token TEXT PRIMARY KEY, definition TEXT, platform INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vocab_aliases(alias TEXT PRIMARY KEY, canonical TEXT);
CREATE TABLE IF NOT EXISTS designs(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, idea TEXT, title TEXT, requirements_json TEXT,
  options_json TEXT, engine TEXT, meta_json TEXT, created TEXT);
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
-- Columns the v0.3 migrate() added to `capabilities` (_CAP_COLUMNS at that commit);
-- a real v0.3 database already had them.
ALTER TABLE capabilities ADD COLUMN provides_json TEXT DEFAULT '[]';
ALTER TABLE capabilities ADD COLUMN requires_json TEXT DEFAULT '[]';
ALTER TABLE capabilities ADD COLUMN novelty TEXT DEFAULT '';
ALTER TABLE capabilities ADD COLUMN confidence REAL;
ALTER TABLE capabilities ADD COLUMN needs_review INTEGER DEFAULT 0;
ALTER TABLE capabilities ADD COLUMN acceptance_json TEXT DEFAULT '[]';
