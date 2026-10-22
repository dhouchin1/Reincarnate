"""SQLite store. The only module that touches the database."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import interfaces, paths
from .models import Capability, Composition, Design, Dossier, Fit, Profile, Project

SCHEMA = """
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
"""

# columns added to `capabilities` after v0.1; migrate() adds whichever are missing
_CAP_COLUMNS = (
    ("provides_json", "TEXT DEFAULT '[]'"),
    ("requires_json", "TEXT DEFAULT '[]'"),
    ("novelty", "TEXT DEFAULT ''"),
    ("confidence", "REAL"),
    ("needs_review", "INTEGER DEFAULT 0"),
)
NOVELTY = ("commodity", "pattern", "distinctive")

FTS_SCHEMA = """
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
"""

# latest dossier per (owner, repo)
_CURRENT = """d.id = (SELECT d2.id FROM dossiers d2 WHERE d2.owner = d.owner AND d2.repo = d.repo
                     ORDER BY d2.comprehended_at DESC, d2.id DESC LIMIT 1)"""

_CAP_SELECT = """SELECT c.*, d.owner AS _owner, d.repo AS _repo, d.license AS _license
                 FROM capabilities c JOIN dossiers d ON d.id = c.dossier_id"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _novelty(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in NOVELTY else ""


def _l(s, default):
    try:
        return json.loads(s) if s else default
    except ValueError:
        return default


class Store:
    def __init__(self, db_path: Path | None = None):
        self.path = Path(db_path) if db_path else paths.db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.fts = False
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    # -- schema ---------------------------------------------------------------------------
    def migrate(self) -> None:
        with self.conn:
            self.conn.executescript(SCHEMA)
            have = {r["name"] for r in self.conn.execute("PRAGMA table_info(capabilities)")}
            for col, decl in _CAP_COLUMNS:
                if col not in have:
                    self.conn.execute(f"ALTER TABLE capabilities ADD COLUMN {col} {decl}")
            if not self.conn.execute("SELECT 1 FROM vocab_tokens LIMIT 1").fetchone():
                self._seed_vocabulary()
        try:
            with self.conn:
                self.conn.executescript(FTS_SCHEMA)
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def _seed_vocabulary(self) -> None:
        data = interfaces.load_packaged_vocabulary()
        self.conn.executemany(
            "INSERT OR IGNORE INTO vocab_tokens(token, definition, platform) VALUES (?,?,?)",
            [(t, (d or {}).get("definition", ""), 1 if (d or {}).get("platform") else 0)
             for t, d in (data.get("tokens") or {}).items()])
        self.conn.executemany(
            "INSERT OR IGNORE INTO vocab_aliases(alias, canonical) VALUES (?,?)",
            list((a, c) for a, c in (data.get("aliases") or {}).items()))

    def get_vocabulary(self) -> interfaces.Vocabulary:
        rows = self.conn.execute("SELECT token, definition, platform FROM vocab_tokens")
        tokens = {r["token"]: {"definition": r["definition"] or "", "platform": bool(r["platform"])}
                  for r in rows}
        aliases = {r["alias"]: r["canonical"]
                   for r in self.conn.execute("SELECT alias, canonical FROM vocab_aliases")}
        return interfaces.Vocabulary(tokens, aliases)

    # -- profile --------------------------------------------------------------------------
    def get_profile(self) -> Profile | None:
        r = self.conn.execute("SELECT * FROM profile WHERE id=1").fetchone()
        if not r:
            return None
        return Profile(name=r["name"] or "", stack=_l(r["stack_json"], []),
                       intents=_l(r["intents_json"], []),
                       license_policy=_l(r["license_policy_json"], {}),
                       created=r["created"] or "", updated=r["updated"] or "")

    def save_profile(self, p: Profile) -> Profile:
        existing = self.get_profile()
        p.created = (existing.created if existing else "") or p.created or now()
        p.updated = now()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO profile VALUES (1,?,?,?,?,?,?)",
                (p.name, _j(p.stack), _j(p.intents), _j(p.license_policy), p.created, p.updated))
        return p

    # -- projects -------------------------------------------------------------------------
    @staticmethod
    def _project(r) -> Project:
        return Project(slug=r["slug"], path=r["path"] or "", repo_url=r["repo_url"] or "",
                       stack=_l(r["stack_json"], []), goal=r["goal"] or "",
                       test_cmd=r["test_cmd"] or "", created=r["created"] or "")

    def add_project(self, p: Project) -> Project:
        p.created = p.created or now()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?)",
                (p.slug, p.path, p.repo_url, _j(p.stack), p.goal, p.test_cmd, p.created))
        return p

    def list_projects(self) -> list[Project]:
        rows = self.conn.execute("SELECT * FROM projects ORDER BY slug").fetchall()
        return [self._project(r) for r in rows]

    def get_project(self, slug: str) -> Project | None:
        r = self.conn.execute("SELECT * FROM projects WHERE slug=?", (slug,)).fetchone()
        return self._project(r) if r else None

    def remove_project(self, slug: str) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM projects WHERE slug=?", (slug,))
        return cur.rowcount > 0

    # -- dossiers -------------------------------------------------------------------------
    @staticmethod
    def _dossier(r) -> Dossier:
        return Dossier(id=r["id"], owner=r["owner"], repo=r["repo"], url=r["url"] or "",
                       sha=r["sha"] or "", comprehended_at=r["comprehended_at"] or "",
                       model=r["model"] or "", cost_usd=r["cost_usd"] or 0.0,
                       source=r["source"] or "", summary=r["summary"] or "",
                       license=r["license"] or "", data=_l(r["data_json"], {}))

    def upsert_dossier(self, owner, repo, url, sha, data: dict, *, model="", cost_usd=0.0,
                       source="claude-p") -> Dossier:
        summary = data.get("summary", "") or ""
        dep = data.get("dependencies")
        license_ = (dep.get("license", "") if isinstance(dep, dict) else "") or ""
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM dossiers WHERE owner=? AND repo=? AND sha=?",
                (owner, repo, sha)).fetchone()
            vals = (url, now(), model, float(cost_usd or 0.0), source, summary, license_,
                    _j(data))
            if row:
                did = row["id"]
                old = [r[0] for r in self.conn.execute(
                    "SELECT id FROM capabilities WHERE dossier_id=?", (did,))]
                self._drop_cap_refs(old)
                self.conn.execute("DELETE FROM capabilities WHERE dossier_id=?", (did,))
                self.conn.execute(
                    "UPDATE dossiers SET url=?, comprehended_at=?, model=?, cost_usd=?, source=?,"
                    " summary=?, license=?, data_json=? WHERE id=?", (*vals, did))
            else:
                cur = self.conn.execute(
                    "INSERT INTO dossiers(owner, repo, sha, url, comprehended_at, model, cost_usd,"
                    " source, summary, license, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, repo, sha, *vals))
                did = cur.lastrowid
            ids: dict[str, int] = {}
            vocab = self.get_vocabulary()
            for i, c in enumerate(data.get("capabilities") or []):
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                cur = self.conn.execute(
                    "INSERT INTO capabilities(dossier_id, idx, name, what, files_json, deps_json,"
                    " portability, portability_notes, security_notes, provides_json, requires_json,"
                    " novelty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (did, i, c["name"], c.get("what", ""), _j(c.get("files") or []),
                     _j(c.get("dependencies") or []), c.get("portability", "medium"),
                     c.get("portability_notes", ""), c.get("security_notes", ""),
                     _j(vocab.canonicalize(interfaces.normalize_list(c.get("provides")))),
                     _j(vocab.canonicalize(interfaces.normalize_list(c.get("requires")))),
                     _novelty(c.get("novelty"))))
                ids.setdefault(c["name"].strip().lower(), cur.lastrowid)
            for f in data.get("fit_to_projects") or []:
                if not isinstance(f, dict):
                    continue
                cid = ids.get(str(f.get("capability", "")).strip().lower())
                slug = f.get("project", "")
                if cid and self.conn.execute(
                        "SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                    self.conn.execute(
                        "INSERT OR REPLACE INTO fits VALUES (?,?,?,?,?)",
                        (cid, slug, 0.8, f.get("how", ""), "dossier"))
        return self.get_dossier(did)  # type: ignore[return-value]

    def _drop_cap_refs(self, cap_ids: list[int]) -> None:
        for cid in cap_ids:
            self.conn.execute("DELETE FROM fits WHERE capability_id=?", (cid,))
            self.conn.execute("DELETE FROM votes WHERE capability_id=?", (cid,))

    def get_dossier(self, ref: str | int) -> Dossier | None:
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref)
        if isinstance(ref, int):
            r = self.conn.execute("SELECT * FROM dossiers WHERE id=?", (ref,)).fetchone()
        else:
            ref = ref.strip()
            base, _, sha = ref.partition("@")
            owner, _, repo = base.partition("/")
            if sha:
                r = self.conn.execute(
                    "SELECT * FROM dossiers WHERE owner=? AND repo=? AND sha LIKE ?"
                    " ORDER BY comprehended_at DESC, id DESC LIMIT 1",
                    (owner, repo, sha + "%")).fetchone()
            else:
                r = self.conn.execute(
                    "SELECT * FROM dossiers WHERE owner=? AND repo=?"
                    " ORDER BY comprehended_at DESC, id DESC LIMIT 1", (owner, repo)).fetchone()
        return self._dossier(r) if r else None

    def list_dossiers(self, *, current_only=True) -> list[Dossier]:
        where = f"WHERE {_CURRENT}" if current_only else ""
        rows = self.conn.execute(
            f"SELECT * FROM dossiers d {where} ORDER BY owner, repo, comprehended_at DESC, id DESC"
        ).fetchall()
        return [self._dossier(r) for r in rows]

    def delete_dossier(self, dossier_id: int) -> bool:
        with self.conn:
            ids = [r[0] for r in self.conn.execute(
                "SELECT id FROM capabilities WHERE dossier_id=?", (dossier_id,))]
            self._drop_cap_refs(ids)
            cur = self.conn.execute("DELETE FROM dossiers WHERE id=?", (dossier_id,))
        return cur.rowcount > 0

    # -- capabilities ---------------------------------------------------------------------
    @staticmethod
    def _cap(r) -> Capability:
        return Capability(
            id=r["id"], dossier_id=r["dossier_id"], idx=r["idx"], name=r["name"] or "",
            what=r["what"] or "", files=_l(r["files_json"], []),
            dependencies=_l(r["deps_json"], []), portability=r["portability"] or "medium",
            portability_notes=r["portability_notes"] or "",
            security_notes=r["security_notes"] or "",
            provides=_l(r["provides_json"], []), requires=_l(r["requires_json"], []),
            novelty=r["novelty"] or "",
            confidence=r["confidence"], needs_review=bool(r["needs_review"]),
            donor=f"{r['_owner']}/{r['_repo']}", license=r["_license"] or "")

    def list_capabilities(self, *, dossier_id: int | None = None,
                          current_only=True, needs_review: bool | None = None) -> list[Capability]:
        where, args = [], []
        if needs_review is not None:
            where.append("COALESCE(c.needs_review, 0)=?")
            args.append(1 if needs_review else 0)
        if dossier_id is not None:
            where.append("c.dossier_id=?")
            args.append(dossier_id)
        elif current_only:
            where.append(_CURRENT)
        sql = _CAP_SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY d.owner, d.repo, c.dossier_id, c.idx"
        return [self._cap(r) for r in self.conn.execute(sql, args)]

    def get_capability(self, capability_id: int) -> Capability | None:
        r = self.conn.execute(_CAP_SELECT + " WHERE c.id=?", (capability_id,)).fetchone()
        return self._cap(r) if r else None

    def set_capability_fields(self, capability_id, *, provides=None, requires=None,
                              novelty=None, confidence=None, needs_review=None) -> None:
        """Update interfaces / novelty / confidence / review flag; tokens are normalized and
        canonicalized. None = keep (confidence < 0 clears it to NULL)."""
        sets, args = [], []
        vocab = self.get_vocabulary() if (provides is not None or requires is not None) else None
        if provides is not None:
            sets.append("provides_json=?")
            args.append(_j(vocab.canonicalize(interfaces.normalize_list(list(provides)))))
        if requires is not None:
            sets.append("requires_json=?")
            args.append(_j(vocab.canonicalize(interfaces.normalize_list(list(requires)))))
        if novelty is not None:
            sets.append("novelty=?")
            args.append(_novelty(novelty))
        if confidence is not None:  # a negative value clears it back to NULL (unrated)
            sets.append("confidence=?")
            args.append(float(confidence) if float(confidence) >= 0 else None)
        if needs_review is not None:
            sets.append("needs_review=?")
            args.append(1 if needs_review else 0)
        if not sets:
            return
        with self.conn:
            self.conn.execute(f"UPDATE capabilities SET {', '.join(sets)} WHERE id=?",
                              (*args, capability_id))

    def search_capabilities(self, query: str, *, limit=50, current_only=True) -> list[Capability]:
        """FTS (or LIKE) search. `current_only` hides capabilities of superseded dossiers,
        so a repo re-comprehended at a newer SHA does not show up twice."""
        tokens = re.findall(r"\w+", query or "")
        if not tokens:
            return []
        cur = f" AND {_CURRENT}" if current_only else ""
        if self.fts:
            match = " OR ".join(f'"{t}"*' for t in tokens)
            try:
                rows = self.conn.execute(
                    _CAP_SELECT + " JOIN capabilities_fts ON capabilities_fts.rowid = c.id"
                    f" WHERE capabilities_fts MATCH ?{cur} ORDER BY capabilities_fts.rank LIMIT ?",
                    (match, limit)).fetchall()
                return [self._cap(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        cond = " OR ".join("(c.name LIKE ? OR c.what LIKE ? OR c.portability_notes LIKE ?)"
                           for _ in tokens)
        args = [f"%{t}%" for t in tokens for _ in range(3)]
        rows = self.conn.execute(_CAP_SELECT + f" WHERE ({cond}){cur} ORDER BY c.id LIMIT ?",
                                 (*args, limit)).fetchall()
        return [self._cap(r) for r in rows]

    # -- fits / votes ---------------------------------------------------------------------
    def set_fit(self, capability_id, project_slug, score: float, how: str, source: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO fits VALUES (?,?,?,?,?)",
                              (capability_id, project_slug, float(score), how, source))

    def list_fits(self, *, capability_id=None, project_slug=None) -> list[Fit]:
        where, args = [], []
        if capability_id is not None:
            where.append("capability_id=?")
            args.append(capability_id)
        if project_slug is not None:
            where.append("project_slug=?")
            args.append(project_slug)
        sql = "SELECT * FROM fits" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY score DESC, capability_id, project_slug"
        return [Fit(r["capability_id"], r["project_slug"], r["score"], r["how"] or "",
                    r["source"] or "") for r in self.conn.execute(sql, args)]

    def set_vote(self, capability_id: int, vote: int) -> None:
        with self.conn:
            if vote == 0:
                self.conn.execute("DELETE FROM votes WHERE capability_id=?", (capability_id,))
            else:
                self.conn.execute("INSERT OR REPLACE INTO votes VALUES (?,?,?)",
                                  (capability_id, 1 if vote > 0 else -1, now()))

    def get_votes(self) -> dict[int, int]:
        return {r[0]: r[1] for r in self.conn.execute("SELECT capability_id, vote FROM votes")}

    # -- compositions / kits --------------------------------------------------------------
    @staticmethod
    def _comp(r) -> Composition:
        return Composition(id=r["id"], slug=r["slug"], name=r["name"] or "", pitch=r["pitch"] or "",
                           idea=r["idea"] or "", target_kind=r["target_kind"] or "",
                           target_slug=r["target_slug"] or "",
                           proposal=_l(r["proposal_json"], {}), status=r["status"] or "proposed",
                           created=r["created"] or "")

    def save_composition(self, c: Composition) -> Composition:
        c.created = c.created or now()
        with self.conn:
            row = self.conn.execute("SELECT id FROM compositions WHERE slug=?",
                                    (c.slug,)).fetchone()
            vals = (c.name, c.pitch, c.idea, c.target_kind, c.target_slug, _j(c.proposal),
                    c.status)
            if row:
                c.id = row["id"]
                self.conn.execute(
                    "UPDATE compositions SET name=?, pitch=?, idea=?, target_kind=?,"
                    " target_slug=?, proposal_json=?, status=? WHERE id=?", (*vals, c.id))
            else:
                cur = self.conn.execute(
                    "INSERT INTO compositions(slug, name, pitch, idea, target_kind, target_slug,"
                    " proposal_json, status, created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (c.slug, c.name, c.pitch, c.idea, c.target_kind, c.target_slug,
                     _j(c.proposal), c.status, c.created))
                c.id = cur.lastrowid
        return c

    def get_composition(self, ref: str | int) -> Composition | None:
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref)
        col = "id" if isinstance(ref, int) else "slug"
        r = self.conn.execute(f"SELECT * FROM compositions WHERE {col}=?", (ref,)).fetchone()
        return self._comp(r) if r else None

    def list_compositions(self) -> list[Composition]:
        rows = self.conn.execute("SELECT * FROM compositions ORDER BY id").fetchall()
        return [self._comp(r) for r in rows]

    def add_kit(self, composition_id: int, path: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO kits(composition_id, path, created) VALUES (?,?,?)",
                (composition_id, path, now()))
        return cur.lastrowid

    # -- designs --------------------------------------------------------------------------
    @staticmethod
    def _design(r) -> Design:
        return Design(id=r["id"], slug=r["slug"], idea=r["idea"] or "", title=r["title"] or "",
                      requirements=_l(r["requirements_json"], []),
                      options=_l(r["options_json"], []), engine=r["engine"] or "",
                      meta=_l(r["meta_json"], {}), created=r["created"] or "")

    def save_design(self, d: Design) -> Design:
        d.created = d.created or now()
        with self.conn:
            row = self.conn.execute("SELECT id FROM designs WHERE slug=?", (d.slug,)).fetchone()
            vals = (d.idea, d.title, _j(d.requirements), _j(d.options), d.engine, _j(d.meta))
            if row:
                d.id = row["id"]
                self.conn.execute(
                    "UPDATE designs SET idea=?, title=?, requirements_json=?, options_json=?,"
                    " engine=?, meta_json=? WHERE id=?", (*vals, d.id))
            else:
                cur = self.conn.execute(
                    "INSERT INTO designs(slug, idea, title, requirements_json, options_json,"
                    " engine, meta_json, created) VALUES (?,?,?,?,?,?,?,?)",
                    (d.slug, *vals, d.created))
                d.id = cur.lastrowid
        return d

    def get_design(self, ref: str | int) -> Design | None:
        if isinstance(ref, str) and ref.strip().isdigit():
            ref = int(ref)
        col = "id" if isinstance(ref, int) else "slug"
        r = self.conn.execute(f"SELECT * FROM designs WHERE {col}=?", (ref,)).fetchone()
        return self._design(r) if r else None

    def list_designs(self) -> list[Design]:
        return [self._design(r) for r in self.conn.execute("SELECT * FROM designs ORDER BY id")]
