"""The kit reconcile pass: one model call that makes every spec agree with its scope, the answered
decisions and CONTRACTS.md.

The specs' acceptance checks and key values come from the donors, so they keep behavior the plan
put out of scope, contradict an answered decision, or name things differently from the contracts.
This pass reads catalog text only (the checks and values the specs would render, the plan and the
answers) and returns, per step, which checks and key values to keep, rewrite or drop, plus a
concrete default for each setting a decision now settles. The reply is validated before anything
is used; a pass that fails leaves the kit as it was and the export never fails because of it. The
result is cached in the kit manifest and the data folder by a hash of the input."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

from . import claude_runner, paths, salvage, schema
from .prompts import UNTRUSTED_DATA_RULE

MODEL = "claude-sonnet-5-5"
TEXT_CAP = 400
PROSE_CAP = 2000
SCHEMA_HINT = (
    '{"steps": [{"n": 0, "checks": [{"i": 0, "action": "keep|rewrite|drop", "text": "", '
    '"reason": ""}], "key_values": [{"i": 0, "action": "keep|rewrite|drop", "value": "", '
    '"reason": ""}], "prose": [{"i": 0, "action": "keep|rewrite|drop", "text": "", '
    '"reason": ""}]}], "settings": [{"key": "", "default": "", "reason": ""}]}')
PROSE_LABELS = {"what": "behavior description", "design_notes": "design notes",
                "intent": "integration intent"}
PROMPT_REV = "reconcile-2"  # bump when the prompt or the reply's shape changes: old caches go stale
HEADING = "**Changed to match the decisions and contracts:**"
_UNKNOWN = re.compile(r"\s*\[value unknown[^\]]*\]", re.IGNORECASE)


def _clip(text, n: int = TEXT_CAP) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n].rstrip(" ,;:") + "..."


def _line(text, n: int = TEXT_CAP) -> str:
    """One gated line (URLs kept: a check may name a route or a host)."""
    return _clip(salvage.gate_text(" ".join(str(text or "").split()), keep_urls=True), n)


def _strs(v, n: int = 200) -> list[str]:
    return [t for t in (salvage.gate_line(x, n=n) for x in v if isinstance(x, (str, int, float)))
            if t] if isinstance(v, list) else []


def _fields(item) -> list[str]:
    """The field names a provides/uses entry carries (slice A adds them; absent is fine)."""
    out = []
    for f in item.get("fields") or [] if isinstance(item, dict) else []:
        name = f.get("name") if isinstance(f, dict) else f
        t = salvage.gate_line(name, n=60) if isinstance(name, (str, int, float)) else ""
        if t:
            out.append(t)
    return out[:20]


def _contract_items(items) -> list[dict]:
    out = []
    for x in items or []:
        row = {"name": salvage.gate_line(x.get("name"), n=120)}
        if x.get("kind"):
            row["kind"] = str(x["kind"])
        if x.get("from") is not None:
            row["from"] = x["from"]
        if _fields(x):
            row["fields"] = _fields(x)
        out.append(row)
    return out


def build_input(store, steps: list[dict], plan: dict, decisions: list[dict], proposal: dict,
                lists: dict) -> dict:
    """The pass's whole input as plain data: catalog text only. `lists` maps a step number to
    `{"checks": [str], "key_values": [dict], "prose": [{kind, text}]}`, exactly what that step's
    spec would render (`prose` is the behavior text, design notes and integration intent)."""
    entries = {e["n"]: e for e in plan.get("steps") or []}
    out_steps = []
    for st in steps:
        n = st["n"]
        e = entries.get(n) or {}
        own = lists.get(n) or {}
        shapes = []
        if st["kind"] == "cap":
            shapes = [salvage.gate_line(s.get("name"), n=60)
                      for s in getattr(st["cap"], "data_shapes", None) or []
                      if isinstance(s, dict) and str(s.get("name") or "").strip()]
        out_steps.append({
            "n": n, "kind": st["kind"],
            "checks": [{"i": i, "text": _line(c)} for i, c in enumerate(own.get("checks") or [])],
            "key_values": [{"i": i, "name": _line(kv.get("name"), 80), "value": _line(
                kv.get("value"), 200)} for i, kv in enumerate(own.get("key_values") or [])
                if isinstance(kv, dict)],
            "prose": [{"i": i, "kind": p["kind"], "text": _line(p["text"], PROSE_CAP)}
                      for i, p in enumerate(own.get("prose") or [])],
            "data_shapes": shapes[:6],
            "in_scope": _strs(e.get("in_scope"), 300), "out_of_scope": _strs(
                e.get("out_of_scope"), 300),
            "provides": _contract_items(e.get("provides")),
            "uses": _contract_items(e.get("uses"))})
    return {
        "api_style": _clip(plan.get("api_style"), 600),
        "access_model": _strs(plan.get("access_model"), 300),
        "settings": [{"key": salvage.gate_line(s.get("key"), n=80), "step": s.get("step"),
                      "meaning": salvage.gate_line(s.get("meaning"), n=200)}
                     for s in plan.get("settings") or []],
        "decisions": [{"id": d["id"], "question": salvage.gate_line(d["question"], n=300),
                       "answer": salvage.gate_line(d["answer"], n=300)}
                      for d in decisions or [] if d.get("answer")],
        "steps": out_steps}


def has_work(data: dict) -> bool:
    return (any(s["checks"] or s["key_values"] or s["prose"] for s in data["steps"])
            or bool(data["settings"]))


def cache_key(data: dict, model: str) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False) + "\n" + model + "\n" + PROMPT_REV
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_prompt(data: dict) -> str:
    return f"""\
You are the editor of a software kit: an ordered list of build steps, each built later by a \
separate session that reads only its own spec and a contracts file. The specs' acceptance \
checks, key values and behavior descriptions were taken from other projects, so some no longer \
fit this kit. Make them consistent with the kit's scope, its answered decisions and its \
contracts. {UNTRUSTED_DATA_RULE}

Rules:
- For every check and key value of every step choose keep, rewrite or drop. Use the index "i" \
given in the data.
- A check or value that conflicts with the step's out_of_scope list, an answered decision or the \
contracts (API style, the names a step provides or uses, the access model) must be rewritten to \
match, or dropped when the behavior it tests is out of scope. Prefer rewrite when the behavior \
stays relevant; drop only what no longer belongs.
- Examples: a check about renaming boards under the decision "single board" is dropped; a \
session lifetime of 30 days under the decision "7 days" is rewritten to 7 days; a cookie named \
differently from the contract's name is rewritten to the contract's name; a status list \
that counts 302 as online under a decision of 200-299 is rewritten.
- Never invent new behavior, new checks or new values. A rewrite changes only what conflicts \
and keeps the rest of the wording. Keep concrete numbers unless a decision changes them.
- A rewrite gives the full new text ("text" for a check, "value" for a key value). Every drop \
and rewrite gives a short "reason" naming the scope item, decision or contract behind it.
- prose is the step's behavior description, design notes and integration intent, in order ("kind" \
says which). Choose keep, rewrite or drop for each by its "i". Rewrite one that describes behavior \
the step's out_of_scope list, a decision or the contracts rule out, so it matches the scope, the \
decisions and the contracts' names (remove the out-of-scope behavior, use the contract's names). \
Keep every concrete detail that is still in scope. Never add behavior. Drop a paragraph only when \
all of it is out of scope. A rewrite gives the full new text.
- Leave out entries you keep (they stay as they are).
- settings: for each setting whose value an answered decision now determines, give its "key" \
exactly as listed, a concrete "default" and a "reason". Leave out the others.
- Never copy file paths, code or credentials from the data.

Kit data (JSON):
{json.dumps(data, indent=1, ensure_ascii=False)}

Return ONLY a JSON object, no prose:
{SCHEMA_HINT}"""


def _entries(raw, count: int, field: str, problems: list[str], n: int) -> list[dict]:
    """Validated non-keep entries for one list of `count` items."""
    out: list[dict] = []
    seen: set[int] = set()
    text_key = "value" if field == "key value" else "text"
    for x in raw if isinstance(raw, list) else []:
        i = x.get("i") if isinstance(x, dict) else None
        if isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < count:
            problems.append(f"step {n}: {field} index {i!r} is out of range; ignored")
            continue
        if i in seen:
            continue
        seen.add(i)
        action = str(x.get("action") or "").strip().lower()
        reason = salvage.gate_line(x.get("reason"), n=200)
        if action == "drop":
            out.append({"i": i, "action": "drop", "reason": reason})
        elif action == "rewrite":
            text = _line(x.get(text_key), PROSE_CAP if field == "prose" else TEXT_CAP)
            if not text:
                problems.append(f"step {n}: empty rewrite of {field} {i}; ignored")
                continue
            out.append({"i": i, "action": "rewrite", "reason": reason, text_key: text})
        elif action not in ("keep", ""):
            problems.append(f"step {n}: unknown action {action!r} for {field} {i}; ignored")
    return out


def validate(raw, data: dict) -> tuple[dict | None, list[str]]:
    """(result, problems): the reply cleaned, gated and checked against the input. Only drops and
    rewrites are kept; entries for unknown steps, out-of-range indices, empty rewrites and
    unknown settings are dropped. None when the reply is not an object."""
    problems: list[str] = []
    if not isinstance(raw, dict):
        return None, ["the reconcile reply is not a JSON object"]
    known = {s["n"]: s for s in data["steps"]}
    steps = []
    seen: set[int] = set()
    for s in raw.get("steps") if isinstance(raw.get("steps"), list) else []:
        n = s.get("n") if isinstance(s, dict) else None
        if isinstance(n, bool) or n not in known:
            problems.append(f"reconcile entry for unknown step {n!r} dropped")
            continue
        if n in seen:
            continue
        seen.add(n)
        checks = _entries(s.get("checks"), len(known[n]["checks"]), "check", problems, n)
        kvs = _entries(s.get("key_values"), len(known[n]["key_values"]), "key value",
                       problems, n)
        prose = _entries(s.get("prose"), len(known[n].get("prose") or []), "prose", problems, n)
        if checks or kvs or prose:
            steps.append({"n": n, "checks": checks, "key_values": kvs, "prose": prose})
    keys = {s["key"].casefold(): s["key"] for s in data["settings"]}
    settings, done = [], set()
    for x in raw.get("settings") if isinstance(raw.get("settings"), list) else []:
        key = keys.get(str(x.get("key") or "").strip().casefold()) if isinstance(x, dict) else None
        default = salvage.gate_line(x.get("default"), n=120) if key else ""
        if key and default and key not in done:
            done.add(key)
            settings.append({"key": key, "default": default,
                             "reason": salvage.gate_line(x.get("reason"), n=200)})
        elif isinstance(x, dict) and x.get("key"):
            problems.append(f"reconcile setting {x.get('key')!r} ignored: unknown key or no "
                            "default")
    return {"steps": sorted(steps, key=lambda e: e["n"]), "settings": settings}, problems


def _default_runner(prompt: str, model: str):
    with tempfile.TemporaryDirectory(prefix="reincarnate-reconcile-") as tmp:
        return claude_runner.run(prompt, cwd=Path(tmp), model=model, max_turns=1, tools="",
                                 timeout=900, label="kit reconcile pass")


def _cache_file(key: str) -> Path:
    return paths.data_dir() / "reconcile" / f"{key}.json"


def _read_cache(key: str) -> dict | None:
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


def reconcile(store, steps: list[dict], plan: dict, decisions: list[dict], proposal: dict,
              lists: dict, *, model: str = MODEL, runner=None, cache: dict | None = None) -> dict:
    """Run the pass, or reuse `cache` (the manifest's `reconcile` entry) when its key matches.
    Never raises. Returns `{result, problems, key, model, cost_usd, spent, cached, reason,
    unavailable}`: `result` is `{steps, settings}` (None when the pass did not run or its reply
    was unusable; `reason` says why; `unavailable` is true when claude is missing)."""
    out = {"result": None, "problems": [], "key": "", "model": model, "cost_usd": 0.0,
           "spent": 0.0, "cached": False, "reason": "", "unavailable": False}
    try:
        data = build_input(store, steps, plan, decisions, proposal, lists)
    except Exception as e:  # noqa: BLE001 - the pass never blocks the export
        return {**out, "reason": f"could not build the input: {e}"[:300]}
    if not has_work(data):
        return {**out, "result": {"steps": [], "settings": []}}
    out["key"] = key = cache_key(data, model)
    if not (isinstance(cache, dict) and cache.get("key") == key):
        cache = _read_cache(key)  # a preview (which writes no kit) or another kit made it
    if (isinstance(cache, dict) and cache.get("key") == key
            and isinstance(cache.get("data"), dict)):
        try:
            cost = float(cache.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        return {**out, "result": cache["data"], "cached": True, "cost_usd": cost}
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
    result, problems = validate(raw, data)
    if result is None:
        return {**out, "problems": problems, "reason": "; ".join(problems)[:300]}
    _write_cache(key, {"key": key, "model": model, "cost_usd": spent, "data": result})
    return {**out, "result": result, "problems": problems}


# -- applying the result ---------------------------------------------------------------------
def _apply_list(items: list, entries: list[dict], field: str, label: str) -> tuple[list, list]:
    """(new items, changes): drops removed, rewrites replaced. A change is
    `{kind, action, old, new, reason}` with every text gated."""
    by_i = {e["i"]: e for e in entries}
    new, changes = [], []
    for i, item in enumerate(items):
        e = by_i.get(i)
        old = str(item.get("value") or "").strip() if field == "value" else str(item).strip()
        if e is None or (e["action"] == "rewrite" and e[field] == old):
            new.append(item)
            continue
        name = _line(item.get("name"), 80) if field == "value" else ""
        shown = f"{name}: {_line(old)}" if name else _line(old)
        if e["action"] == "drop":
            changes.append({"kind": label, "action": "drop", "old": shown, "new": "",
                            "reason": e["reason"]})
            continue
        new.append({**item, "value": e["value"]} if field == "value" else e["text"])
        changes.append({"kind": label, "action": "rewrite", "old": shown,
                        "new": f"{name}: {e[field]}" if name else e[field],
                        "reason": e["reason"]})
    return new, changes


def _apply_prose(items: list[dict], entries: list[dict]) -> tuple[dict, list]:
    """(overrides, changes): `overrides` maps `what`/`design_notes`/`intent` to its new text
    ("" when dropped). A change is `{kind: "prose", label, action, reason}`: the old and new
    paragraphs are not repeated."""
    over, changes = {}, []
    for e in entries:
        item = items[e["i"]]
        if e["action"] == "rewrite" and e["text"] == _line(item["text"], PROSE_CAP):
            continue
        over[item["kind"]] = "" if e["action"] == "drop" else e["text"]
        changes.append({"kind": "prose", "label": PROSE_LABELS.get(item["kind"], "text"),
                        "action": e["action"], "old": "", "new": "", "reason": e["reason"]})
    return over, changes


def apply(plan: dict, lists: dict, result: dict) -> dict:
    """Apply a validated result. Returns `{plan, steps, changes}`: `plan` is a copy whose glue
    and build-fresh checks and settings are updated, `steps` maps a changed step number to
    `{checks, key_values, prose}` (the lists its spec renders instead; `prose` maps what,
    design_notes and intent to their replacement text), `changes` maps a step number to its
    change dicts, and `settings` lists the setting changes."""
    steps: dict[int, dict] = {}
    changes: dict[int, list] = {}
    for s in (result or {}).get("steps") or []:
        own = lists.get(s["n"]) or {}
        checks, c1 = _apply_list(list(own.get("checks") or []), s["checks"], "text", "check")
        kvs, c2 = _apply_list(list(own.get("key_values") or []), s["key_values"], "value",
                              "key value")
        over, c3 = _apply_prose(list(own.get("prose") or []), s.get("prose") or [])
        if c1 or c2 or c3:
            steps[s["n"]] = {"checks": checks, "key_values": kvs, "prose": over}
            changes[s["n"]] = c1 + c2 + c3
    by_key = {s["key"]: s for s in (result or {}).get("settings") or []}
    settings, setting_changes = [], []
    for s in plan.get("settings") or []:
        r = by_key.get(s["key"])
        if r is None:
            settings.append(s)
            continue
        meaning = _UNKNOWN.sub("", s.get("meaning") or "").strip()
        settings.append({**s, "default": r["default"],
                         "meaning": (meaning + " " if meaning else "") + f"Default: {r['default']}"
                         + ("" if r["default"].endswith(".") else ".")})
        setting_changes.append({"kind": "setting", "action": "default", "old": s["key"],
                                "new": r["default"], "reason": r["reason"]})
    entries = []
    for e in plan.get("steps") or []:
        if e["n"] in steps and e["checks"]:
            e = {**e, "checks": steps[e["n"]]["checks"]}
        entries.append(e)
    return {"plan": {**plan, "settings": settings, "steps": entries}, "steps": steps,
            "changes": changes, "settings": setting_changes}


def count(applied: dict) -> int:
    return sum(len(c) for c in applied["changes"].values()) + len(applied["settings"])


def changed_section(changes: list[dict]) -> list[str]:
    """The `**Changed to match ...**` lines for one spec (empty when nothing changed)."""
    if not changes:
        return []
    out = [HEADING, ""]
    for c in changes:
        why = f" (reason: {c['reason']})" if c["reason"] else ""
        if c["kind"] == "prose":
            if c["action"] == "drop":
                out.append(f"- Removed the {c['label']}{why}")
            else:
                out.append(f"- Rewrote the {c['label']} to match the scope"
                           + (f": {c['reason']}" if c["reason"] else ""))
        elif c["action"] == "drop":
            out.append(f"- Removed {c['kind']}: {c['old']}{why}")
        else:
            out.append(f"- Rewrote {c['kind']}: {c['old']} -> {c['new']}{why}")
    return out
