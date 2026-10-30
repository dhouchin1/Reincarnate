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
    {"name": "a short human title in Title Case, not an identifier", "what": "", "files": [],
     "dependencies": [],
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
  "dependencies": {"license": "", "license_note": "", "notable": [], "supply_chain_flags": []},
  "security": {"concerns": [], "network_egress": "", "dangerous_patterns": []},
  "fit_to_projects": [{"project": "<profile project slug>", "capability": "<name>", "how": ""}],
  "salvage_recommendations": [],
  "unknowns": [{"question": "", "reason": ""}]
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
MAX_UNKNOWNS = 20
# how a capability field is known (Code Trace's evidence basis); keys of Capability.basis
BASIS = ("observed", "derived", "inferred", "external")
BASIS_FIELDS = ("what", "interfaces", "novelty", "acceptance", "data_shapes", "key_values")


def _str(v) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


# -- single-line donor-derived fields -------------------------------------------------------
# Names, stack items and paths come from donor repositories and end up in builder-facing markdown
# (headings, bullets, table cells, backtick spans). They are cleaned by shape at the boundary.
MAX_NAME = 80
MAX_STACK_ITEM = 40
MAX_FILE = 200
MAX_LICENSE = 80
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_LEAD_MARKER = re.compile(r"^(?:[#>\-*|]+|\[[ xX]\])\s*")
_STACK_OK = re.compile(r"^[A-Za-z0-9 .+#/_(),-]{1,40}$")


# a backtick span that looks like code rather than a word or an interface token
CODE_LIKE = re.compile(
    r"=|;|->|::|\w\s*\(.*\)|\w\[|\w\s*(?:[+*%<>&|^!]|\s-\s)\s*\w"
    r"|\b(?:import|def|function|return|const|class|var|let|require|export|async|await|lambda)\b"
    r"|\b(?:SELECT|INSERT|UPDATE|DELETE|DROP)\b"
    # a shell command line
    r"|^\s*(?:\$\s*)?(?:sudo|rm|curl|wget|chmod|chown|bash|sh|zsh|python3?|node|npm|npx|pip3?"
    r"|git|docker|make|cd|cat|echo|export|eval|kill|mv|cp|ssh|scp|nc)\s")
_SPAN = re.compile(r"`([^`\n]*)`")


def clean_line(v, n: int = MAX_NAME, *, backticks: bool = False) -> str:
    """One line of text: whitespace and control characters collapsed to single spaces, leading
    markdown markers (`#`, `>`, `-`, `*`, `|`, `[ ]`, `[x]`) stripped, capped at n characters;
    backticks removed when `backticks` is false (names must not open a code span)."""
    t = _CONTROL.sub(" ", _str(v))
    if not backticks:
        t = _SPAN.sub(lambda m: "[code removed]" if CODE_LIKE.search(m.group(1)) else m.group(0), t)
        t = t.replace("`", "")
    t = " ".join(t.split())
    while True:
        u = _LEAD_MARKER.sub("", t, count=1)
        if u == t:
            break
        t = u
    return t[:n].rstrip()


def clean_file(v) -> str:
    """A single relative path, or "" (backticks, `..` segments, absolute paths, empty)."""
    t = clean_line(v, MAX_FILE, backticks=True)
    if not t or "`" in t or t.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:[\\/]", t):
        return ""
    if ".." in re.split(r"[\\/]", t):
        return ""
    return t


_URL_USERINFO = re.compile(r"^(https?://)[^/@\s]*@", re.IGNORECASE)


def strip_url_credentials(url) -> str:
    """`https://user:token@host/x` -> `https://host/x`. Only http(s) userinfo is a secret; an
    scp-style `git@host:x` or `ssh://git@host/x` carries none and is returned as it is."""
    return _URL_USERINFO.sub(r"\1", _str(url).strip())


_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def valid_segment(v) -> bool:
    """A GitHub owner or repo name: safe as one file-name component."""
    s = _str(v)
    return bool(_SEGMENT.match(s)) and ".." not in s


def clean_stack_item(v) -> str:
    t = clean_line(v, MAX_STACK_ITEM)
    return t if _STACK_OK.match(t) else ""


def _stack_pieces(v) -> list[str]:
    """An item too long for one stack entry, as entries that each fit: "Go (stdlib os/exec,
    log/slog, crypto/rand)" becomes "Go", "stdlib os/exec", "log/slog", "crypto/rand" rather
    than a clipped "... crypto/ran". A piece still too long is cut at a word boundary."""
    t = " ".join(_str(v).split())
    if len(t) <= MAX_STACK_ITEM:
        return [t]
    m = re.fullmatch(r"([^()]*)\((.*)\)\s*(.*)", t)
    pieces = ([m.group(1), *re.split(r",|;| with | and ", m.group(2)), m.group(3)] if m
              else [t])
    out = []
    for x in (p.strip() for p in pieces):
        if len(x) > MAX_STACK_ITEM:
            x = x[:MAX_STACK_ITEM + 1].rsplit(" ", 1)[0].rstrip(" ,(")
        if x:
            out.append(x)
    return out


def clean_stack(items) -> list[str]:
    out: list[str] = []
    for x in items or []:
        for piece in _stack_pieces(x):
            t = clean_stack_item(piece)
            if t and t not in out:
                out.append(t)
    return out


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


MAX_QUIRKS = 8


def normalize_quirks(v) -> list[str]:
    """Donor quirks: non-empty stripped strings, at most MAX_QUIRKS, each truncated."""
    if not isinstance(v, (list, tuple)):
        return []
    out = [t for t in (_cap_str(x, MAX_ACCEPTANCE_CHARS) for x in v) if t]
    return out[:MAX_QUIRKS]


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


def normalize_unknowns(v) -> list[dict]:
    """At most MAX_UNKNOWNS {question, reason} pairs with a question; non-list -> []."""
    out: list[dict] = []
    if not isinstance(v, (list, tuple)):
        return out
    for u in v:
        if isinstance(u, str):
            u = {"question": u}
        if not isinstance(u, dict):
            continue
        q = _cap_str(u.get("question"), 300)
        if q:
            out.append({"question": q, "reason": _cap_str(u.get("reason"), 300)})
        if len(out) >= MAX_UNKNOWNS:
            break
    return out


def unknown_coverage(scope: str, reason: str, facet: str = "files") -> dict:
    """Coverage for a dossier whose reading was not recorded: `unknown` means not examined."""
    return {"status": "unknown", "scope": scope,
            "facets": {facet: {"status": "unknown", "reason": reason}}, "omitted": []}


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
    out["languages"] = clean_stack(_strlist(out["languages"]))
    out["entry_points"] = _strlist(out["entry_points"])
    out["salvage_recommendations"] = _strlist(out["salvage_recommendations"])
    caps = []
    for c in out["capabilities"]:
        if not isinstance(c, dict) or not _str(c.get("name")).strip():
            continue
        nc = dict(c)
        for k in _CAP_STR:
            nc[k] = _str(c.get(k))
        nc["name"] = clean_line(c.get("name"))
        if not nc["name"]:
            continue
        for k in _CAP_LIST:
            nc[k] = _strlist(c.get(k))
        nc["files"] = [f for f in dict.fromkeys(clean_file(x) for x in nc["files"]) if f]
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
    out["unknowns"] = normalize_unknowns(src.get("unknowns"))
    fits = []
    for f in out["fit_to_projects"]:
        if isinstance(f, dict):
            fits.append({"project": clean_line(f.get("project")),
                         "capability": clean_line(f.get("capability")),
                         "how": _str(f.get("how"))})
    out["fit_to_projects"] = fits
    dep = src.get("dependencies") if isinstance(src.get("dependencies"), dict) else {}
    out["dependencies"] = {**dep, "license": clean_line(dep.get("license"), 600),
                           "license_note": _str(dep.get("license_note")),
                           "notable": _strlist(dep.get("notable")),
                           "supply_chain_flags": _strlist(dep.get("supply_chain_flags"))}
    sec = src.get("security") if isinstance(src.get("security"), dict) else {}
    out["security"] = {**sec, "concerns": _strlist(sec.get("concerns")),
                       "network_egress": _str(sec.get("network_egress")),
                       "dangerous_patterns": _strlist(sec.get("dangerous_patterns"))}
    return out


def clip_words(text: str, n: int = 600) -> str:
    """`text` cut at a word boundary to at most n characters, with an ellipsis when cut."""
    text = str(text or "")
    if len(text) <= n:
        return text
    cut = text[:n - 1]
    if text[n - 1].isspace():
        return cut.rstrip() + "\u2026"
    head, sep, _ = cut.rpartition(" ")
    return (head if sep and head else cut).rstrip() + "\u2026"


_FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.DOTALL)


def json_fences(text: str) -> list[str]:
    """Bodies of the ```json fenced blocks in `text`, in order."""
    return [b for t, b in _FENCE.findall(text or "") if t.lower() == "json"]


def _scan(text: str, expect: type):
    """The largest top-level value of type `expect` that decodes anywhere in `text` (ties go to
    the later one). Positions inside an already-decoded value are skipped, so a nested object
    never competes with the one holding it; a brace that does not decode is just prose."""
    open_ch = "{" if expect is dict else "["
    dec = json.JSONDecoder(strict=False)
    best, best_len = None, -1
    i = text.find(open_ch)
    while i != -1:
        try:
            val, end = dec.raw_decode(text, i)
        except ValueError:
            i = text.find(open_ch, i + 1)
            continue
        if isinstance(val, expect) and end - i >= best_len:
            best, best_len = val, end - i
        i = text.find(open_ch, end)
    return best


def extract_json(text: str, expect: type = dict):
    """The JSON value of type `expect` (dict or list) in a model reply, or None.

    Order: the last ```json fenced block, then earlier json blocks, then other fenced blocks
    (last first), then the whole text. Inside each, `_scan` takes the largest (later on a tie)
    top-level value of the expected type, so trailing prose, leading prose with stray braces
    and raw newlines inside strings are tolerated."""
    if not text or expect not in (dict, list):
        return None
    blocks = _FENCE.findall(text)
    ordered = ([b for t, b in reversed(blocks) if t.lower() == "json"]
               + [b for t, b in reversed(blocks) if t.lower() != "json"])
    for body in ordered:
        val = _scan(body, expect)
        if val is not None:
            return val
    return _scan(text, expect)


def extract_dossier_json(text: str) -> dict | None:
    return extract_json(text, dict)


def blocking_problems(d: dict) -> list[str]:
    """What stops a dossier from being stored: any validate_dossier problem (no summary, no
    capabilities, ...). Shared by `ingest` and headless comprehension."""
    return validate_dossier(d)
