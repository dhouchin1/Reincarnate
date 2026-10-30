"""SQLite store. The only module that touches the database."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import interfaces, licenses, paths, schema
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
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, kind TEXT, target TEXT, params_json TEXT, state TEXT, stage TEXT,
  stages_json TEXT, created TEXT, started TEXT, finished TEXT, cost_usd REAL,
  result_json TEXT, error TEXT);
"""

# columns added to `capabilities` after v0.1; migrate() adds whichever are missing
_CAP_COLUMNS = (
    ("provides_json", "TEXT DEFAULT '[]'"),
    ("requires_json", "TEXT DEFAULT '[]'"),
    ("novelty", "TEXT DEFAULT ''"),
    ("confidence", "REAL"),
    ("needs_review", "INTEGER DEFAULT 0"),
    ("acceptance_json", "TEXT DEFAULT '[]'"),
    ("data_shapes_json", "TEXT DEFAULT '[]'"),
    ("key_values_json", "TEXT DEFAULT '[]'"),
    ("deepened_at", "TEXT DEFAULT ''"),
    ("deepened_sha", "TEXT DEFAULT ''"),
    ("deepened_model", "TEXT DEFAULT ''"),
    ("retired_at", "TEXT DEFAULT ''"),
    ("interfaces_rated_at", "TEXT DEFAULT ''"),  # set once provides/requires were written
    ("basis_json", "TEXT DEFAULT '{}'"),  # field group -> how it is known (schema.BASIS)
    ("quirks_json", "TEXT DEFAULT '[]'"),  # donor behaviors that look like bugs (v6)
)
# bumped when a release adds schema a previous engine would silently ignore
SCHEMA_VERSION = 6
NOVELTY = ("commodity", "pattern", "distinctive")
# a weaker fit source never overwrites a stronger one; overlay is the word-overlap heuristic
FIT_RANK = {"manual": 4, "dossier": 3, "overlay-claude": 2, "overlay": 1}

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

# Search tokens: stopwords and tokens under 3 characters never count, except these.
_SEARCH_STOP = frozenset(
    "a an and the of to for with in on by is are be or my our your that this it as at from into "
    "app application build want need using use can will should which who what when".split())
_SHORT_OK = frozenset("ui db io ai ml qr os ci vm ip".split())


def _fold(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", (text or "").lower())
                   if not unicodedata.combining(ch))


def _fold_words(text: str) -> list[str]:
    return re.findall(r"\w+", _fold(text))


def search_tokens(query: str) -> list[str]:
    """Meaningful, de-duplicated query words (lowercased, accents folded), in query order."""
    out: list[str] = []
    for t in _fold_words(query):
        if t in _SEARCH_STOP or (len(t) < 3 and t not in _SHORT_OK) or t in out:
            continue
        out.append(t)
    return out


# latest dossier per (owner, repo); owner and repo compare case-insensitively
_CURRENT = """d.id = (SELECT d2.id FROM dossiers d2
                     WHERE d2.owner = d.owner COLLATE NOCASE AND d2.repo = d.repo COLLATE NOCASE
                     ORDER BY d2.comprehended_at DESC, d2.id DESC LIMIT 1)"""
# capabilities a re-ingest no longer found stay resolvable by id but leave every listing
_LIVE = "COALESCE(c.retired_at, '') = ''"

_CAP_SELECT = """SELECT c.*, d.owner AS _owner, d.repo AS _repo, d.license AS _license
                 FROM capabilities c JOIN dossiers d ON d.id = c.dossier_id"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _j(v) -> str:
    return json.dumps(v, ensure_ascii=False)


def _novelty(v) -> str:
    v = str(v or "").strip().lower()
    return v if v in NOVELTY else ""


def _name_key(name) -> str:
    """Identity of a capability within a donor: case, spacing and edge punctuation ignored."""
    s = " ".join(str(name or "").lower().split())
    return re.sub(r"^[\W_]+|[\W_]+$", "", s) or s


# basis group -> the capability columns that carry it
_BASIS_COLS = {
    "what": ("what",), "interfaces": ("provides_json", "requires_json"),
    "novelty": ("novelty",), "acceptance": ("acceptance_json",),
    "data_shapes": ("data_shapes_json",), "key_values": ("key_values_json",)}


def _clean_basis(v) -> dict:
    """Only known field groups with a known basis name survive."""
    if not isinstance(v, dict):
        return {}
    return {k: b for k, b in v.items() if k in schema.BASIS_FIELDS and b in schema.BASIS}


def _empty(v) -> bool:
    return v in (None, "", "[]", "{}", "null")


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
        self.last_reconcile: dict | None = None
        self.migrate()
        self._warn_newer()

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
            if self._user_version() < 2:
                self._normalize_licenses()
            if self._user_version() < 3:
                self._backfill_evidence()
            if self._user_version() < 4:
                self._strip_project_credentials()
            if self._user_version() < SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={int(SCHEMA_VERSION)}")
        try:
            with self.conn:
                had_fts = self.conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='capabilities_fts'").fetchone()
                self.conn.executescript(FTS_SCHEMA)
                # an index created over rows that already exist starts out empty
                if not had_fts and self.conn.execute(
                        "SELECT 1 FROM capabilities LIMIT 1").fetchone():
                    self.conn.execute(
                        "INSERT INTO capabilities_fts(capabilities_fts) VALUES('rebuild')")
            self.fts = True
        except sqlite3.OperationalError:
            self.fts = False

    def _normalize_licenses(self) -> None:
        """v2: licenses are SPDX-style ids; prose moves to dependencies.license_note."""
        for r in self.conn.execute("SELECT id, license, data_json FROM dossiers").fetchall():
            raw = r["license"] or ""
            norm = licenses.normalize(raw)
            if norm == raw:
                continue
            data = _l(r["data_json"], {})
            if isinstance(data, dict):
                dep = data.get("dependencies")
                dep = dep if isinstance(dep, dict) else {}
                if raw.strip() and not dep.get("license_note"):
                    dep["license_note"] = raw
                dep["license"] = norm
                data["dependencies"] = dep
                self.conn.execute("UPDATE dossiers SET license=?, data_json=? WHERE id=?",
                                  (norm, _j(data), r["id"]))
            else:
                self.conn.execute("UPDATE dossiers SET license=? WHERE id=?", (norm, r["id"]))
        for r in self.conn.execute("SELECT id, license_policy_json FROM profile").fetchall():
            pol = _l(r["license_policy_json"], {})
            if not isinstance(pol, dict):
                continue
            fixed = {**pol}
            for key in ("deny", "warn"):
                if isinstance(pol.get(key), list):
                    fixed[key] = list(dict.fromkeys(licenses.policy_entry(x) for x in pol[key]))
            if fixed != pol:
                self.conn.execute("UPDATE profile SET license_policy_json=? WHERE id=?",
                                  (_j(fixed), r["id"]))

    def _backfill_evidence(self) -> None:
        """v3: deepened fields were read from donor code (observed), vault imports were reported
        by another tool (external), and no dossier before v3 recorded what it read (coverage
        unknown). Everything else stays unrecorded. Only fills what is missing, so it is safe
        to run twice."""
        deepened = ("what", "acceptance", "data_shapes", "key_values")
        for r in self.conn.execute(
                "SELECT c.id, c.basis_json, c.deepened_at, d.source FROM capabilities c"
                " JOIN dossiers d ON d.id = c.dossier_id").fetchall():
            basis = _l(r["basis_json"], {})
            basis = basis if isinstance(basis, dict) else {}
            fill = []
            if r["deepened_at"]:
                fill.append(("observed", deepened))
            if r["source"] == "vault-import":
                fill.append(("external", schema.BASIS_FIELDS))
            merged = dict(basis)
            for name, keys in fill:
                for k in keys:
                    merged.setdefault(k, name)
            if merged != basis:
                self.conn.execute("UPDATE capabilities SET basis_json=? WHERE id=?",
                                  (_j(merged), r["id"]))
        for r in self.conn.execute(
                "SELECT id, owner, repo, sha, source, data_json FROM dossiers").fetchall():
            data = _l(r["data_json"], {})
            if not isinstance(data, dict) or data.get("coverage"):
                continue
            why = ("imported from second-brain; what was read is not recorded"
                   if r["source"] == "vault-import" else "ingested without coverage")
            data["coverage"] = schema.unknown_coverage(
                f"{r['owner']}/{r['repo']}@{r['sha']}", why)
            self.conn.execute("UPDATE dossiers SET data_json=? WHERE id=?", (_j(data), r["id"]))

    def _strip_project_credentials(self) -> None:
        """v4: a project's repo_url may carry `user:token@` from its git remote; drop it."""
        for r in self.conn.execute("SELECT slug, repo_url FROM projects").fetchall():
            clean = schema.strip_url_credentials(r["repo_url"] or "")
            if clean != (r["repo_url"] or ""):
                self.conn.execute("UPDATE projects SET repo_url=? WHERE slug=?",
                                  (clean, r["slug"]))

    def _user_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def _warn_newer(self) -> None:
        have = self._user_version()
        if have > SCHEMA_VERSION and os.environ.get("REINCARNATE_QUIET", "") not in ("1", "true"):
            print(f"warning: this database was written by a newer Reincarnate (schema {have}, "
                  f"this engine knows {SCHEMA_VERSION}); newer data may be missing from its "
                  "output. Upgrade: uv tool install --reinstall . (from an updated checkout)",
                  file=sys.stderr)

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
        return Project(slug=r["slug"], path=r["path"] or "",
                       repo_url=schema.strip_url_credentials(r["repo_url"]),
                       stack=_l(r["stack_json"], []), goal=r["goal"] or "",
                       test_cmd=r["test_cmd"] or "", created=r["created"] or "")

    def add_project(self, p: Project) -> Project:
        p.created = p.created or now()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO projects VALUES (?,?,?,?,?,?,?)",
                (p.slug, p.path, schema.strip_url_credentials(p.repo_url), _j(p.stack), p.goal,
                 p.test_cmd, p.created))
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
            self.conn.execute("DELETE FROM fits WHERE project_slug=?", (slug,))
        return cur.rowcount > 0

    # -- dossiers -------------------------------------------------------------------------
    @staticmethod
    def _dossier(r) -> Dossier:
        return Dossier(id=r["id"], owner=r["owner"], repo=r["repo"], url=r["url"] or "",
                       sha=r["sha"] or "", comprehended_at=r["comprehended_at"] or "",
                       model=r["model"] or "", cost_usd=r["cost_usd"] or 0.0,
                       source=r["source"] or "", summary=r["summary"] or "",
                       license=r["license"] or "", data=_l(r["data_json"], {}))

    def find_dossier(self, owner, repo, sha) -> Dossier | None:
        """The dossier for exactly this (owner, repo, sha); owner and repo ignore case."""
        r = self.conn.execute(
            "SELECT * FROM dossiers WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE"
            " AND sha=?", (owner, repo, sha)).fetchone()
        return self._dossier(r) if r else None

    def upsert_dossier(self, owner, repo, url, sha, data: dict, *, model="", cost_usd=0.0,
                       source="claude-p") -> Dossier:
        """Insert a dossier, or re-comprehend an existing (owner, repo, sha) in place.

        An existing dossier is reconciled, never wiped: capabilities keep their ids (kits,
        compositions and designs reference them). `last_reconcile` holds the counts.

        Owner and repo become file names (`export-md`), so anything but a GitHub segment is a
        ValueError."""
        for label, seg in (("owner", owner), ("repo", repo)):
            if not schema.valid_segment(seg):
                raise ValueError(f"invalid {label} name for a dossier: {str(seg)[:60]!r}")
        summary = data.get("summary", "") or ""
        dep = data.get("dependencies")
        raw_lic = (dep.get("license", "") if isinstance(dep, dict) else "") or ""
        license_ = licenses.normalize(raw_lic)
        if isinstance(dep, dict) and dep.get("license") != license_:
            dep = {**dep, "license": license_}
            if str(raw_lic).strip() and str(raw_lic) != license_ and not dep.get("license_note"):
                dep["license_note"] = str(raw_lic).strip()
            data = {**data, "dependencies": dep}
        incoming = [c for c in (data.get("capabilities") or [])
                    if isinstance(c, dict) and c.get("name")]
        counts = {"new": 0, "updated": 0, "unchanged": 0, "retired": 0}
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM dossiers WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE"
                " AND sha=?", (owner, repo, sha)).fetchone()
            vals = (url, now(), model, float(cost_usd or 0.0), source, summary, license_,
                    _j(data))
            prev = None
            if row:
                did = row["id"]
                self.conn.execute(
                    "UPDATE dossiers SET url=?, comprehended_at=?, model=?, cost_usd=?, source=?,"
                    " summary=?, license=?, data_json=? WHERE id=?", (*vals, did))
            else:
                prev = self.conn.execute(
                    "SELECT id, owner, repo FROM dossiers WHERE owner=? COLLATE NOCASE"
                    " AND repo=? COLLATE NOCASE ORDER BY comprehended_at DESC, id DESC LIMIT 1",
                    (owner, repo)).fetchone()
                if prev:  # keep one display case per repo
                    owner, repo = prev["owner"], prev["repo"]
                cur = self.conn.execute(
                    "INSERT INTO dossiers(owner, repo, sha, url, comprehended_at, model, cost_usd,"
                    " source, summary, license, data_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (owner, repo, sha, *vals))
                did = cur.lastrowid
            ids = self._reconcile_capabilities(did, incoming, counts)
            for f in data.get("fit_to_projects") or []:
                if not isinstance(f, dict):
                    continue
                cid = ids.get(_name_key(f.get("capability", "")))
                slug = f.get("project", "")
                if cid and self.conn.execute(
                        "SELECT 1 FROM projects WHERE slug=?", (slug,)).fetchone():
                    # a re-ingest must not overwrite a fit the user or overlay already set
                    self.conn.execute(
                        "INSERT OR IGNORE INTO fits VALUES (?,?,?,?,?)",
                        (cid, slug, 0.8, f.get("how", ""), "dossier"))
            if prev:  # a new SHA: votes are user data, so carry them to same-named capabilities
                old = {}
                for r in self.conn.execute(
                        "SELECT id, name FROM capabilities WHERE dossier_id=? ORDER BY idx",
                        (prev["id"],)):
                    old.setdefault(_name_key(r["name"]), r["id"])
                for key, cid in ids.items():
                    if key in old:
                        self.conn.execute(
                            "INSERT OR IGNORE INTO votes SELECT ?, vote, updated FROM votes"
                            " WHERE capability_id=?", (cid, old[key]))
        self.last_reconcile = counts
        return self.get_dossier(did)  # type: ignore[return-value]

    def _reconcile_capabilities(self, did: int, incoming: list[dict], counts: dict) -> dict:
        """Match incoming capabilities to the dossier's stored ones by normalized name.

        Matched rows are updated in place (same id). Comprehension-owned columns follow the new
        extraction; enrichment columns are kept once filled, because the same SHA means the
        donor code is unchanged and paid Tier 2 / deepen output is still valid.
        Unmatched stored rows are retired, not deleted; a retired name that
        reappears is un-retired. Returns normalized name -> capability id."""
        old: dict[str, list] = {}
        for r in self.conn.execute(
                "SELECT * FROM capabilities WHERE dossier_id=? ORDER BY idx, id", (did,)):
            old.setdefault(_name_key(r["name"]), []).append(r)
        vocab = self.get_vocabulary()
        ids: dict[str, int] = {}
        for i, c in enumerate(incoming):
            key = _name_key(c["name"])
            fresh = {
                "idx": i, "name": c["name"], "what": c.get("what", ""),
                "files_json": _j(c.get("files") or []),
                "deps_json": _j(c.get("dependencies") or []),
                "portability": c.get("portability", "medium"),
                "portability_notes": c.get("portability_notes", ""),
                "security_notes": c.get("security_notes", "")}
            rated = ("provides" in c or "requires" in c)
            enrich = {
                "novelty": _novelty(c.get("novelty")),
                "acceptance_json": _j(schema.normalize_acceptance(c.get("acceptance"))),
                "provides_json": _j(vocab.canonicalize(
                    interfaces.normalize_list(c.get("provides")))),
                "requires_json": _j(vocab.canonicalize(
                    interfaces.normalize_list(c.get("requires")))),
                "data_shapes_json": _j(schema.normalize_data_shapes(c.get("data_shapes"))),
                "key_values_json": _j(schema.normalize_key_values(c.get("key_values")))}
            inc_basis = _clean_basis(c.get("basis"))
            match = old[key].pop(0) if old.get(key) else None
            if match is None:
                cols = {**fresh, **enrich, "dossier_id": did, "basis_json": _j(inc_basis)}
                if rated:
                    cols["interfaces_rated_at"] = now()
                cur = self.conn.execute(
                    f"INSERT INTO capabilities({', '.join(cols)})"
                    f" VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
                ids.setdefault(key, cur.lastrowid)
                counts["new"] += 1
                continue
            change = dict(fresh)
            if match["deepened_at"]:  # deepen rewrote `what` from the donor code; keep it
                del change["what"]
            for col, val in enrich.items():
                if _empty(match[col]):
                    change[col] = val
            if rated and not match["interfaces_rated_at"]:
                change["interfaces_rated_at"] = now()
            if inc_basis:  # a field group's basis follows the columns that were really written
                basis = _l(match["basis_json"], {})
                basis = dict(basis) if isinstance(basis, dict) else {}
                for group, cols in _BASIS_COLS.items():
                    if group in inc_basis and any(col in change for col in cols):
                        basis[group] = inc_basis[group]
                change["basis_json"] = _j(basis)
            change = {k: v for k, v in change.items() if match[k] != v}
            if match["retired_at"]:
                change["retired_at"] = ""
            if change:
                self.conn.execute(
                    f"UPDATE capabilities SET {', '.join(f'{k}=?' for k in change)} WHERE id=?",
                    (*change.values(), match["id"]))
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
            ids.setdefault(key, match["id"])
        for rows in old.values():
            for r in rows:
                if not r["retired_at"]:
                    self.conn.execute("UPDATE capabilities SET retired_at=? WHERE id=?",
                                      (now(), r["id"]))
                    counts["retired"] += 1
        return ids

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
                    "SELECT * FROM dossiers WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE"
                    " AND sha LIKE ? ORDER BY comprehended_at DESC, id DESC LIMIT 1",
                    (owner, repo, sha + "%")).fetchone()
            else:
                r = self.conn.execute(
                    "SELECT * FROM dossiers WHERE owner=? COLLATE NOCASE AND repo=? COLLATE NOCASE"
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
            novelty=r["novelty"] or "", acceptance=_l(r["acceptance_json"], []),
            confidence=r["confidence"], needs_review=bool(r["needs_review"]),
            data_shapes=_l(r["data_shapes_json"], []), key_values=_l(r["key_values_json"], []),
            quirks=_l(r["quirks_json"], []),
            interfaces_rated_at=r["interfaces_rated_at"] or "",
            deepened_at=r["deepened_at"] or "", deepened_sha=r["deepened_sha"] or "",
            deepened_model=r["deepened_model"] or "",
            basis=_clean_basis(_l(r["basis_json"], {})), retired_at=r["retired_at"] or "",
            donor=f"{r['_owner']}/{r['_repo']}", license=r["_license"] or "")

    def list_capabilities(self, *, dossier_id: int | None = None,
                          current_only=True, needs_review: bool | None = None,
                          include_retired=False) -> list[Capability]:
        where, args = ([] if include_retired else [_LIVE]), []
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
                              novelty=None, confidence=None, needs_review=None,
                              acceptance=None, what=None, data_shapes=None, key_values=None,
                              quirks=None, deepened=None, basis=None) -> bool:
        """Update interfaces / novelty / confidence / review flag / behavior text / shapes;
        tokens are normalized and canonicalized. None = keep (confidence < 0 clears it to NULL).
        `deepened` is {"at", "sha", "model"}. The FTS index follows `what` via the update
        trigger. `basis` ({field group: observed|derived|inferred|external}) is merged into the
        stored basis. Returns False when no capability has that id (nothing was updated)."""
        sets, args = [], []
        if what is not None:
            sets.append("what=?")
            args.append(str(what))
        if data_shapes is not None:
            sets.append("data_shapes_json=?")
            args.append(_j(schema.normalize_data_shapes(data_shapes)))
        if key_values is not None:
            sets.append("key_values_json=?")
            args.append(_j(schema.normalize_key_values(key_values)))
        if quirks is not None:
            sets.append("quirks_json=?")
            args.append(_j(schema.normalize_quirks(quirks)))
        if deepened is not None:
            sets.append("deepened_at=?, deepened_sha=?, deepened_model=?")
            args.extend([str(deepened.get("at", "")), str(deepened.get("sha", "")),
                         str(deepened.get("model", ""))])
        vocab = self.get_vocabulary() if (provides is not None or requires is not None) else None
        if vocab is not None:  # writing either side means interfaces were rated
            sets.append("interfaces_rated_at=?")
            args.append(now())
        if provides is not None:
            sets.append("provides_json=?")
            args.append(_j(vocab.canonicalize(interfaces.normalize_list(list(provides)))))
        if requires is not None:
            sets.append("requires_json=?")
            args.append(_j(vocab.canonicalize(interfaces.normalize_list(list(requires)))))
        if novelty is not None:
            sets.append("novelty=?")
            args.append(_novelty(novelty))
        if acceptance is not None:
            sets.append("acceptance_json=?")
            args.append(_j(schema.normalize_acceptance(acceptance)))
        if confidence is not None:  # a negative value clears it back to NULL (unrated)
            sets.append("confidence=?")
            args.append(float(confidence) if float(confidence) >= 0 else None)
        if needs_review is not None:
            sets.append("needs_review=?")
            args.append(1 if needs_review else 0)
        new_basis = _clean_basis(basis)
        if new_basis:
            row = self.conn.execute("SELECT basis_json FROM capabilities WHERE id=?",
                                    (capability_id,)).fetchone()
            if row is not None:
                sets.append("basis_json=?")
                args.append(_j({**_clean_basis(_l(row["basis_json"], {})), **new_basis}))
        if not sets:
            return True
        with self.conn:
            cur = self.conn.execute(f"UPDATE capabilities SET {', '.join(sets)} WHERE id=?",
                                    (*args, capability_id))
        return cur.rowcount > 0

    def search_capabilities(self, query: str, *, limit=50, current_only=True) -> list[Capability]:
        """FTS (or LIKE) search, best match first. See `search_matches`."""
        return [c for c, _ in self.search_matches(query, limit=limit, current_only=current_only)]

    def search_matches(self, query: str, *, limit=50,
                       current_only=True) -> list[tuple[Capability, list[str]]]:
        """(capability, matched query words) pairs. Stopwords and short tokens are dropped from
        the query; a row must match ceil(n/2) of the n remaining tokens (all when n <= 2); rows
        order by matched count, then FTS rank (LIKE fallback: id). `current_only` hides
        capabilities of superseded dossiers, so a repo re-comprehended at a newer SHA does not
        show up twice. `limit` None returns every qualifying row."""
        tokens = search_tokens(query)
        if not tokens:
            return []
        cur = f" AND {_CURRENT} AND {_LIVE}" if current_only else ""
        rows = None
        if self.fts:
            match = " OR ".join(f'"{t}"*' for t in tokens)
            try:
                rows = self.conn.execute(
                    _CAP_SELECT + " JOIN capabilities_fts ON capabilities_fts.rowid = c.id"
                    f" WHERE capabilities_fts MATCH ?{cur} ORDER BY capabilities_fts.rank",
                    (match,)).fetchall()
            except sqlite3.OperationalError:
                rows = None
        if rows is None:
            # no FTS5: scan the live rows; the Python pass below does the (accent-folding) match
            rows = self.conn.execute(
                _CAP_SELECT + f" WHERE 1=1{cur} ORDER BY c.id").fetchall()
        need = len(tokens) if len(tokens) <= 2 else -(-len(tokens) // 2)
        hits = []
        for r in rows:
            c = self._cap(r)
            words = _fold_words(f"{c.name} {c.what} {c.portability_notes}")
            got = [t for t in tokens if any(w.startswith(t) for w in words)]
            if len(got) >= need:
                hits.append((c, got))
        hits.sort(key=lambda h: -len(h[1]))  # stable: FTS rank breaks ties
        return hits if limit is None else hits[:limit]

    # -- fits / votes ---------------------------------------------------------------------
    def set_fit(self, capability_id, project_slug, score: float, how: str, source: str) -> None:
        if self.get_capability(capability_id) is None:
            raise ValueError(f"no such capability: {capability_id}")
        if self.get_project(project_slug) is None:
            raise ValueError(f"no such project: {project_slug}")
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
        if self.get_capability(capability_id) is None:
            raise ValueError(f"no such capability: {capability_id}")
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
        """Record a kit export; a re-export into the same path updates its row."""
        with self.conn:
            row = self.conn.execute("SELECT id FROM kits WHERE composition_id=? AND path=? "
                                    "ORDER BY id LIMIT 1", (composition_id, path)).fetchone()
            if row is not None:
                self.conn.execute("UPDATE kits SET created=? WHERE id=?", (now(), row["id"]))
                return row["id"]
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

    # -- jobs (the queue behind `reincarnate serve`) --------------------------------------
    _JOB_COLS = frozenset({"state", "stage", "stages_json", "started", "finished", "cost_usd",
                           "result_json", "error"})

    def add_job(self, kind: str, target: str, params_json: str, stages_json: str,
                stage: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs(kind, target, params_json, state, stage, stages_json, created,"
                " cost_usd, result_json, error) VALUES (?,?,?,'queued',?,?,?,0,'{}','')",
                (kind, target, params_json, stage, stages_json, now()))
        return int(cur.lastrowid)

    def get_job(self, job_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    def list_jobs(self, *, limit=100, states: tuple[str, ...] | None = None,
                  oldest_first=False) -> list[sqlite3.Row]:
        where, args = "", []
        if states:
            where = f"WHERE state IN ({','.join('?' * len(states))})"
            args = list(states)
        order = "ASC" if oldest_first else "DESC"
        return self.conn.execute(
            f"SELECT * FROM jobs {where} ORDER BY id {order} LIMIT ?", (*args, int(limit))
        ).fetchall()

    def update_job(self, job_id: int, *, only_if: tuple[str, ...] | None = None, **cols) -> bool:
        """Set job columns; with `only_if`, only while the job is in one of those states (the
        guard and the write are one statement, so a claim or a cancel cannot be lost)."""
        bad = set(cols) - self._JOB_COLS
        if bad or not cols:
            raise ValueError(f"cannot update job columns: {sorted(bad) or 'none given'}")
        sql = f"UPDATE jobs SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?"
        args = [*cols.values(), job_id]
        if only_if:
            sql += f" AND state IN ({','.join('?' * len(only_if))})"
            args += list(only_if)
        with self.conn:
            return self.conn.execute(sql, args).rowcount > 0
