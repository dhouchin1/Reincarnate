"""The kit contracts and scope pass: one model call that fixes what the steps of a kit agree on.

Reads catalog text only (never donor files, never deepen) and returns the API style, who may do
what, the settings, and for each step its scope, what it provides to later steps and what it uses
from earlier ones. The reply is validated before anything is used; a pass that fails falls back
to the plain kit and the export never fails because of it. The result is cached in the kit
manifest by a hash of the input, so a re-export with no change makes no new call."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from . import claude_runner, paths, salvage, schema
from .prompts import UNTRUSTED_DATA_RULE

MODEL = "claude-sonnet-5-5"
WHAT_CAP = 600
MAX_CHECKS = 4
MAX_ITEMS = 8
MAX_SETTINGS = 40
MAX_DECISIONS = 6
MAX_FIELDS = 25
MAX_SIGNATURES = 6
MAX_PROPS = 30
MAX_EVENTS = 10
API_STYLE_CAP = 1500
LINE_CAP = 400
NAME_CAP = 200
MEANING_CAP = 300
PROVIDE_KINDS = ("route", "record", "module", "setting")
_FIELD_ROW = '[{"name": "", "type": "", "meaning": ""}]'
_PROP_ROW = '[{"name": "", "type": "", "meaning": "", "required": true}]'
SCHEMA_HINT = (
    '{"api_style": "", "access_model": [""], "settings": [{"key": "", "step": 0, "meaning": ""}], '
    '"steps": [{"n": 0, "in_scope": [""], "out_of_scope": [""], "provides": [{"name": "", '
    '"kind": "route|record|module|setting", "token": "", "consumers": [0], "fields": {"request": '
    + _FIELD_ROW + ', "response": ' + _FIELD_ROW + ', "errors": [""], "fields": ' + _FIELD_ROW
    + ', "signature": "", "props": ' + _PROP_ROW + ', "events": [{"name": "", "payload": '
    + _FIELD_ROW + '}], "state": ' + _FIELD_ROW + ', "type": "", "default": ""}}], '
    '"uses": [{"name": "", "from": 0}], "checks": [""], "library_note": ""}], '
    '"open_decisions": [{"id": "D1", "question": "", "why": "", "options": ["", ""], '
    '"default": "", "steps": [0]}]}')
CONTRACTS_LINE = ("Contracts between steps (API style, who may do what, open decisions and their "
                  "answers, settings, what each step provides and uses) are in CONTRACTS.md; "
                  "follow them exactly.")
# bump when the prompt or the reply's shape changes: old caches go stale
PROMPT_REV = "defaults-1"


def _as_int(v):
    if isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clip(text, n: int) -> str:
    """Clipped at a word boundary."""
    t = " ".join(str(text or "").split())
    if len(t) <= n:
        return t
    cut = t[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n // 2 else cut).rstrip(" ,;:") + "..."


def _gate_plan_text(text) -> str:
    """The air-gap gate for the pass's own text. The model wrote it from gated catalog text, so
    identifiers (`current_user`, `require_admin(request)`), route paths and JSON shapes in
    backtick spans stay; fenced blocks, donor paths and code-host URLs still go."""
    text = str(text or "").replace("\x01", "")
    parked: list[str] = []

    def span(m: re.Match) -> str:
        body = m.group(1)
        if salvage._plain(body, ()) != body:
            return salvage.DONOR_FILE
        parked.append(m.group(0))
        return f"\x01{len(parked) - 1}\x01"

    text = salvage._SPAN.sub(span, salvage._FENCE_BLOCK.sub(salvage.CODE_REMOVED, text))
    text = salvage.gate_text(text)
    while "\x01" in text:  # restore until none is left, as salvage does for parked URLs
        text = re.sub("\x01(\\d+)\x01", lambda m: parked[int(m.group(1))], text)
    return text


def _plan_text(text, n: int) -> str:
    """Gated free text on one line, cut at a word boundary."""
    return schema.clip_words(" ".join(_gate_plan_text(text).split()), n)


def _plan_line(text, n: int = NAME_CAP) -> str:
    """A name or short line for a heading, bullet or table cell: no backticks, gated, one line."""
    t = schema.clean_line(_gate_plan_text(schema.clean_line(text, 2000, backticks=True)), 2000,
                          backticks=True).replace("`", "")
    return schema.clip_words(" ".join(t.split()), n)


def _tokens(cap, attr: str, defs: dict) -> list[dict]:
    out = []
    for t in dict.fromkeys(str(x) for x in getattr(cap, attr, None) or []):
        out.append({"token": salvage.gate_line(t, n=60),
                    "meaning": salvage.gate_line(defs.get(t, {}).get("definition"), n=200)})
    return out


def _shapes(cap) -> list[dict]:
    out = []
    for s in getattr(cap, "data_shapes", None) or []:
        if not isinstance(s, dict) or not str(s.get("name") or "").strip():
            continue
        fields = []
        for f in s.get("fields") or []:
            if isinstance(f, dict) and str(f.get("name") or "").strip():
                typ = salvage.gate_line(f.get("type"), n=30)
                fields.append(salvage.gate_line(f["name"], n=40) + (f" ({typ})" if typ else ""))
        out.append({"name": salvage.gate_line(s["name"], n=60), "fields": fields[:20]})
    return out[:6]


def wiring_links(steps: list[dict], proposal: dict) -> list[dict]:
    """The kit's provider -> consumer links as `{token, provider, consumer}` step numbers: the
    design's wired links between kit parts, and the build-fresh steps that provide a token to
    the parts needing it."""
    by_cid = {st["cap"].id: st["n"] for st in steps if st["kind"] == "cap"}
    links: list[dict] = []
    seen: set[tuple] = set()

    def add(tok, prov, cons):
        key = (str(tok), prov, cons)
        if tok and prov is not None and cons is not None and prov != cons and key not in seen:
            seen.add(key)
            links.append({"token": str(tok), "provider": prov, "consumer": cons})

    for w in (proposal.get("wiring") or {}).get("wired") or []:
        if isinstance(w, dict):
            add(w.get("token"), by_cid.get((w.get("provider") or {}).get("id")),
                by_cid.get((w.get("consumer") or {}).get("id")))
    for st in steps:
        if st["kind"] != "gap" or not st["gap"].get("token"):
            continue
        gap = st["gap"]
        add(gap["token"], st["n"], by_cid.get(_as_int(gap.get("consumer_capability_id"))))
        for a in gap.get("also_consumers") or []:
            if isinstance(a, dict):
                add(gap["token"], st["n"], by_cid.get(_as_int(a.get("capability_id"))))
    return links


def _coverage(steps: list[dict], proposal: dict) -> list[dict]:
    texts = {r.get("id"): str(r.get("text") or "") for r in proposal.get("requirements") or []
             if isinstance(r, dict)}
    by_cap = {f"c{st['cap'].id}": st["n"] for st in steps if st["kind"] == "cap"}
    glue = {st["req"]: st["n"] for st in steps if st["kind"] == "glue"}
    covered: dict[str, list[int]] = {}
    for row in proposal.get("coverage") or []:
        if not isinstance(row, dict):
            continue
        req = str(row.get("req") or "")
        n = by_cap.get(row.get("cap")) if row.get("kind") == "catalog" else glue.get(req)
        covered.setdefault(req, [])
        if n is not None and n not in covered[req]:
            covered[req].append(n)
    return [{"id": salvage.gate_line(r, n=40),
             "text": _clip(salvage.gate_text(texts.get(r) or r), 300), "steps": ns}
            for r, ns in covered.items()]


def _security(store, cap, files: list[str]) -> list[str]:
    """The security requirements the cap's spec will carry: its security notes and the dossier
    concerns that apply to it (read-only, the same rules as salvage.build_clean_room_spec)."""
    out = []
    note = salvage.gate_text((cap.security_notes or "").strip(), files)
    if note:
        out.append(note)
    dossier = store.get_dossier(cap.dossier_id) if cap.dossier_id else None
    security = ((dossier.data or {}).get("security") if dossier else None) or {}
    for concern in security.get("concerns") or [] if isinstance(security, dict) else []:
        if salvage.concern_applies(str(concern), cap.name or "", cap.what or ""):
            c = salvage.gate_text(f"Avoid: {str(concern).strip()}", files)
            if c not in out:
                out.append(c)
    return [_clip(x, 400) for x in out][:6]


def _step_requirements(st: dict, proposal: dict, files: list[str]) -> list[str]:
    texts = {r.get("id"): str(r.get("text") or "") for r in proposal.get("requirements") or []
             if isinstance(r, dict)}
    out = []
    for row in proposal.get("coverage") or []:
        if (isinstance(row, dict) and row.get("kind") == "catalog"
                and row.get("cap") == f"c{st['cap'].id}"):
            t = _clip(salvage.gate_text(texts.get(row.get("req")) or "", files), 300)
            if t and t not in out:
                out.append(t)
    return out


def _key_values(cap, files: list[str]) -> list[str]:
    out = []
    for kv in getattr(cap, "key_values", None) or []:
        if isinstance(kv, dict) and str(kv.get("name") or "").strip():
            out.append(_clip(salvage.gate_text(f"{kv.get('name')}: {kv.get('value', '')}", files),
                             160))
    return out[:8]


def _step_input(store, st: dict, proposal: dict, defs: dict) -> dict:
    item: dict = {"n": st["n"], "kind": st["kind"]}
    if st["kind"] == "cap":
        cap = st["cap"]
        files = [f for f in getattr(cap, "files", None) or [] if f]
        item.update(part=salvage.gate_line(cap.name, files), role=salvage.gate_line(
            st.get("role"), n=200), what=_clip(salvage.gate_text(cap.what, files), WHAT_CAP),
            provides=_tokens(cap, "provides", defs), requires=_tokens(cap, "requires", defs),
            data_shapes=_shapes(cap), requirements=_step_requirements(st, proposal, files),
            key_values=_key_values(cap, files), security=_security(store, cap, files),
            checks=[_clip(salvage.gate_text(c, files), 200) for c in
                    (getattr(cap, "acceptance", None) or [])[:6] if str(c).strip()])
    elif st["kind"] == "glue":
        item.update(requirement=st["req"], what=_clip(st["text"], WHAT_CAP))
    elif st["kind"] == "gap":
        gap = st["gap"]
        tok = str(gap.get("token") or "")
        item.update(need=_clip(salvage.gate_text(gap.get("need")), 300))
        if tok:
            item.update(token=salvage.gate_line(tok, n=60),
                        meaning=salvage.gate_line(defs.get(tok, {}).get("definition"), n=200))
    else:
        item.update(what="scaffold of the new project: manifests, skeleton, test runner")
    return item


def _answered(decisions) -> list[dict]:
    """The answered decisions as plain gated data `[{id, question, answer}]`; unanswered ones
    and malformed rows are left out."""
    out = []
    for d in decisions if isinstance(decisions, list) else []:
        if not isinstance(d, dict) or not str(d.get("answer") or "").strip():
            continue
        out.append({"id": _plan_line(d.get("id"), 20),
                    "question": _plan_text(d.get("question"), LINE_CAP),
                    "answer": _plan_text(d.get("answer"), LINE_CAP)})
    return out


def build_input(store, steps: list[dict], proposal: dict, policy: dict, decisions=None) -> dict:
    """The pass's whole input as plain data: catalog text only, and the answered decisions when
    the pass runs a second time (phase 2)."""
    defs = store.get_vocabulary().tokens
    answered = _answered(decisions)
    return {**({"decisions": answered} if answered else {}),
        "target": {"kind": policy.get("kind"), "stack": list(policy.get("stack") or []),
                   "test_command": policy.get("test_cmd") or ""},
        "steps": [_step_input(store, st, proposal, defs) for st in steps],
        "requirements": _coverage(steps, proposal),
        "wiring": wiring_links(steps, proposal),
    }


def cache_key(data: dict, model: str) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n" + model + "\n" + PROMPT_REV
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


DECISIONS_RULE = """
- These decisions are final. The contracts must reflect them: access/error lists (a public \
route has no UNAUTHORIZED), fields and settings a decision implies (for example a per-item \
opt-in field, a secrets-file setting, a trusted-proxy setting), and nothing that contradicts \
them. Return an empty open_decisions list."""


def build_prompt(data: dict) -> str:
    return f"""\
You are the planner of a software kit: an ordered list of build steps, each built later by a \
separate session that sees only its own step and a contracts file. Your job is to fix what the \
steps agree on, so nobody has to guess. {UNTRUSTED_DATA_RULE}

Rules:
- api_style: one paragraph, fixed once for the whole kit and fitting the target's stack (for \
example for FastAPI: REST + JSON under /api, an error shape, and one auth guard signature). \
Name concrete conventions, not options.
- access_model: who may view, edit and administer, as short sentences derived from the \
requirements. Do not invent roles the requirements do not imply.
- settings: each config key a step owns, with the step number and what it means.
- For each step (use the step numbers given, including step 0 when present):
  - in_scope: the behaviors to build, bounded by the step's role and the requirements it covers.
  - out_of_scope: behaviors the part's description mentions that this kit does not need (for \
example a description with LDAP and OIDC when the role is local sign-in).
  - provides: concrete things later steps rely on, named exactly (route paths, record names, \
module names, setting keys), each with its kind and the consumer step numbers (consumers are \
LATER steps only). Every wired token a step provides (see wiring) must be covered by a provides \
entry: set its "token" to the wired token it realizes. Give each entry its exact "fields", \
consistent with the requirements, the decisions to come and the data shapes in the input: for a \
route (method and path stay in the name) "request" and "response" as lists of {{name, type, \
meaning}} and "errors" as the closed list of error codes; for a record "fields" as a list of \
{{name, type, meaning}}; for a module "signature", one line "name(args) -> result" per exported \
function, at most 6; for a module that is a UI component (a PascalCase name, or a part providing \
a ui: token) also "props" as {{name, type, meaning, required}} (at most 30, with item shapes \
spelled out in the type, for example tiles: Array<{{id, x, y, w, h}}>, and the settings it takes), \
"events" as {{name, payload: [{{name, type, meaning}}]}} (at most 10; every callback with what it \
is called with) and, when it owns state, "state" as {{name, type, meaning}}; for a setting "type" \
and a concrete "default" fit for a local single-user install (a relative path under the \
data directory, a loopback URL, a conservative limit). At most 25 fields per list. Outside \
settings, use [value unknown: ...] rather than inventing a value.
  - uses: what the step calls from EARLIER steps, by the exact name in that step's provides, \
with that step's number in "from".
  - checks: 2-4 acceptance checks, only for glue and build-fresh steps, taken from the \
requirement text. Never invent numbers, limits or timings: write [value unknown: ...] instead.
  - library_note: only when the part's description suggests a library outside the target's \
stack; say to use the stack's own equivalent.
- open_decisions: at most 6 product decisions the requirements leave open, each one that (a) \
changes behavior, the data model, access or scope, (b) is not settled by the idea, the \
requirements or the part's role, and (c) a builder would otherwise have to guess (for example \
whether anonymous visitors may view a board). Not technical choices the builder can make \
(library, file layout). Each has an id "D1", "D2", ..., the question, why (what depends on it), \
2-4 concrete options, the default (one of the options, the safest and simplest), and the step \
numbers it affects. Return an empty list when nothing is open.
- Conflicts are decisions: each cap step carries its security requirements (security), its \
requirements, key_values and checks. When a security requirement conflicts with a requirement, a \
key value or a check of the same step (for example "tolerate self-signed certificates" against \
"do not disable TLS verification"), emit an open decision that names both sides, with options \
(for example "Verify certificates always", "Per-tile opt-in to skip verification, off by \
default", "Skip verification for all tiles") and the security-preferring option as the default.
- Never copy file paths, code, URLs or credentials from the data.
- Field names: use one naming convention for record and route fields, state it in api_style, and \
keep every field exactly in it. Use only libraries of the target's stack or the standard library; \
name no other library.{DECISIONS_RULE if "decisions" in data else ""}

Kit data (JSON):
{json.dumps(data, indent=1, ensure_ascii=False)}

Return ONLY a JSON object, no prose:
{SCHEMA_HINT}"""


def _strs(v, n: int, cap: int = MAX_ITEMS) -> list[str]:
    out = []
    for x in v if isinstance(v, list) else []:
        t = _plan_line(x, n) if isinstance(x, (str, int, float)) else ""
        if t and t not in out:
            out.append(t)
    return out[:cap]


def _field_list(v) -> list[dict]:
    out: list[dict] = []
    for f in v if isinstance(v, list) else []:
        f = {"name": f} if isinstance(f, (str, int)) and not isinstance(f, bool) else f
        name = _plan_line(f.get("name"), 80) if isinstance(f, dict) else ""
        if name and not any(x["name"] == name for x in out):
            out.append({"name": name, "type": _plan_line(f.get("type"), 60),
                        "meaning": _plan_line(f.get("meaning"), MEANING_CAP)})
    return out[:MAX_FIELDS]


def _scalar(v, n: int) -> str:
    return _plan_line(str(v).lower() if isinstance(v, bool) else v, n)


_COMPONENT = re.compile(r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+(?=$|[\s(<{:])")


def is_component(name: str, token: str = "") -> bool:
    """A module that reads as a UI component: a PascalCase name or a `ui:` token."""
    return bool(_COMPONENT.match(str(name).strip())) or str(token).strip().startswith("ui:")


def _props(v) -> list[dict]:
    out: list[dict] = []
    for f in v if isinstance(v, list) else []:
        name = _plan_line(f.get("name"), 80) if isinstance(f, dict) else ""
        if name and not any(x["name"] == name for x in out):
            req = f.get("required")
            out.append({"name": name, "type": _plan_line(f.get("type"), NAME_CAP),
                        "meaning": _plan_line(f.get("meaning"), MEANING_CAP),
                        "required": req if isinstance(req, bool)
                        else str(req).strip().lower() in ("true", "yes", "required")})
    return out[:MAX_PROPS]


def _events(v) -> list[dict]:
    out: list[dict] = []
    for e in v if isinstance(v, list) else []:
        name = _plan_line(e.get("name"), 80) if isinstance(e, dict) else ""
        if name and not any(x["name"] == name for x in out):
            out.append({"name": name, "payload": _field_list(e.get("payload"))})
    return out[:MAX_EVENTS]


def _validate_fields(kind: str, raw, component: bool = False) -> dict:
    """The entry's `fields` for its kind; malformed parts are dropped, never the entry."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    if kind == "route":
        for key in ("request", "response"):
            rows = _field_list(raw.get(key))
            if rows:
                out[key] = rows
        errors = _strs(raw.get("errors"), 40, MAX_FIELDS)
        if errors:
            out["errors"] = errors
    elif kind == "record":
        rows = _field_list(raw.get("fields"))
        if rows:
            out["fields"] = rows
    elif kind == "module":
        sig = raw.get("signature")
        lines = sig if isinstance(sig, list) else str(sig or "").splitlines()
        sigs = _strs([x for x in lines if isinstance(x, str)], MEANING_CAP, MAX_SIGNATURES)
        if sigs:
            out["signature"] = "\n".join(sigs)
        if component:
            for key, rows in (("props", _props(raw.get("props"))),
                              ("events", _events(raw.get("events"))),
                              ("state", _field_list(raw.get("state")))):
                if rows:
                    out[key] = rows
    else:
        if _scalar(raw.get("type"), 60):
            out["type"] = _scalar(raw.get("type"), 60)
        if raw.get("default") not in (None, ""):
            out["default"] = setting_default(raw.get("default"))
    return out


# A setting's default is a value, not a donor reference: a loopback URL or a data path (no source
# file extension, not under src/ or an absolute home) stays; the gate still takes anything else.
_LOOPBACK_URL = re.compile(r"https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?(?:/[\w./-]*)?")
_DATA_PATH = re.compile(r"(?:~/|\./|\.\./)?[\w.-]+(?:/[\w.-]+)*/?")
_SOURCE_EXT = re.compile(r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|rb|php|c|h|cpp|cs|swift|"
                         r"vue|svelte|sh)$", re.IGNORECASE)
_MARKER = re.compile(r"\[(?:value unknown[^\]]*|path removed|url removed|code removed)\]", re.I)
NO_DEFAULT = ("none given; choose a conventional default, keep it a setting and state it in the "
              "README")


def setting_default(v) -> str:
    """A setting's default for CONTRACTS.md. A default the gate or the model left as a marker
    ("[value unknown: ...]", "[path removed]") becomes an explicit instruction instead."""
    raw = " ".join(str(v).lower() if isinstance(v, bool) else str(v or "").split()).strip("` ")
    if _LOOPBACK_URL.fullmatch(raw) or (
            _DATA_PATH.fullmatch(raw) and "/" in raw and not _SOURCE_EXT.search(raw)
            and not re.match(r"(?:~/)?src/", raw)):
        return raw[:120]
    text = _scalar(v, 120)
    return NO_DEFAULT if not text or _MARKER.fullmatch(text.strip()) else text


def _covers(entry: dict, token: str) -> bool:
    tok = token.casefold()
    return any(str(p.get("token") or "").casefold() == tok or tok in p["name"].casefold()
               for p in entry["provides"])


def _words_of(name: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"\{[^}]*\}", " ", name.casefold()))
    return {w[:-1] if len(w) > 3 and w.endswith("s") else w for w in words}


def _provided_as(name: str, provides: list[dict]) -> str | None:
    """The provides entry a `uses` name refers to, under the provider's own wording: the same
    name, or one whose words contain all of the use's words ("require_admin guard" is in
    "current_user / require_user / require_admin guards"). The model words the two sides
    separately, so exact matching dropped good references."""
    for p in provides:
        if p["name"].casefold() == name.casefold():
            return p["name"]
    want = _words_of(name)
    hits = [p["name"] for p in provides if want and want <= _words_of(p["name"])]
    return hits[0] if len(hits) == 1 else None


def _norm_q(text) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").casefold()))


def _validate_decisions(raw, kinds: dict, problems: list[str]) -> list[dict]:
    out: list[dict] = []
    for d in raw if isinstance(raw, list) else []:
        if len(out) >= MAX_DECISIONS:
            problems.append(f"more than {MAX_DECISIONS} open decisions; the rest dropped")
            break
        if not isinstance(d, dict):
            continue
        did = str(d.get("id") or "").strip().upper()
        question = _plan_line(d.get("question"), LINE_CAP)
        options = _strs(d.get("options"), NAME_CAP, 4)
        default = _plan_line(d.get("default"), NAME_CAP)
        default = next((o for o in options if o.casefold() == default.casefold()), "")
        steps = [_as_int(x) for x in d["steps"]] if isinstance(d.get("steps"), list) else []
        why = None
        if not re.fullmatch(r"D\d+", did) or any(x["id"] == did for x in out):
            why = "a missing or repeated id"
        elif not question:
            why = "no question"
        elif len(options) < 2 or not default:
            why = "fewer than two options or a default that is not one of them"
        elif not steps or any(x not in kinds for x in steps):
            why = "no step or a step out of range"
        if why:
            problems.append(f"open decision {did or '?'} dropped: {why}")
            continue
        out.append({"id": did, "question": question, "why": _plan_line(d.get("why"), LINE_CAP),
                    "options": options, "default": default, "steps": sorted(set(steps))})
    return out


def validate(raw, steps: list[dict], wiring: list[dict],
             decided: bool = False) -> tuple[dict | None, list[str]]:
    """(plan, problems): the reply cleaned and gated. Entries for unknown steps are dropped; a
    `uses` pointing to a later step or to a name that step does not provide is dropped; a wired
    token no `provides` entry covers is a problem. None when nothing usable is left. With
    `decided` (phase 2) the reply's open decisions are ignored."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["the plan is not a JSON object"]
    kinds = {st["n"]: st["kind"] for st in steps}
    entries: dict[int, dict] = {}
    for s in raw.get("steps") if isinstance(raw.get("steps"), list) else []:
        n = _as_int(s.get("n")) if isinstance(s, dict) else None
        if n not in kinds:
            was = s.get("n") if isinstance(s, dict) else s
            problems.append(f"plan entry for unknown step {was!r} dropped")
            continue
        if n in entries:
            continue
        provides = []
        for p in s.get("provides") if isinstance(s.get("provides"), list) else []:
            name = _plan_line(p.get("name")) if isinstance(p, dict) else ""
            if not name or any(q["name"] == name for q in provides):
                continue
            kind = str(p.get("kind") or "").strip().lower()
            kind = kind if kind in PROVIDE_KINDS else "module"
            cons = []
            for x in p.get("consumers") if isinstance(p.get("consumers"), list) else []:
                c = _as_int(x)
                if c not in kinds:
                    continue
                if c <= n:
                    problems.append(f"step {n} provides {name!r} to step {c}, which is not "
                                    "later; consumer dropped")
                else:
                    cons.append(c)
            provides.append({"name": name, "kind": kind,
                             "token": _plan_line(p.get("token"), 60),
                             "consumers": sorted(set(cons)),
                             "fields": _validate_fields(
                                 kind, p.get("fields"),
                                 kind == "module" and is_component(name, p.get("token")))})
        entries[n] = {
            "n": n,
            "in_scope": _strs(s.get("in_scope"), LINE_CAP),
            "out_of_scope": _strs(s.get("out_of_scope"), LINE_CAP),
            "provides": provides[:20], "uses": [],
            "checks": (_strs(s.get("checks"), LINE_CAP, MAX_CHECKS)
                       if kinds[n] in ("glue", "gap") else []),
            "library_note": _plan_line(s.get("library_note"), LINE_CAP)}
        entries[n]["_uses_raw"] = s.get("uses") if isinstance(s.get("uses"), list) else []
    for n in sorted(entries):
        e = entries[n]
        for u in e.pop("_uses_raw"):
            name = _plan_line(u.get("name")) if isinstance(u, dict) else ""
            src = _as_int(u.get("from")) if isinstance(u, dict) else None
            if not name:
                continue
            if src is None or src not in entries or src >= n:
                problems.append(f"step {n} uses {name!r} from "
                                f"{'a later or unknown step' if src is not None else 'no step'}"
                                "; dropped")
            else:
                match = _provided_as(name, entries[src]["provides"])
                if match is None:
                    problems.append(f"step {n} uses {name!r}, which step {src} does not "
                                    "provide; dropped")
                elif not any(x["name"] == match and x["from"] == src for x in e["uses"]):
                    e["uses"].append({"name": match, "from": src})
    for n in sorted(entries):  # a named consumer needs the matching `uses`, added if missing
        for p in entries[n]["provides"]:
            for c in p["consumers"]:
                if c in entries and not any(u["name"] == p["name"] and u["from"] == n
                                            for u in entries[c]["uses"]):
                    entries[c]["uses"].append({"name": p["name"], "from": n})
    for w in wiring:
        prov = entries.get(w["provider"])
        if prov is None or not _covers(prov, w["token"]):
            problems.append(f"wired token {w['token']} from step {w['provider']} is not covered "
                            f"by any provides entry of that step (step {w['consumer']} needs it)")
    settings = []
    for x in raw.get("settings") if isinstance(raw.get("settings"), list) else []:
        key = _plan_line(x.get("key")) if isinstance(x, dict) else ""
        if key:
            step = _as_int(x.get("step"))
            settings.append({"key": key, "step": step if step in kinds else None,
                             "meaning": _plan_line(x.get("meaning"), MEANING_CAP)})
    if not entries:
        return None, problems + ["the plan has no usable steps"]
    plan = {"api_style": _plan_text(raw.get("api_style"), API_STYLE_CAP),
            "access_model": _strs(raw.get("access_model"), LINE_CAP, 12),
            "settings": settings[:MAX_SETTINGS],
            "steps": [entries[n] for n in sorted(entries)],
            "open_decisions": ([] if decided else
                                _validate_decisions(raw.get("open_decisions"), kinds, problems))}
    return plan, problems


_TLS_TOPIC = re.compile(r"\b(?:tls|ssl|https|certificates?|certs?|self-?signed|insecure)\b",
                        re.IGNORECASE)
CONFLICT_OPTIONS = ["Always enforce", "Opt-in per item, off by default", "Allow it everywhere"]


def _conflict_decisions(pl: dict, data: dict, problems: list[str]) -> None:
    """Backstop for a model that missed it: a cap step whose security note endorses insecure TLS
    behavior while its requirements, key values or checks ask for that same behavior gets an
    open decision (default: the opt-in), unless an existing decision already covers the topic."""
    decisions = pl["open_decisions"]
    for item in data.get("steps") or []:
        sec = [x for x in item.get("security") or []
               if salvage.is_insecure_check(x) and _TLS_TOPIC.search(x)]
        wanted = " ".join(str(x) for k in ("requirements", "key_values", "checks")
                          for x in item.get(k) or [])
        if not sec or not _TLS_TOPIC.search(wanted):
            continue
        if any(_TLS_TOPIC.search(" ".join([d["question"], d["why"], *d["options"]]))
               for d in decisions):
            continue
        if len(decisions) >= MAX_DECISIONS:
            gone = decisions.pop()
            problems.append(f"open decision {gone['id']} dropped to make room for a conflict "
                            f"between step {item['n']}'s security note and its requirements")
        used = {d["id"] for d in decisions}
        did = next(f"D{i}" for i in range(1, MAX_DECISIONS + 20) if f"D{i}" not in used)
        part = item.get("part") or f"Step {item['n']}"
        decisions.append({
            "id": did, "steps": [item["n"]], "options": list(CONFLICT_OPTIONS),
            "default": CONFLICT_OPTIONS[1],
            "question": _plan_line(f"{part}: its requirements or values ask to tolerate "
                                   "unverified or self-signed certificates, but its security "
                                   "notes forbid turning TLS verification off. Which wins?",
                                   LINE_CAP),
            "why": "The requirement and the security note cannot both be met; this decides "
                   "whether certificate verification can ever be skipped."})


def _default_runner(prompt: str, model: str):
    with tempfile.TemporaryDirectory(prefix="reincarnate-plan-") as tmp:
        return claude_runner.run(prompt, cwd=Path(tmp), model=model, max_turns=1, tools="",
                                 timeout=900, label="kit contracts pass")


def plan(store, steps: list[dict], proposal: dict, policy: dict, *, model: str = MODEL,
         runner=None, cache: dict | None = None, decisions: list[dict] | None = None) -> dict:
    """Run the pass, or reuse `cache` (the manifest's `plan` entry) when its key matches. Never
    raises. Returns `{plan, problems, key, model, cost_usd, spent, cached, reason,
    unavailable}`: `plan` is None when unusable (`reason` says why; `unavailable` is true when
    claude itself is missing), `spent` is what this call cost, `cost_usd` what the stored plan
    cost in all. `runner(prompt, model)` returns a `claude_runner.RunResult`. `decisions`
    (`[{id, question, answer}]`, the answered ones) makes this phase 2: the contracts must reflect
    them, the reply's open decisions are ignored and the cache key covers the answers."""
    out = {"plan": None, "problems": [], "key": "", "model": model, "cost_usd": 0.0,
           "spent": 0.0, "cached": False, "reason": "", "unavailable": False}
    try:
        data = build_input(store, steps, proposal, policy, decisions)
    except Exception as e:  # noqa: BLE001 - the pass never blocks the export
        return {**out, "reason": f"could not build the input: {e}"[:300]}
    out["key"] = key = cache_key(data, model)
    if not (isinstance(cache, dict) and cache.get("key") == key):
        cache = _read_cache(key)  # a preview (which writes no kit) or another kit made it
    if (isinstance(cache, dict) and cache.get("key") == key
            and isinstance(cache.get("data"), dict) and cache["data"].get("steps")):
        try:
            cost = float(cache.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return {**out, "plan": cache["data"], "cached": True, "cost_usd": cost}
    try:
        res = (runner or _default_runner)(build_prompt(data), model)
    except Exception as e:  # noqa: BLE001
        return {**out, "reason": f"model call failed: {type(e).__name__}: {e}"[:300]}
    spent = float(getattr(res, "cost_usd", 0.0) or 0.0)
    out.update(spent=spent, cost_usd=spent)
    if res.failed:
        return {**out, "reason": f"model call failed: {res.error_message()}",
                "unavailable": res.rc == 127}
    raw = schema.extract_json(res.text, dict)
    if raw is None:
        return {**out, "reason": "no JSON object in the model's reply: "
                f"{res.text.strip()[:120]}".strip()}
    decided = bool(data.get("decisions"))
    pl, problems = validate(raw, steps, wiring_links(steps, proposal), decided)
    if pl is None:
        return {**out, "problems": problems, "reason": "; ".join(problems)[:300]}
    if not decided:
        _conflict_decisions(pl, data, problems)
    _write_cache(key, {"key": key, "model": model, "cost_usd": spent, "data": pl})
    return {**out, "plan": pl, "problems": problems}


def _cache_file(key: str) -> Path:
    return paths.data_dir() / "plans" / f"{key}.json"


def _read_cache(key: str) -> dict | None:
    """The validated plan for this exact input, from the data folder, so the studio's dry-run
    previews and repeated exports pay for the pass once."""
    try:
        return json.loads(_cache_file(key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_cache(key: str, entry: dict) -> None:
    try:
        f = paths.ensure(_cache_file(key).parent) / f"{key}.json"
        f.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # a cache that cannot be written only costs a repeat call


# -- rendering -----------------------------------------------------------------------------
def _entry(pl: dict, n: int) -> dict | None:
    return next((e for e in pl["steps"] if e["n"] == n), None)


def _label(labels: dict, n) -> str:
    return f"step {n}" + (f" ({labels[n]})" if labels.get(n) else "")


def _consumers(p: dict, labels: dict) -> str:
    return ", ".join(_label(labels, c) for c in p["consumers"]) or "no later step named"


def _fmt_fields(rows: list[dict]) -> str:
    return "; ".join(f"`{f['name']}`" + (f" ({f['type']})" if f.get("type") else "")
                     + (f": {f['meaning']}" if f.get("meaning") else "") for f in rows)


def field_lines(p: dict) -> list[str]:
    """A provided entry's fields as short lines (empty when it has none)."""
    f = p.get("fields") or {}
    out = [f"{label}: {_fmt_fields(f[key])}" for key, label in
           (("request", "Request"), ("response", "Response"), ("fields", "Fields")) if f.get(key)]
    if f.get("errors"):
        out.append("Errors: " + ", ".join(f["errors"]))
    out += [f"Signature: `{x}`" for x in str(f.get("signature") or "").splitlines()]
    if f.get("props"):
        out.append("Props: " + "; ".join(
            f"`{x['name']}`" + (f" ({x['type']}{', required' if x.get('required') else ''})"
                                if x.get("type") else (" (required)" if x.get("required") else ""))
            + (f": {x['meaning']}" if x.get("meaning") else "") for x in f["props"]))
    if f.get("events"):
        out.append("Events: " + "; ".join(
            f"`{x['name']}`" + (" called with " + _fmt_fields(x["payload"]) if x["payload"]
                                else " (no payload)") for x in f["events"]))
    if f.get("state"):
        out.append("State: " + _fmt_fields(f["state"]))
    if f.get("type") or f.get("default"):
        out.append("; ".join(x for x in (f"Type: {f['type']}" if f.get("type") else "",
                                         f"Default: {f['default']}" if f.get("default") else "")
                             if x))
    return out


def _used_fields(pl: dict, u: dict) -> list[str]:
    prov = _entry(pl, u["from"])
    p = next((x for x in prov["provides"] if x["name"] == u["name"]), None) if prov else None
    return field_lines(p) if p else []


def has_uses(pl: dict, n: int) -> bool:
    e = _entry(pl, n)
    return bool(e and e["uses"])


def resolve_decisions(pl: dict | None, stored) -> list[dict]:
    """The plan's open decisions with their saved answers (`answer`, `source`, `answered_at`;
    None when unanswered). An answer counts only while the id and the question text still match
    what was answered, so a re-plan that rewords a question asks it again."""
    stored = stored if isinstance(stored, dict) else {}
    out = []
    for d in (pl or {}).get("open_decisions") or []:
        s = stored.get(d["id"])
        ok = (isinstance(s, dict) and str(s.get("answer") or "").strip()
              and _norm_q(s.get("question")) == _norm_q(d["question"]))
        out.append({**d, "answer": str(s["answer"]) if ok else None,
                    "source": str(s.get("source") or "user") if ok else None,
                    "answered_at": str(s.get("answered_at") or "") if ok else ""})
    return out


def decision_line(d: dict) -> str:
    if d.get("answer"):
        return f"**{d['id']}. {d['question']}** Decided: {d['answer']}"
    return (f"**{d['id']}. {d['question']}** Not decided; build with the default: "
            f"{d['default']}. (Ask the user before changing it.)")


def step_notes(pl: dict, n: int, kind: str, labels: dict, decisions=()) -> list[str]:
    """Markdown lines for step n's spec (the builders' `notes`): scope, what it provides and
    uses, the library note, the open decisions that touch it (`decisions`, from
    `resolve_decisions`) and, for glue and build-fresh steps, the plan's checks."""
    own = [d for d in decisions if n in d["steps"]]
    tail = ["**Decisions that apply:**", "", *[f"- {decision_line(d)}" for d in own]] if own else []
    e = _entry(pl, n)
    if e is None:
        return tail
    out: list[str] = []
    if e["in_scope"] or e["out_of_scope"]:
        out += ["", "**Scope:**", ""]
        if e["in_scope"]:
            out += ["In scope:", "", *[f"- {x}" for x in e["in_scope"]], ""]
        if e["out_of_scope"]:
            out += ["Out of scope (do not build, even if a description mentions it):", "",
                    *[f"- {x}" for x in e["out_of_scope"]], ""]
    if e["provides"]:
        out += ["**Provides to later steps** (named exactly; later steps rely on these):", "",
                *[x for p in e["provides"]
                  for x in [f"- `{p['name']}` ({p['kind']}): used by {_consumers(p, labels)}",
                            *[f"  - {y}" for y in field_lines(p)]]], ""]
    if e["uses"]:
        out += ["**Uses from earlier steps:**", "",
                *[x for u in e["uses"]
                  for x in [f"- `{u['name']}` from {_label(labels, u['from'])}",
                            *[f"  - {y}" for y in _used_fields(pl, u)]]], ""]
    if e["library_note"]:
        out += [f"**Library note:** {e['library_note']}", ""]
    if e["checks"] and kind in ("glue", "gap"):
        out += ["**Acceptance checks (from the requirement):**", "",
                *[f"- {c}" for c in e["checks"]], ""]
    while out and not out[-1]:
        out.pop()
    return [*out, *([""] if out and tail else []), *tail]


def contracts_md(pl: dict, labels: dict, banner: str = "", decisions=()) -> str:
    def cell(s) -> str:
        return str(s).replace("|", "\\|").replace("\n", " ")

    lines = ["# Contracts between steps", ""]
    if banner:
        lines += [banner, ""]
    lines += ["Follow these exactly. A step may add to them; it must not rename or reshape them.",
              "", "## API style", "", pl["api_style"] or "(none given)", "",
              "## Who may do what", ""]
    lines += [f"- {x}" for x in pl["access_model"]] or ["(none given)"]
    if decisions:
        lines += ["", "## Decisions", ""]
        lines += [f"- {decision_line(d)}" for d in decisions]
    lines += ["", "## Settings", ""]
    if pl["settings"]:
        lines += ["| Key | Owned by | Meaning |", "| --- | --- | --- |"]
        for s in pl["settings"]:
            owner = _label(labels, s["step"]) if s["step"] is not None else "-"
            lines.append(f"| `{cell(s['key'])}` | {owner} | {cell(s['meaning'])} |")
    else:
        lines.append("(none)")
    for e in pl["steps"]:
        head = _label(labels, e["n"])
        lines += ["", f"## {head[0].upper()}{head[1:]}", ""]
        if e["provides"]:
            lines += ["| Provides | Kind | Used by |", "| --- | --- | --- |"]
            lines += [f"| `{cell(p['name'])}` | {p['kind']} | {cell(_consumers(p, labels))} |"
                      for p in e["provides"]]
            shaped = [p for p in e["provides"] if field_lines(p)]
            if shaped:
                lines += ["", "Shapes:", ""]
                for p in shaped:
                    lines += [f"- `{p['name']}`", *[f"  - {y}" for y in field_lines(p)]]
        else:
            lines.append("Provides: nothing later steps rely on.")
        lines.append("")
        if e["uses"]:
            lines += ["| Uses | From |", "| --- | --- |"]
            lines += [f"| `{cell(u['name'])}` | {_label(labels, u['from'])} |" for u in e["uses"]]
        else:
            lines.append("Uses: nothing from earlier steps.")
    return "\n".join(lines) + "\n"


# -- lint ----------------------------------------------------------------------------------
_CONVENTIONS = (
    ("snake_case", re.compile(r"snake[_ -]?case", re.I),
     re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")),
    ("camelCase", re.compile(r"camel[_ -]?case", re.I),
     re.compile(r"[a-z][a-z0-9]*(?:[A-Z][a-z0-9]*)*")),
    ("kebab-case", re.compile(r"kebab[_ -]?case", re.I),
     re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")),
)


def _style(name: str) -> str:
    if "_" in name:
        return "snake_case"
    if "-" in name:
        return "kebab-case"
    return "PascalCase" if name[:1].isupper() else "camelCase"


def _claimed_convention(pl: dict) -> tuple[str, re.Pattern] | None:
    """The one naming convention CONTRACTS claims for fields or tables; None when it claims none
    or more than one (for example snake_case columns and camelCase JSON)."""
    text = " ".join([pl.get("api_style") or "", *(pl.get("access_model") or [])])
    hits = [(n, rx) for n, claim, rx in _CONVENTIONS if claim.search(text)]
    return hits[0] if len(hits) == 1 else None


def _data_field_names(e: dict) -> list[str]:
    """Field names of records and route request/response lists (not props, events or state)."""
    out: list[str] = []
    for p in e["provides"]:
        f = p.get("fields") or {}
        for key in ("request", "response", "fields"):
            for row in f.get(key) or []:
                for part in re.split(r"[^A-Za-z0-9_-]+", row["name"]):
                    if part and not part[0].isdigit() and part not in out:
                        out.append(part)
    return out


# Libraries a spec may name from habit, by ecosystem, with the standard-library way when known.
_LIBRARIES = {
    "bcrypt": "hashlib.scrypt", "passlib": "hashlib.scrypt", "argon2-cffi": "hashlib.scrypt",
    "argon2": "hashlib.scrypt", "pyjwt": "hmac with hashlib", "jose": "hmac with hashlib",
    "requests": "urllib.request", "httpx": "urllib.request", "sqlalchemy": "sqlite3",
    "pydantic": "dataclasses", "flask-login": "", "itsdangerous": "hmac with hashlib",
    "react-grid-layout": "", "dnd-kit": "", "axios": "fetch", "lodash": "built-in array methods",
    "jsonwebtoken": "node:crypto createHmac", "bcryptjs": "node:crypto scrypt",
    "moment": "Intl.DateTimeFormat", "uuid": "crypto.randomUUID", "express-session": "",
}
# Too common as plain words: counted only in backticks or after an install/import verb.
_AMBIGUOUS = {"requests", "moment", "jose", "uuid", "argon2"}
# What the stack word brings along with it.
_STACK_BRINGS = {
    "fastapi": {"httpx", "pydantic", "starlette", "uvicorn"}, "starlette": {"httpx"},
    "react": {"react-dom"}, "pytest": {"httpx"}, "flask": {"werkzeug", "itsdangerous"},
    "sqlmodel": {"pydantic", "sqlalchemy"},
}
_LIB_RX = re.compile(r"(?<![\w-])(" + "|".join(re.escape(x) for x in sorted(_LIBRARIES, key=len,
                     reverse=True)) + r")(?![\w-])", re.I)
_LIB_CONTEXT = re.compile(r"`$|(?:import|install|pip|npm|using|library|package)\s+$", re.I)


def _allowed_libs(stack) -> set[str]:
    allowed: set[str] = set()
    for item in stack or []:
        for w in re.findall(r"[a-z0-9][a-z0-9.+-]*", str(item).casefold()):
            allowed.add(w)
            allowed |= _STACK_BRINGS.get(w, set())
    return allowed


def _libraries_in(text: str) -> list[str]:
    found: list[str] = []
    for m in _LIB_RX.finditer(text or ""):
        lib = m.group(1).casefold()
        if lib in _AMBIGUOUS and not _LIB_CONTEXT.search(text[max(0, m.start() - 12):m.start()]):
            continue
        if lib not in found:
            found.append(lib)
    return found


def _entry_texts(e: dict) -> list[str]:
    """The plan text of a step a library name could be read from (not the out-of-scope list or
    the library note, which exist to say what not to use)."""
    out = list(e["in_scope"])
    for p in e["provides"]:
        out.append(p["name"])
        f = p.get("fields") or {}
        for key in ("request", "response", "fields", "props", "state"):
            out += [f"{r['name']} {r.get('type', '')} {r.get('meaning', '')}"
                    for r in f.get(key) or []]
        out.append(str(f.get("signature") or ""))
    return out


def lint(pl: dict, policy: dict, spec_texts: dict[int, str] | None = None) -> list[dict]:
    """Deterministic checks on the plan and the step specs, as `[{step, message}]` (step is None
    when not about one step): fields that break the naming convention CONTRACTS claims, and
    libraries named outside the target's stack. The messages are instructions to the builder."""
    out: list[dict] = []
    claim = _claimed_convention(pl)
    if claim:
        name, rx = claim
        for e in pl["steps"]:
            bad = [x for x in _data_field_names(e) if not rx.fullmatch(x)]
            if bad:
                styles = " / ".join(sorted({_style(x) for x in bad}))
                shown = ", ".join(f"`{x}`" for x in bad[:10]) + (", ..." if len(bad) > 10 else "")
                out.append({"step": e["n"], "message": (
                    f"CONTRACTS says {name} but these fields are {styles}: {shown}; use the "
                    "field names exactly as listed.")})
    stack = (policy or {}).get("stack") or []
    if stack:
        allowed = _allowed_libs(stack)
        texts: dict = {None: [pl.get("api_style") or "", *(pl.get("access_model") or []),
                              *[s["meaning"] for s in pl["settings"]]]}
        for e in pl["steps"]:
            texts[e["n"]] = _entry_texts(e)
        for n, t in (spec_texts or {}).items():
            texts.setdefault(n, []).append(str(t))
        for n, parts in texts.items():
            for lib in _libraries_in("\n".join(parts)):
                if lib in allowed:
                    continue
                alt = _LIBRARIES[lib]
                hint = f" (for example {alt})" if alt else ""
                out.append({"step": n, "message": (
                    f"Spec mentions {lib}, which is not in the allowed set: use the standard "
                    f"library or the stack{hint} or log it in DEPENDENCIES.md with its reason.")})
    return out
