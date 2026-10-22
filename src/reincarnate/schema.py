"""Dossier JSON schema hint, validation, normalization, and extraction from model output."""

from __future__ import annotations

import json
import re

SCHEMA_HINT = """{
  "summary": "one or two sentences",
  "what_it_does": "markdown prose",
  "languages": ["most-used first"],
  "build_system": "",
  "entry_points": ["path - what it is"],
  "architecture": "markdown prose",
  "capabilities": [
    {"name": "", "what": "", "files": [], "dependencies": [],
     "portability": "high|medium|low", "portability_notes": "", "security_notes": "",
     "provides": ["kind:thing, 0-6 interface tokens this capability offers"],
     "requires": ["kind:thing, 0-6 interface tokens it needs from other parts or the environment"],
     "novelty": "commodity|pattern|distinctive",
     "acceptance": ["2-4 observable input-to-outcome checks a test can verify; behavior only"],
     "data_shapes": [{"name": "", "fields": [{"name": "", "type": "", "meaning": ""}],
                      "notes": "each persisted or exchanged structure, field by field"}],
     "key_values": [{"name": "", "value": "",
                     "meaning": "a concrete size, limit, threshold, timeout or format"}]}
  ],
  "dependencies": {"license": "", "notable": [], "supply_chain_flags": []},
  "security": {"concerns": [], "network_egress": "", "dangerous_patterns": []},
  "fit_to_projects": [{"project": "<profile project slug>", "capability": "<name>", "how": ""}],
  "salvage_recommendations": []
}"""

PORTABILITY = ("high", "medium", "low")
_STR_KEYS = ("summary", "what_it_does", "build_system", "architecture")
_LIST_KEYS = ("languages", "entry_points", "capabilities", "fit_to_projects",
              "salvage_recommendations")
_CAP_STR = ("name", "what", "portability_notes", "security_notes")
_CAP_LIST = ("files", "dependencies")
NOVELTY = ("commodity", "pattern", "distinctive")
MAX_TOKENS = 6
MAX_ACCEPTANCE = 5
MAX_ACCEPTANCE_CHARS = 600
MAX_SHAPES = 8
MAX_SHAPE_FIELDS = 40
MAX_KEY_VALUES = 20


def _str(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _list(v) -> list:
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _strlist(v) -> list[str]:
    return [_str(x) for x in _list(v)]


def _tokens(v) -> list[str]:
    """Lowercased, stripped, deduped strings, at most MAX_TOKENS; non-list -> []."""
    out: list[str] = []
    if not isinstance(v, (list, tuple)):
        return out
    for x in v:
        t = _str(x).strip().lower()
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TOKENS:
            break
    return out


def normalize_acceptance(v) -> list[str]:
    """Non-empty stripped strings, at most MAX_ACCEPTANCE, each truncated; non-list -> []."""
    out: list[str] = []
    if not isinstance(v, (list, tuple)):
        return out
    for x in v:
        t = _cap_str(x, MAX_ACCEPTANCE_CHARS)
        if t:
            out.append(t)
        if len(out) >= MAX_ACCEPTANCE:
            break
    return out


TRUNCATED = " [truncated]"


def _cap_str(v, n: int) -> str:
    """Strip and cap at n chars. An over-long string is cut at a word boundary and marked, so a
    builder never reads a silently half-finished check or value (v0.4 cut URLs and checks)."""
    t = _str(v).strip()
    if len(t) <= n:
        return t
    cut = t[:max(1, n - len(TRUNCATED))]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:-") + TRUNCATED


def normalize_data_shapes(v) -> list[dict]:
    """At most MAX_SHAPES named shapes of at most MAX_SHAPE_FIELDS named fields, strings
    stripped and capped; anything malformed is dropped, non-list -> []."""
    out: list[dict] = []
    if not isinstance(v, (list, tuple)):
        return out
    for s in v:
        if not isinstance(s, dict):
            continue
        name = _cap_str(s.get("name"), 80)
        if not name:
            continue
        fields = []
        raw = s.get("fields")
        for f in raw if isinstance(raw, (list, tuple)) else []:
            if not isinstance(f, dict):
                continue
            fname = _cap_str(f.get("name"), 80)
            if not fname:
                continue
            fields.append({"name": fname, "type": _cap_str(f.get("type"), 80),
                           "meaning": _cap_str(f.get("meaning"), 400)})
            if len(fields) >= MAX_SHAPE_FIELDS:
                break
        out.append({"name": name, "fields": fields, "notes": _cap_str(s.get("notes"), 400)})
        if len(out) >= MAX_SHAPES:
            break
    return out


def normalize_key_values(v) -> list[dict]:
    """At most MAX_KEY_VALUES entries with a name and a value; non-list -> []."""
    out: list[dict] = []
    if not isinstance(v, (list, tuple)):
        return out
    for e in v:
        if not isinstance(e, dict):
            continue
        name, value = _cap_str(e.get("name"), 120), _cap_str(e.get("value"), 300)
        if not name or not value:
            continue
        out.append({"name": name, "value": value, "meaning": _cap_str(e.get("meaning"), 400)})
        if len(out) >= MAX_KEY_VALUES:
            break
    return out


def validate_dossier(d: dict) -> list[str]:
    """Return a list of problems; empty means ok."""
    if not isinstance(d, dict):
        return ["dossier must be a JSON object"]
    problems: list[str] = []
    if not _str(d.get("summary")).strip():
        problems.append("summary is missing or empty")
    caps = d.get("capabilities")
    if not isinstance(caps, list):
        problems.append("capabilities must be a list")
        caps = []
    elif not caps:
        problems.append("capabilities is empty")
    for i, c in enumerate(caps):
        if not isinstance(c, dict):
            problems.append(f"capabilities[{i}] must be an object")
            continue
        if not _str(c.get("name")).strip():
            problems.append(f"capabilities[{i}].name is missing")
        if c.get("portability") not in PORTABILITY:
            problems.append(f"capabilities[{i}].portability must be one of high|medium|low")
    for key in ("dependencies", "security"):
        if key in d and not isinstance(d[key], dict):
            problems.append(f"{key} must be an object")
    return problems


def normalize_dossier(d: dict) -> dict:
    """Return a new dossier dict with all keys present and types coerced."""
    src = dict(d) if isinstance(d, dict) else {}
    if "fit_to_fleet" in src:
        fleet = src.pop("fit_to_fleet")
        if not src.get("fit_to_projects"):
            src["fit_to_projects"] = fleet
    out = dict(src)
    for k in _STR_KEYS:
        out[k] = _str(src.get(k))
    for k in _LIST_KEYS:
        out[k] = _list(src.get(k))
    out["languages"] = _strlist(out["languages"])
    out["entry_points"] = _strlist(out["entry_points"])
    out["salvage_recommendations"] = _strlist(out["salvage_recommendations"])
    caps = []
    for c in out["capabilities"]:
        if not isinstance(c, dict) or not _str(c.get("name")).strip():
            continue
        nc = dict(c)
        for k in _CAP_STR:
            nc[k] = _str(c.get(k))
        for k in _CAP_LIST:
            nc[k] = _strlist(c.get(k))
        p = _str(c.get("portability")).strip().lower()
        nc["portability"] = p if p in PORTABILITY else "medium"
        nc["provides"] = _tokens(c.get("provides"))
        nc["requires"] = _tokens(c.get("requires"))
        n = _str(c.get("novelty")).strip().lower()
        nc["novelty"] = n if n in NOVELTY else ""
        nc["acceptance"] = normalize_acceptance(c.get("acceptance"))
        nc["data_shapes"] = normalize_data_shapes(c.get("data_shapes"))
        nc["key_values"] = normalize_key_values(c.get("key_values"))
        caps.append(nc)
    out["capabilities"] = caps
    fits = []
    for f in out["fit_to_projects"]:
        if isinstance(f, dict):
            fits.append({"project": _str(f.get("project")), "capability": _str(f.get("capability")),
                         "how": _str(f.get("how"))})
    out["fit_to_projects"] = fits
    dep = src.get("dependencies") if isinstance(src.get("dependencies"), dict) else {}
    out["dependencies"] = {**dep, "license": _str(dep.get("license")),
                           "notable": _strlist(dep.get("notable")),
                           "supply_chain_flags": _strlist(dep.get("supply_chain_flags"))}
    sec = src.get("security") if isinstance(src.get("security"), dict) else {}
    out["security"] = {**sec, "concerns": _strlist(sec.get("concerns")),
                       "network_egress": _str(sec.get("network_egress")),
                       "dangerous_patterns": _strlist(sec.get("dangerous_patterns"))}
    return out


_FENCE = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_dossier_json(text: str) -> dict | None:
    """Parse the LAST ```json fenced block in text, or the whole text if bare JSON."""
    if not text:
        return None
    for body in reversed(_FENCE.findall(text)):
        try:
            obj = json.loads(body)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        obj = json.loads(text.strip())
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    # bare JSON followed by trailing prose
    start = text.find("{")
    if start >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[start:])
            return obj if isinstance(obj, dict) else None
        except ValueError:
            return None
    return None
