"""Export a buildable `.reincarnate/` kit for a composition."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import re
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__, design, kitplan, paths, reconcile, salvage, schema
from . import planner as idea_planner
from .models import Project
from .store import _name_key

# Glue steps list the kit parts (name + behavior) and glue bullets that overlap the requirement at
# least this much (the design auto-fill relevance measure).
GLUE_JOIN_MIN = 0.2
DEEPEN_MODEL = "claude-haiku-5-5"
DEEPEN_MAX_TURNS = 25
MANIFEST = "manifest.json"
SLUG_MAX = 40

RUN_SH = r'''#!/usr/bin/env bash
# Reincarnate kit driver: builds each step with `claude -p`, runs the tests, then Code Read's
# turn check (`cr check-turn`) when installed, ticks the step in tasks.md and pauses for review.
# Usage: .reincarnate/run.sh [--yes] [--from N]
#   --yes       do not pause for review between steps
#   --from N    start at step N (re-runs ticked steps); default: the first unticked step
#        .reincarnate/run.sh --begin N | --check N
#   --begin N   record step N's baseline and print its spec, for a build in a Claude Code
#               session (/reincarnate:build); no `claude -p` is run
#   --check N   run the tests and Code Read against that baseline, then tick step N
# Env: REINCARNATE_MODEL (default sonnet); REINCARNATE_CR = the Code Read command, split on
#   spaces (no quoting), e.g. "node /path/to/coderead/cli/dist/cli.js"; default: `cr` on PATH;
#   REINCARNATE_SANDBOX=1 = this is a sandbox, the builder may run any Bash command.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.." || exit 1
usage() {
  echo "Usage: .reincarnate/run.sh [--yes] [--from N] | --begin N | --check N" >&2; exit 2
}
YES=0
FROM=""
MODE=run
STEP=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) YES=1 ;;
    --from) shift; FROM="${1:-}"; case "$FROM" in ''|*[!0-9]*) usage ;; esac ;;
    --begin|--check)
      [ "$MODE" = run ] || usage
      MODE="${1#--}"; shift; STEP="${1:-}"; case "$STEP" in ''|*[!0-9]*) usage ;; esac ;;
    *) usage ;;
  esac
  shift
done
[ "$MODE" = run ] || { [ "$YES" -eq 0 ] && [ -z "$FROM" ]; } || usage
MODEL="${REINCARNATE_MODEL:-sonnet}"
TEST_CMD=__TEST_CMD__
ALLOWED=(__ALLOWED__)
NEW_TARGET=__NEW_TARGET__
TASKS=.reincarnate/tasks.md
UNVERIFIED=" (unverified: Code Read not installed)"
CR_HINT='Code Read: https://github.com/dhouchin1/coderead (build cli/, then run "npm link" in cli/'
CR_HINT+=' to put cr on PATH), or set REINCARNATE_CR="node /path/to/coderead/cli/dist/cli.js"'

step_open() { grep -q "^- \[ \] Step $1:" "$TASKS" 2>/dev/null; }
tick() {  # tick step $1 in tasks.md; $2 = suffix to append (may be empty)
  awk -v n="$1" -v suf="$2" -v unv="$UNVERIFIED" '
    { pre = "- [ ] Step " n ":"; pre2 = "- [x] Step " n ":"
      if (index($0, pre) == 1 || index($0, pre2) == 1) {
        rest = substr($0, length(pre) + 1)
        if (length(rest) >= length(unv) && substr(rest, length(rest) - length(unv) + 1) == unv)
          rest = substr(rest, 1, length(rest) - length(unv))
        $0 = "- [x] Step " n ":" rest suf
      }
      print }' "$TASKS" > "$TASKS.tmp" && mv "$TASKS.tmp" "$TASKS"
}
baseline() {  # print the sha of a tree holding the working directory, without touching the repo
  local tmp sha
  tmp="$(mktemp -d)" || return 1
  if GIT_INDEX_FILE="$tmp/index" git add -A >/dev/null 2>&1; then
    sha="$(GIT_INDEX_FILE="$tmp/index" git write-tree)"
  fi
  rm -rf "$tmp"
  [ -n "${sha:-}" ] && echo "$sha"
}
stop() {  # stop $1 "message"
  echo "Step $1: $2" >&2
  if [ "$MODE" = run ]; then
    echo "Fix the cause, then rerun: .reincarnate/run.sh --from $1" >&2
  else
    echo "Fix the cause, then run: .reincarnate/run.sh --check $1" >&2
  fi
  exit 1
}
need_git() {
  command -v git >/dev/null 2>&1 || { echo "git is not installed; run.sh needs it." >&2; exit 1; }
  # a new target gets its own repository, even inside another one, so its baselines hold only it
  local top
  top="$(git rev-parse --show-toplevel 2>/dev/null)"
  if [ -z "$top" ] || { [ "$NEW_TARGET" -eq 1 ] && [ "$top" != "$(pwd -P)" ]; }; then
    git init -q || exit 1
  fi
}
find_cr() {  # Code Read is optional; CR=() when it is not installed
  CR=()
  if [ -n "${REINCARNATE_CR:-}" ]; then
    read -r -a CR <<< "$REINCARNATE_CR"
  elif command -v cr >/dev/null 2>&1; then
    CR=(cr)
  fi
  if [ "${#CR[@]}" -gt 0 ] && ! command -v "${CR[0]}" >/dev/null 2>&1; then
    echo "REINCARNATE_CR command not found: ${CR[0]}" >&2
    CR=()
  fi
  if [ "${#CR[@]}" -eq 0 ]; then
    echo "################################################################"
    echo "# WARNING: Code Read is not installed. Steps are still built and"
    echo "# tested, but they are NOT checked for secrets, tripwires or new"
    echo "# dependencies. They are ticked as unverified in tasks.md."
    echo "################################################################"
  fi
}
open_base_of() {  # the baseline state.json holds for step $1 while it is open
  sed -n "s/^{\"step\": $1, \"baseline\": \"\([0-9a-f]*\)\", \"status\": \"open\"}$/\1/p" \
    .reincarnate/state.json 2>/dev/null
}
open_baseline() {  # BASE for step $1: a stopped step's own baseline, else a fresh snapshot
  BASE="$(open_base_of "$1")"
  if [ -n "$BASE" ] && git cat-file -e "$BASE^{tree}" 2>/dev/null; then
    echo "--- rerun of step $1: keeping its baseline ${BASE:0:12} ---"
  else
    BASE="$(baseline)" || stop "$1" "could not snapshot the working tree with git."
  fi
}
save_state() {  # save_state N open|passed
  printf '{"step": %s, "baseline": "%s", "status": "%s"}\n' "$1" "$BASE" "$2" \
    > .reincarnate/state.json
}
verify_step() {  # tests, then Code Read against BASE; ticks step $1; sets SUF
  local nn rc out cmd
  nn="$(printf '%02d' "$1")"
  cmd="$TEST_CMD"
  case "$cmd" in  # a Python test command runs in the project's .venv once step 0 made one
    "python3 -m "*|"python -m "*)
      [ -x .venv/bin/python ] && cmd=".venv/bin/python -m ${cmd#* -m }" ;;
  esac
  if [ -n "$cmd" ]; then
    echo "--- running tests: $cmd ---"
    bash -c "$cmd" || stop "$1" "the tests failed."
  fi
  SUF=""
  if [ "${#CR[@]}" -gt 0 ]; then
    echo "--- Code Read turn check ---"
    out=".reincarnate/checks/step-$nn.txt"
    "${CR[@]}" check-turn --repo . --since "$BASE" --format text 2>&1 | tee "$out"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 1 ]; then
      stop "$1" "Code Read found problems (see .reincarnate/checks/step-$nn.txt)."
    elif [ "$rc" -ne 0 ]; then
      stop "$1" "Code Read could not run (exit $rc)."
    fi
  else
    SUF="$UNVERIFIED"
  fi
  tick "$1" "$SUF"
  save_state "$1" passed
}
spec_of() {  # the spec file of step $1
  local nn f
  nn="$(printf '%02d' "$1")"
  for f in .reincarnate/specs/"$nn"-*.md; do [ -e "$f" ] && { echo "$f"; return 0; }; done
  return 1
}

# /reincarnate:build: the session builds between --begin and --check
if [ "$MODE" = begin ]; then
  SPEC="$(spec_of "$STEP")" || { echo "No step $STEP in this kit." >&2; exit 2; }
  if ! step_open "$STEP"; then
    echo "Step $STEP is already ticked in tasks.md; untick it first to rebuild it." >&2
    exit 1
  fi
  need_git
  open_baseline "$STEP"
  save_state "$STEP" open
  echo "Step $STEP begun (baseline ${BASE:0:12}). Spec: $SPEC"
  echo "Build it under the clean-room rules, then run: .reincarnate/run.sh --check $STEP"
  exit 0
fi
if [ "$MODE" = check ]; then
  spec_of "$STEP" >/dev/null || { echo "No step $STEP in this kit." >&2; exit 2; }
  BASE="$(open_base_of "$STEP")"
  if [ -z "$BASE" ]; then
    echo "Step $STEP was not begun; run .reincarnate/run.sh --begin $STEP first." >&2
    exit 1
  fi
  need_git
  find_cr
  mkdir -p .reincarnate/checks
  verify_step "$STEP"
  if [ -n "$SUF" ]; then
    echo "Step $STEP done (unverified: Code Read not installed)."
    echo "$CR_HINT"
  else
    echo "Step $STEP done (verified by Code Read)."
  fi
  exit 0
fi

# which steps will run
PENDING=0
for spec in .reincarnate/specs/*.md; do
  [ -e "$spec" ] || continue
  f="$(basename "$spec")"
  N=$((10#${f%%-*}))
  if [ -n "$FROM" ]; then
    [ "$N" -ge "$FROM" ] && PENDING=$((PENDING + 1))
  elif step_open "$N"; then
    PENDING=$((PENDING + 1))
  fi
done
if [ "$PENDING" -eq 0 ]; then
  if [ -n "$FROM" ]; then echo "No step $FROM or later." >&2; exit 2; fi
  echo "All steps are already done."
  exit 0
fi

command -v claude >/dev/null 2>&1 || { echo "claude is not on PATH." >&2; exit 1; }
need_git
find_cr

# builder permissions: any Bash only inside a sandbox, else an allowlist
PERM=()
if [ -f /.dockerenv ] || [ -f /run/.containerenv ] || [ "${REINCARNATE_SANDBOX:-}" = "1" ]; then
  PERM=(--permission-mode bypassPermissions)
  echo "Builder permissions: sandbox detected, any Bash command allowed."
else
  PERM=(--permission-mode acceptEdits)
  if [ "${#ALLOWED[@]}" -gt 0 ]; then
    PERM+=(--allowedTools "${ALLOWED[@]}")
  fi
  echo "Builder permissions: not a sandbox, edits plus ${#ALLOWED[@]} Bash patterns."
fi

mkdir -p .reincarnate/checks
VERIFIED=0
UNVER=0
FIRST=""
LAST=""
for spec in .reincarnate/specs/*.md; do
  [ -e "$spec" ] || continue
  f="$(basename "$spec")"
  N=$((10#${f%%-*}))
  if [ -n "$FROM" ]; then
    [ "$N" -lt "$FROM" ] && continue
  else
    step_open "$N" || continue
  fi
  echo "=== Step $N: $f ==="
  # a rerun of a step that stopped keeps that step's baseline, so whatever the stopped attempt
  # left behind is still in the diff Code Read checks
  open_baseline "$N"
  save_state "$N" open
  PROMPT="You are building step $N of the Reincarnate kit in $(pwd)."
  PROMPT+=" Read .reincarnate/CLAUDE.md and .reincarnate/specs/$f, then implement it."
  PROMPT+=" Follow the clean-room rules exactly. Do not read any donor repository."
  PROMPT+=" Every step is checked afterwards by the tests and by Code Read's turn check."
  claude -p "$PROMPT" "${PERM[@]}" --max-turns 60 --model "$MODEL"
  rc=$?
  [ "$rc" -eq 0 ] || stop "$N" "the builder failed (exit $rc)."
  verify_step "$N"
  if [ -n "$SUF" ]; then UNVER=$((UNVER + 1)); else VERIFIED=$((VERIFIED + 1)); fi
  [ -n "$FIRST" ] || FIRST="$N"
  LAST="$N"
  if [ "$YES" -eq 0 ]; then
    read -r -p "Review step $N, then press Enter to continue or Ctrl-C to stop" _
  fi
done
if [ "$FIRST" = "$LAST" ]; then RANGE="step $FIRST"; else RANGE="steps $FIRST-$LAST"; fi
echo "Done: built $RANGE ($VERIFIED verified by Code Read, $UNVER unverified)."
[ "$UNVER" -eq 0 ] || echo "$CR_HINT"
exit 0
'''


def slugify(text: str) -> str:
    """Lowercase letters and digits of any script; everything else becomes one dash."""
    return re.sub(r"[\W_]+", "-", str(text).lower()).strip("-") or "app"


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _kit_made_project(kit_dir: Path, composition) -> bool:
    """This kit created the project: its manifest says it was exported for this composition as a
    new target, or (a kit from before manifests) its tasks begin with the scaffold step. Such a
    project stays a new target on re-export even once step 0 has written its manifests, so the
    option's stack and step 0 survive."""
    try:
        data = json.loads((kit_dir / MANIFEST).read_text(encoding="utf-8"))
        return (composition is not None and data.get("composition") == composition.slug
                and data.get("kind") == "new")
    except (OSError, ValueError, AttributeError):
        pass
    try:
        tasks = (kit_dir / "tasks.md").read_text(encoding="utf-8")
    except OSError:
        return False
    return re.search(r"^- \[[ x]\] Step 0: scaffold the project", tasks, re.M) is not None


def _kit_composition(kit_dir: Path) -> str | None:
    """The composition slug an existing kit's manifest names (None: no readable manifest)."""
    try:
        data = json.loads((kit_dir / MANIFEST).read_text(encoding="utf-8"))
        return str(data["composition"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _resolve_target(store, target: str, composition=None,
                    warnings: list[str] | None = None) -> tuple[Path, str, Project | None]:
    """Return (target dir, spec target slug, project for an existing target or None for a new
    one). Existing: a registered project, or a directory holding a manifest. New: `new:<name>`
    or a directory without a manifest. A `new:` name whose directory already holds another
    composition's kit gets a `-2`, `-3`... suffix."""
    target = str(target)
    if target.startswith(salvage.NEW_PREFIX):
        name = target[len(salvage.NEW_PREFIX):].strip()
        if not name:
            raise ValueError("new: target needs a name")
        base = slug = slugify(name)
        n = 1
        while True:
            owner = _kit_composition(paths.data_dir() / "new" / slug / ".reincarnate")
            if owner is None or composition is None or owner == composition.slug:
                break
            n += 1
            slug = f"{base}-{n}"
        if slug != base and warnings is not None:
            warnings.append(f"new:{name}: {paths.data_dir() / 'new' / base} already holds the "
                            f"kit of another composition; exporting into {slug}")
        return paths.data_dir() / "new" / slug, f"{salvage.NEW_PREFIX}{name}", None
    proj = store.get_project(target)
    if proj is not None:
        d = Path(proj.path).expanduser()
        if _kit_made_project(d / ".reincarnate", composition):
            return d, proj.slug, None
        return d, proj.slug, proj
    d = Path(target).expanduser()
    if d.is_dir():
        d = d.resolve()
        if salvage.has_manifest(d) and not _kit_made_project(d / ".reincarnate", composition):
            return d, d.name, Project(slug=d.name, path=str(d))
        return d, d.name, None
    raise ValueError(f"unknown target: {target} (not a project slug, new:<name>, or directory)")


def _table_cell(s: str) -> str:
    return salvage.gate_text(s).replace("|", "\\|").replace("\n", " ")


def _name(s, n: int = 80) -> str:
    """A donor-derived name for a heading, bullet or cell: one clean gated line."""
    return salvage.gate_line(s, n=n)


def _ref_name(x) -> str:
    if isinstance(x, dict):
        return _name(x.get("name") or x.get("id") or "?")
    return _name(x)


def _tok(x: dict) -> str:
    return _name(x.get("token"), 60).replace("`", "")


def _wiring_lines(w: dict) -> list[str]:
    """`proposal["wiring"]` (from a picked design option) as markdown bullets."""
    out = []
    for x in w.get("wired") or []:
        out.append(f"- wired: {_ref_name(x.get('consumer'))} needs `{_tok(x)}` from "
                   f"{_ref_name(x.get('provider'))}")
    for x in w.get("unmet") or []:
        extra = ""
        if x.get("rejected_provider"):
            extra = f" (link to {_ref_name(x['rejected_provider'])} rejected)"
        elif x.get("candidates"):
            extra = " (catalog candidates: " + ", ".join(
                _ref_name(c) for c in x["candidates"]) + ")"
        out.append(f"- unmet: {_ref_name(x.get('consumer'))} needs `{_tok(x)}`{extra}")
    labels = {"settings": "setting", "external": "external", "owned": "owned state",
              "platform": "platform"}
    for k, label in labels.items():
        for x in w.get(k) or []:
            out.append(f"- {label}: {_ref_name(x.get('consumer'))} needs `{_tok(x)}`")
    return out or ["(no interface needs)"]


def _coverage_lines(proposal: dict) -> list[str]:
    texts = {r.get("id"): r.get("text", "") for r in proposal.get("requirements") or []
             if isinstance(r, dict)}
    names = {f"c{c.get('capability_id')}": _name(c.get("name", ""))
             for c in proposal.get("capabilities") or [] if isinstance(c, dict)}
    rows = ["| Requirement | Kind | Capability |", "| --- | --- | --- |"]
    for row in proposal.get("coverage") or []:
        if not isinstance(row, dict):
            continue
        req = row.get("req", "")
        label = f"{req}: {_name(texts[req], 300)}" if texts.get(req) else _name(req, 300)
        cap = row.get("cap") or ""
        rows.append(f"| {_table_cell(label)} | {_table_cell(row.get('kind', ''))} | "
                    f"{_table_cell(names.get(cap) or _name(cap) or '-')} |")
    return rows


def _cap_steps(cap_rows: list[dict], store, problems: list[str]) -> list[dict]:
    """One step per proposal row whose part still exists, is live and (when the row recorded a
    name and donor) is still that part; every other row is a problem."""
    caps = []
    for row in cap_rows:
        cid = row.get("capability_id")
        rname = _name(row.get("name") or "", 80)
        label = f"part c{cid}" + (f" ({rname})" if rname else "")
        cap = store.get_capability(cid)
        if cap is None:
            problems.append(f"{label} is missing from the catalog")
            continue
        if cap.retired_at:
            problems.append(f"{label} is retired: a re-comprehension no longer found it")
            continue
        same_name = not row.get("name") or _name_key(row["name"]) == _name_key(cap.name)
        same_donor = (not row.get("donor")
                      or str(row["donor"]).lower() == str(cap.donor).lower())
        if not (same_name and same_donor):
            problems.append(f"{label} is now a different part ({_name(cap.name)} from "
                            f"{_name(cap.donor, 200)})")
            continue
        caps.append({"kind": "cap", "cap": cap, "role": row.get("role") or ""})
    return caps


def _glue_bullets(glue: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in str(glue or "").splitlines()
            if ln.strip().startswith("- ") and ln.strip()[2:].strip()]


def _named_in(cap, lines: list[str]) -> bool:
    """The capability's exact name (case-insensitive) or its `c<id>` appears in a bullet."""
    name = str(cap.name or "").strip()
    pat = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE) if name else None
    for line in lines:
        if pat is not None and pat.search(line):
            return True
        refs = {int(r[1:]) for m in salvage.CID_REF.finditer(line) for r in m.group(0).split("/")}
        if cap.id in refs:
            return True
    return False


def _glue_steps(proposal: dict, caps: list[dict], glue: str | None = None) -> list[dict]:
    """One step per `kind == "glue"` coverage row: its requirement and the glue bullets that
    overlap it (relevance >= GLUE_JOIN_MIN; any shared word pulled unrelated bullets into every
    step on the homelab design). It joins the kit parts those bullets name (exact name or
    `c<id>`). When they name none, nothing is joined: word overlap against deepened (long)
    behavior text tied the alerts requirement to Auth and TOTP on the real data, so overlap
    matches are only listed as `related` and the step goes last (see `_order_steps`)."""
    texts = {r.get("id"): str(r.get("text") or "").strip()
             for r in proposal.get("requirements") or [] if isinstance(r, dict)}
    bullets = _glue_bullets(proposal.get("glue") or "" if glue is None else glue)
    out: list[dict] = []
    seen: set[str] = set()
    for row in proposal.get("coverage") or []:
        if not isinstance(row, dict) or row.get("kind") != "glue":
            continue
        req = str(row.get("req") or "").strip()
        if req in seen:
            continue
        seen.add(req)
        text = salvage.gate_text(texts.get(req) or req)
        lines = [b for b in bullets if design.text_relevance(b, text) >= GLUE_JOIN_MIN]
        joins = [st["cap"].id for st in caps if _named_in(st["cap"], lines)]
        related = [] if joins else [
            st["cap"].id for st in caps
            if design.text_relevance(f"{st['cap'].name} {st['cap'].what}", text) >= GLUE_JOIN_MIN]
        out.append({"kind": "glue", "req": req, "text": text, "joins": joins,
                    "related": related, "lines": lines})
    return out


def _wired_tokens(items) -> set[str]:
    return {str(t) for t in items or [] if not str(t).startswith(("config:", "ext:"))}


def _wire_order(caps: list[dict]) -> list[dict]:
    """Stable topological order: a part comes after every part providing a token it requires
    (`config:` and `ext:` tokens are not wiring). Parts in a cycle keep their relative order."""
    n = len(caps)
    prov = [_wired_tokens(getattr(st["cap"], "provides", None)) for st in caps]
    need = [_wired_tokens(getattr(st["cap"], "requires", None)) for st in caps]
    first = [{j for j in range(n) if j != i and prov[j] & need[i]} for i in range(n)]
    done: list[int] = []
    while len(done) < n:
        left = [i for i in range(n) if i not in done]
        done.append(next((i for i in left if first[i] <= set(done)), left[0]))
    return [caps[i] for i in done]


def _order_steps(caps: list[dict], gaps: list[dict], glue: list[dict]) -> list[dict]:
    """Capability steps in wiring order (providers first, else proposal order); a gap with
    `before_capability_id` goes right before that capability; a glue step right after the last
    part it joins. Then every other gap, then glue steps that join nothing: with no named
    parts, the safe place is after everything."""
    caps = _wire_order(caps)
    kit_ids = [st["cap"].id for st in caps]
    before: dict[int, list[dict]] = {}
    tail: list[dict] = []
    for gap in gaps:
        bid = _as_int(gap.get("before_capability_id")) if isinstance(gap, dict) else None
        st = {"kind": "gap", "gap": gap if isinstance(gap, dict) else {"need": str(gap)}}
        if bid in kit_ids:
            before.setdefault(bid, []).append(st)
        else:
            tail.append(st)
    after: dict[int, list[dict]] = {}
    loose: list[dict] = []
    for st in glue:
        last = max((kit_ids.index(cid) for cid in st["joins"] if cid in kit_ids), default=None)
        if last is None:
            loose.append(st)
        else:
            after.setdefault(kit_ids[last], []).append(st)
    steps: list[dict] = []
    for st in caps:
        steps += before.pop(st["cap"].id, [])
        steps.append(st)
        steps += after.pop(st["cap"].id, [])
    return steps + tail + loose


def _provides(cap, prefix: str) -> bool:
    return any(str(t).startswith(prefix) for t in getattr(cap, "provides", None) or [])


def _auth_step(steps: list[dict]) -> dict | None:
    """The first capability step whose capability provides an `auth:` token."""
    for st in steps:
        if st["kind"] == "cap" and _provides(st["cap"], "auth:"):
            return {"step": st["n"], "name": _name(st["cap"].name)}
    return None


def _auth_note(auth: dict) -> str:
    a = auth["step"]
    return (f"**Cross-cutting: auth arrives in step {a} ({auth['name']}).** Put every endpoint "
            "and edit action this step adds behind the authorization guard (create it if it "
            f"does not exist). It allows everything until step {a}.")


def _surface(st: dict) -> bool:
    """The step adds endpoints or screens: a part providing `http:`/`ui:`, or a build-fresh
    step for such a token or for a part that does (`surface` is set when ordering)."""
    if st["kind"] == "cap":
        return _provides(st["cap"], "http:") or _provides(st["cap"], "ui:")
    return st["kind"] == "gap" and bool(st.get("surface"))


def _needs_auth_note(st: dict, auth: dict | None) -> bool:
    if auth is None or st["n"] >= auth["step"]:
        return False
    return st["kind"] == "glue" or _surface(st)


def _needs_list(st: dict, steps: list[dict]) -> list[str]:
    """`token from part (step N)` for each wired requirement a part gets from an earlier step."""
    got = []
    wired = _wired_tokens(getattr(st["cap"], "requires", None))
    for tok in dict.fromkeys(t for t in getattr(st["cap"], "requires", None) or [] if t in wired):
        for p in steps:
            if (p["kind"] == "cap" and p["n"] < st["n"]
                    and tok in _wired_tokens(getattr(p["cap"], "provides", None))):
                got.append(f"{_name(tok, 60).replace('`', '')} from {_name(p['cap'].name)} "
                           f"(step {p['n']})")
                break
        else:  # a build-fresh step shared by several consumers: only the later ones list it
            for p in steps:
                g = p["gap"] if p["kind"] == "gap" else {}
                if (p["n"] < st["n"] and g.get("token") == tok and st["cap"].id in
                        [_as_int(a.get("capability_id")) for a in g.get("also_consumers") or []
                         if isinstance(a, dict)]):
                    got.append(f"{_name(tok, 60).replace('`', '')} from the build-fresh step "
                               f"(step {p['n']})")
                    break
    return got


def _gap_notes(gap: dict, in_kit: dict) -> list[str]:
    """Extra lines for a wiring gap's spec: the other parts needing the same token, and a
    verifier rejection."""
    notes = []
    also = []
    for a in gap.get("also_consumers") or []:
        if not isinstance(a, dict):
            continue
        cid = _as_int(a.get("capability_id"))
        step = in_kit.get(cid, (None, None))[1]
        label = _name(a.get("name") or "", 200) or f"part {cid}"
        also.append(f"{label} (step {step})" if step is not None else label)
    if also:
        notes.append("**Also used by:** " + ", ".join(also))
    rej = _name(gap.get("rejected_by_verifier") or "", 300)
    if rej:
        notes.append(f"**Link rejected:** the link verifier rejected {rej} for this need; "
                     "check before building fresh.")
    return notes


def _needs_lines(st: dict, steps: list[dict]) -> str:
    got = _needs_list(st, steps)
    return "**Needs from earlier steps:** " + "; ".join(got) if got else ""


def _check_count(spec: str) -> int:
    """How many acceptance checks a rendered spec lists."""
    lines = spec.splitlines()
    try:
        i = lines.index("**Acceptance checks:**")
    except ValueError:
        return 0
    n = 0
    for ln in lines[i + 1:]:
        if ln.startswith("- "):
            n += 1
        elif ln.strip():
            break
    return n


def _not_deepened(steps: list[dict]) -> list[int]:
    return [st["cap"].id for st in steps
            if st["kind"] == "cap" and not getattr(st["cap"], "deepened_at", "")]


def _run_deepen(store, ids: list[int]) -> dict:
    """`deepen.deepen` for the kit's parts (imported lazily: the module reads donor source)."""
    try:
        mod = importlib.import_module("reincarnate.deepen")
        return mod.deepen(store, ids, model=getattr(mod, "DEFAULT_MODEL", DEEPEN_MODEL),
                          max_turns=DEEPEN_MAX_TURNS, force=False)
    except Exception as e:  # noqa: BLE001 - report, still export the kit
        return {"capabilities": ids, "deepened": [], "skipped": [], "cost_usd": 0.0,
                "failed": [{"capability_id": None, "reason": f"deepen failed: {e}"[:300]}],
                "text": f"deepen failed: {e}"}


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cap_checks(cap, proposal: dict) -> list[str]:
    """Stored acceptance checks, then one line per requirement this capability covers."""
    checks = [str(c).strip() for c in getattr(cap, "acceptance", None) or [] if str(c).strip()]
    texts = {r.get("id"): r.get("text", "") for r in proposal.get("requirements") or []
             if isinstance(r, dict)}
    for row in proposal.get("coverage") or []:
        if (isinstance(row, dict) and row.get("kind") == "catalog"
                and row.get("cap") == f"c{cap.id}"):
            req = str(row.get("req") or "").strip()
            text = str(texts.get(req) or "").strip()
            checks.append(f"Covers requirement {req}: {text}" if text
                          else f"Covers requirement {req}")
    return checks


def _id_resolver(store, in_kit: dict[int, tuple[str, int]]):
    """`c<id>` -> "<name> (step N)" for kit capabilities, else the capability's name."""
    names: dict[int, str | None] = {}

    def lookup(cid: int) -> str | None:
        if cid in in_kit:
            name, step = in_kit[cid]
            return f"{_name(name)} (step {step})"
        if cid not in names:
            cap = store.get_capability(cid)
            names[cid] = _name(cap.name) if cap else None
        return names[cid]

    return lambda text: salvage.resolve_cids(text, lookup)


def _target_policy(store, project: Project | None, target_dir: Path, stack_text) -> dict:
    """New vs existing target, its stack and test command (see DESIGN "Kit generation"). The
    target decides: a registered project or a directory with a manifest is existing."""
    prof = store.get_profile()
    profile_stack = schema.clean_stack(prof.stack if prof else [])
    if project is not None:
        stack = (schema.clean_stack(project.stack) or
                 schema.clean_stack(salvage.infer_stack(target_dir)[0]) or profile_stack)
        test_cmd = project.test_cmd or salvage.manifest_command(target_dir)
        return {"kind": "existing", "stack": stack, "test_cmd": test_cmd, "ecosystems": []}
    stack = schema.clean_stack(salvage.split_stack(stack_text)) or profile_stack
    ecos = salvage.ecosystems(stack)
    return {"kind": "new", "stack": stack, "test_cmd": salvage.derive_test_cmd(ecos),
            "ecosystems": ecos}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


TASK_LINE = re.compile(r"^- \[([ x])\] Step (\d+): (.*)$")
UNVERIFIED_TAIL = re.compile(r"( \(unverified: [^)]*\))$")


def _old_ticks(text: str) -> dict[str, str]:
    """Ticked steps of a tasks.md: title -> the suffix the run left (` (unverified: ...)`)."""
    ticks: dict[str, str] = {}
    for line in text.splitlines():
        m = TASK_LINE.match(line)
        if m and m.group(1) == "x":
            rest = m.group(3)
            tail = UNVERIFIED_TAIL.search(rest)
            ticks[rest[:tail.start()] if tail else rest] = tail.group(1) if tail else ""
    return ticks


def _stage(tmp: Path, text: str, executable: bool) -> None:
    tmp.write_text(text, encoding="utf-8")
    if executable:
        tmp.chmod(0o755)


def _write_all(kit_dir: Path, files: dict[str, str], obsolete: list[str]) -> None:
    """Stage every file next to its destination first, then swap them in; the old kit stays
    intact when staging fails. Obsolete specs go last."""
    staged: list[tuple[Path, Path]] = []
    try:
        for rel, text in files.items():
            if "\x00" in text:  # a gate bug once leaked NUL bytes; never let one reach a kit
                raise ValueError(f"internal error: {rel} contains a NUL byte; nothing written")
            dest = kit_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".tmp")
            staged.append((tmp, dest))
            _stage(tmp, text, rel == "run.sh")
    except BaseException:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise
    for tmp, dest in staged:
        os.replace(tmp, dest)
    for rel in obsolete:
        (kit_dir / rel).unlink(missing_ok=True)


def _backup(kit_dir: Path, rels: list[str]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest, n = kit_dir / f"backup-{stamp}", 1
    while dest.exists():
        n += 1
        dest = kit_dir / f"backup-{stamp}-{n}"
    for rel in rels:
        src = kit_dir / rel
        if src.is_file() and not src.is_symlink():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / rel)
    return dest


MAX_ANSWER = 2000


def _manifest_decisions(store, composition) -> list[dict] | None:
    """The open decisions of the composition's latest kit that holds a plan, or None."""
    rows = store.conn.execute("SELECT path FROM kits WHERE composition_id=? ORDER BY id DESC",
                              (composition.id,)).fetchall()
    for r in rows:
        try:
            m = json.loads((Path(r["path"]) / MANIFEST).read_text(encoding="utf-8"))
            return list(m["plan"]["data"].get("open_decisions") or [])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def _plan_takes_decisions() -> bool:
    """True when `kitplan.plan` can re-plan with the answered decisions (the second phase)."""
    try:
        params = inspect.signature(kitplan.plan).parameters
    except (TypeError, ValueError):
        return False
    return "decisions" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def answer_decisions(store, composition, answers: dict, decisions: list[dict] | None = None,
                     save: bool = True, stale: list | None = None) -> dict:
    """Record answers to the plan's open decisions on the composition and return the whole
    decisions map (`{id: {question, answer, answered_at, source}}`). `decisions` is the current
    plan's list (export_kit passes it); without it the latest kit's manifest is read. An answer
    is one of the options or free text, gated like any text a builder reads.

    An answer is plain text (`{"D1": "answer"}`, applied by id) or bound to the question it
    answers (`{"D1": {"question": ..., "answer": ...}}`). A bound answer goes to the decision
    whose question text matches, whatever its id now is; when no current question matches (a
    re-plan asked different questions), it is not applied and `(id, question)` is appended to
    `stale`. Raises ValueError for an unknown id or an unusable answer, saving nothing."""
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object of decision id to answer")
    if decisions is None:
        decisions = _manifest_decisions(store, composition)
    if decisions is None:
        raise ValueError("this composition has no plan with open decisions yet; export a kit "
                         "(or run `reincarnate kit ... --decisions`) first")
    by_id = {d["id"]: d for d in decisions}
    by_q = {kitplan._norm_q(d["question"]): d for d in decisions}
    pairs: list[tuple[dict, object]] = []
    unknown: list[str] = []
    for did, ans in answers.items():
        did = str(did)
        if isinstance(ans, dict):
            q = ans.get("question")
            if not isinstance(q, str) or not q.strip():
                raise ValueError(f"the answer to {did} has no question text")
            d = by_q.get(kitplan._norm_q(q))
            if d is None:
                if stale is not None:
                    stale.append((did, salvage.gate_line(q, n=300)))
                continue
            pairs.append((d, ans.get("answer")))
        elif did in by_id:
            pairs.append((by_id[did], ans))
        else:
            unknown.append(did)
    if unknown:
        raise ValueError(f"unknown decision id(s) {', '.join(unknown)}; the open decisions are: "
                         + (", ".join(by_id) or "(none)"))
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    saved = dict((composition.proposal or {}).get("decisions") or {})
    for d, ans in pairs:
        did = d["id"]
        if not isinstance(ans, str) or not ans.strip():
            raise ValueError(f"the answer to {did} must be non-empty text")
        if len(ans) > MAX_ANSWER:
            raise ValueError(f"the answer to {did} is over {MAX_ANSWER} characters")
        text = salvage.gate_text(" ".join(ans.split())).strip()
        if not text:
            raise ValueError(f"the answer to {did} is empty once paths and code are removed")
        text = next((o for o in d["options"] if o.casefold() == text.casefold()), text)
        saved[did] = {"question": d["question"], "answer": text, "answered_at": stamp,
                      "source": "default" if text == d["default"] else "user"}
    if save and pairs:
        composition.proposal = {**(composition.proposal or {}), "decisions": saved}
        store.save_composition(composition)
    return saved


def _kind_warnings(composition, policy: dict, spec_target: str) -> list[str]:
    """Warnings for a composition designed for another kind of target than the one exported to."""
    is_new = policy["kind"] == "new"
    kind = composition.target_kind
    if is_new and kind == "existing":
        return [f"composition was designed for existing project "
                f"{composition.target_slug}; exporting as a new project adds a "
                "scaffold step and uses the composition's stack"]
    if not is_new and kind == "new":
        return [f"composition was designed for a new project; exporting into existing "
                f"project {spec_target} keeps its stack and adds no scaffold step"]
    if not is_new and kind == "existing" and composition.target_slug != spec_target:
        return [f"composition was designed for project {composition.target_slug}; "
                f"exporting into {spec_target}"]
    return []


class _OldKit:
    """What the target's previous kit holds: its manifest, ticked steps and the specs on disk."""

    def __init__(self, manifest, ticks, on_disk, listed, strays):
        self.manifest, self.ticks, self.on_disk = manifest, ticks, on_disk
        self.listed, self.strays = listed, strays

    def obsolete_for(self, keys) -> list[str]:
        """Specs (and a CONTRACTS.md the new kit no longer has) the old kit wrote or left."""
        if self.manifest is None:
            return [r for r in self.on_disk if r not in keys]
        return [r for r in self.listed if r not in keys and (r.startswith("specs/")
                                                              or r == "CONTRACTS.md")]


def _guard_old_kit(kit_dir: Path, names: list[str], *, force: bool, dry_run: bool, refuse,
                   warnings: list[str]) -> _OldKit:
    """The previous kit in this directory: refuse to clobber edits or progress (before any model
    call), or back it up under `force`. `names` are the files the new kit will write."""
    paths_to_write = [kit_dir, kit_dir / "specs", *(kit_dir / r for r in names),
                      kit_dir / "tasks.md", kit_dir / MANIFEST]
    for p in paths_to_write:
        if os.path.islink(p):
            refuse(f"refusing to write through a symlink: {p}")
    old_manifest = None
    if (kit_dir / MANIFEST).is_file():
        try:
            old_manifest = json.loads((kit_dir / MANIFEST).read_text(encoding="utf-8"))
            if not isinstance(old_manifest.get("files"), dict):
                old_manifest = None
        except (OSError, ValueError, AttributeError):
            old_manifest = None
    old_tasks = ""
    if (kit_dir / "tasks.md").is_file():
        old_tasks = (kit_dir / "tasks.md").read_text(encoding="utf-8", errors="replace")
    ticks = _old_ticks(old_tasks)
    specs_dir = kit_dir / "specs"
    on_disk = sorted(f"specs/{f.name}" for f in specs_dir.glob("*.md") if f.is_file()) \
        if specs_dir.is_dir() else []
    listed: list[str] = []
    if old_manifest is not None:
        listed = list(old_manifest["files"])
        edited = [r for r in listed if (kit_dir / r).is_file()
                  and _sha((kit_dir / r).read_text(encoding="utf-8", errors="replace"))
                  != old_manifest["files"][r]]
        foreign = [r for r in names if r not in listed and (kit_dir / r).is_file()]
        strays = [r for r in on_disk if r not in listed and r not in names]
    else:
        edited, foreign, strays = [], [], []
    old = _OldKit(old_manifest, ticks, on_disk, listed, strays)

    reasons = ([f"edited by hand: {', '.join(edited)}"] if edited else []) \
        + ([f"not written by the kit: {', '.join(foreign)}"] if foreign else []) \
        + ([f"{len(ticks)} ticked step(s): " + "; ".join(sorted(ticks))] if ticks else [])
    if reasons:
        if not force:
            refuse(f"{kit_dir} holds a kit with progress or edits ("
                   + "; ".join(reasons) + "); re-export with --force to replace it "
                   "(the old kit is backed up first)")
        else:
            old_files = sorted(set(o for o in [*names, *old.obsolete_for(names), "tasks.md",
                                               MANIFEST] if (kit_dir / o).is_file()))
            if dry_run:
                warnings.append("the old kit would be backed up first")
            else:
                warnings.append(f"old kit backed up to {_backup(kit_dir, old_files)}")
    if strays:
        warnings.append(f"specs/ holds files the kit did not write (left alone; run.sh runs "
                        f"every .md there): {', '.join(strays)}")
    return old


def _finish(store, composition, *, old: _OldKit, kit_dir: Path, target_dir: Path,
            spec_target: str, policy: dict, files: dict[str, str], titles: list, preview: list,
            n_steps: int, warnings: list[str], refusal: list[str], decisions_out: list[dict],
            plan_info: dict, plan2_info: dict, rec_info: dict, manifest_extra: dict,
            planner_name: str, dry_run: bool, zip_it: bool, deepen_result) -> dict:
    """Everything after the kit is rendered: tick carry-over into tasks.md, the manifest, the
    dry-run result, or the write, the zip, the kit record and the result."""
    obsolete = old.obsolete_for(files)
    ticks = old.ticks
    tasks: list[str] = []
    carried, dropped = 0, dict(ticks)
    for n, title, rel in titles:
        mark = " "
        if title in ticks:
            mark, carried = "x", carried + 1
            dropped.pop(title, None)
        tasks.append(f"- [{mark}] Step {n}: {title}{ticks.get(title, '')}")
        tasks.append(f"  spec: .reincarnate/{rel}")
    if dropped:
        warnings.append("ticked steps no longer in the kit: " + "; ".join(sorted(dropped)))
    files_out = {"tasks.md": "\n".join(tasks) + "\n"}
    manifest = {"composition": composition.slug, "kind": policy["kind"],
                "planner": planner_name, "engine": __version__,
                "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "files": {r: _sha(t) for r, t in files.items()},
                "steps": [{"n": n, "title": t, "spec": rel} for n, t, rel in titles]}
    manifest.update(manifest_extra)
    files_out[MANIFEST] = json.dumps(manifest, indent=2) + "\n"
    if dry_run:
        return {"target": {"dir": str(target_dir), "kind": policy["kind"],
                           "stack": list(policy["stack"]), "test_cmd": policy["test_cmd"]},
                "steps": preview, "files": {**files, **files_out}, "warnings": warnings,
                "refusal": refusal, "decisions": decisions_out, "plan": plan_info,
                "plan_cost": plan_info["cost_usd"], "plan2": plan2_info,
                "plan2_cost": plan2_info["cost_usd"], "reconcile": rec_info,
                "reconcile_cost": rec_info["cost_usd"], "planner": planner_name}
    _write_all(kit_dir, {**files, **files_out}, obsolete)

    listed_files = sorted([*files, "tasks.md", MANIFEST])
    zip_path = None
    if zip_it:
        kdir = paths.ensure(paths.kits_dir())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        zp = kdir / f"{composition.slug}-{stamp}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in listed_files:
                f = kit_dir / rel
                if f.is_file() and not f.is_symlink() and not any(
                        part.startswith(".") for part in rel.split("/")):
                    z.write(f, f.relative_to(target_dir).as_posix())
        zip_path = str(zp)

    store.add_kit(composition.id, str(kit_dir))
    composition.status = "emitted"
    store.save_composition(composition)
    out = {"kit_dir": str(kit_dir), "files": listed_files, "steps": n_steps, "zip": zip_path,
           "target": spec_target, "warnings": warnings, "decisions": decisions_out,
           "plan": plan_info, "plan_cost": plan_info["cost_usd"], "plan2": plan2_info,
           "plan2_cost": plan2_info["cost_usd"], "reconcile": rec_info,
           "reconcile_cost": rec_info["cost_usd"], "planner": planner_name}
    if deepen_result is not None:
        out["deepen"] = deepen_result
    return out


def _unanswered_warning(decided: list[dict], composition, target) -> list[str]:
    open_n = sum(1 for d in decided if not d["answer"])
    if not open_n:
        return []
    return [f"{open_n} open decision{'s are' if open_n != 1 else ' is'} unanswered; the "
            f"builder will use the defaults (reincarnate kit {composition.slug} --into "
            f"{target} --answers FILE, or answer them in the studio)"]


def _stale_warnings(stale: list) -> list[str]:
    return [f"the answer given for {did} was not applied: its question "
            f"(\"{q}\") is no longer asked; answer the current decisions "
            "again (reincarnate kit ... --decisions)" for did, q in stale]


def _decisions_out(decided: list[dict]) -> list[dict]:
    return [{"id": d["id"], "question": d["question"], "why": d["why"],
             "options": d["options"], "default": d["default"], "steps": d["steps"],
             "answer": d["answer"], "source": d["source"]} for d in decided]


def _export_idea(store, composition, target, *, policy: dict, target_dir: Path,
                 spec_target: str, cap_rows: list[dict], zip_it: bool, deepen: bool,
                 force: bool, dry_run: bool, decisions_only: bool, progress, write_progress,
                 answers, runner, warnings: list[str], refusal: list[str], refuse) -> dict:
    """The idea path of `export_kit`: a scope call (requirements and open decisions), the user's
    answers, then a plan call; catalog parts are not part of either prompt."""
    proposal = composition.proposal or {}
    parts = _cap_steps(cap_rows, store, [])  # only to deepen and to list in BLUEPRINT.md
    deepen_result = None
    todo = _not_deepened(parts)
    if deepen and todo and (decisions_only or not dry_run):
        deepen_result = _run_deepen(store, todo)
    if progress is not None and not dry_run:
        progress()
    kit_dir = target_dir / ".reincarnate"
    names = ["CLAUDE.md", "BLUEPRINT.md", "CONTRACTS.md", "run.sh"]
    old = _guard_old_kit(kit_dir, names, force=force, dry_run=dry_run, refuse=refuse,
                         warnings=warnings)
    plan_info = {"status": "off", "note": "", "cached": False, "cost_usd": 0.0}
    plan2_info = {"status": "off", "cached": False, "cost_usd": 0.0}
    rec_info = {"status": "off", "note": "not used by the idea planner", "cached": False,
                "cost_usd": 0.0, "changes": 0}

    def early(decisions_out=()) -> dict:
        """A dry-run result without files: a refusal, or the decisions alone."""
        return {"target": {"dir": str(target_dir), "kind": policy["kind"],
                           "stack": list(policy["stack"]), "test_cmd": policy["test_cmd"]},
                "steps": [], "files": {}, "warnings": warnings, "refusal": refusal,
                "decisions": list(decisions_out), "plan": plan_info,
                "plan_cost": plan_info["cost_usd"], "plan2": plan2_info,
                "plan2_cost": plan2_info["cost_usd"], "reconcile": rec_info,
                "reconcile_cost": rec_info["cost_usd"], "planner": "idea",
                **({"deepen": deepen_result} if deepen_result is not None else {})}

    if refusal and not decisions_only:
        plan_info.update(status="skipped", note="scope call not run: the export is refused")
        plan2_info["status"] = "skipped"
        return early()

    def failed(call: dict, info: dict) -> None:
        info.update(status="skipped", cached=False, cost_usd=round(call["spent"], 4))
        if call["unavailable"]:
            info["note"] = "claude is not available"
        refuse("the idea planner could not plan this kit: "
               + (call["reason"] or "claude is not available"))

    inp = idea_planner.planner_input(composition, policy)
    sres = idea_planner.scope(inp, runner=runner)
    plan_info.update(cached=sres["cached"], cost_usd=round(sres["spent"], 4))
    if sres["data"] is None:
        failed(sres, plan_info)
        return early()
    plan_info["status"] = "cached" if sres["cached"] else "used"
    warnings += [f"scope: {x}" for x in sres["problems"]]
    scope_data = sres["data"]
    stored = proposal.get("decisions")
    if answers:
        stale: list = []
        stored = answer_decisions(store, composition, answers, decisions=scope_data["decisions"],
                                  save=not dry_run, stale=stale)
        warnings += _stale_warnings(stale)
    decided = kitplan.resolve_decisions({"open_decisions": scope_data["decisions"]}, stored)
    warnings += _unanswered_warning(decided, composition, target)
    decisions_out = _decisions_out(decided)
    if decisions_only:
        return early(decisions_out)

    pres = idea_planner.plan(inp, scope_data, decided, runner=runner)
    plan2_info.update(cached=pres["cached"], cost_usd=round(pres["spent"], 4))
    if pres["data"] is None:
        failed(pres, plan2_info)
        return early(decisions_out)
    plan2_info["status"] = "cached" if pres["cached"] else "used"
    warnings += [f"plan: {x}" for x in pres["problems"]]
    plan_data = pres["data"]
    lint = idea_planner.lint_specs(policy["stack"],
                                   {s["n"]: s["spec_md"] for s in plan_data["steps"]})
    files, titles, preview = idea_planner.files(
        inp, composition, policy, scope_data, plan_data, decided, spec_target=spec_target,
        target_dir=target_dir, parts=[(st["cap"].name, st["cap"].donor) for st in parts],
        lint=lint)
    for n, msgs in sorted(lint.items()):
        warnings += [f"contract lint, step {n}: {m}" for m in msgs]
    clash = [r for r in files if r in old.strays]
    if clash:
        if not force:
            refuse(f"specs/ holds files the kit did not write that this plan would overwrite: "
                   f"{', '.join(clash)}; move them, or re-export with --force (they are backed "
                   "up first)")
        elif dry_run:
            warnings.append("the files this plan overwrites would be backed up first")
        else:
            warnings.append(f"files the kit did not write backed up to "
                            f"{_backup(kit_dir, clash)}")
    if write_progress is not None and not dry_run:
        write_progress()
    extra = {"scope": {"key": sres["key"], "model": sres["model"],
                       "cost_usd": sres["cost_usd"], "data": scope_data},
             "plan": {"key": pres["key"], "model": pres["model"], "cost_usd": pres["cost_usd"],
                      "data": {**plan_data, "open_decisions": scope_data["decisions"],
                               "requirements": scope_data["requirements"]}}}
    return _finish(store, composition, old=old, kit_dir=kit_dir, target_dir=target_dir,
                   spec_target=spec_target, policy=policy, files=files, titles=titles,
                   preview=preview, n_steps=len(titles), warnings=warnings, refusal=refusal,
                   decisions_out=decisions_out, plan_info=plan_info, plan2_info=plan2_info,
                   rec_info=rec_info, manifest_extra=extra, planner_name="idea",
                   dry_run=dry_run, zip_it=zip_it, deepen_result=deepen_result)


def export_kit(store, composition, target, *, zip_it: bool = False,
               notes: str | None = None, deepen: bool = False,
               allow_missing: bool = False, force: bool = False,
               dry_run: bool = False, progress=None, plan: bool = True, plan_runner=None,
               write_progress=None, answers: dict | None = None,
               decisions_only: bool = False, reconcile_runner=None,
               planner: str | None = None) -> dict:
    """Write the kit. `dry_run` renders it the same way but writes, deletes, deepens and records
    nothing, and reports what would refuse the real export in `refusal` instead of raising.
    `plan`: run (or reuse from the manifest) the contracts and scope pass, one model call that
    never blocks the export; `plan_runner(prompt, model)` replaces the real call in tests.
    `progress()` fires once the deepen pass is over, `write_progress()` once the plan is.
    `answers` ({decision id: answer, or {question, answer}}, see `answer_decisions`) are
    validated against the plan and saved on the composition (not on a dry run) before the kit
    is rendered. `decisions_only` is a dry run that still deepens when `deepen` is set, so the
    decisions it lists come from the same plan the real export makes; it is a dry run
    that asks for the plan even when the export would be refused; read `decisions` in the result.
    The reconcile pass (a second model call, `reconcile_runner(prompt, model)` in tests) follows
    the plan whenever it was used: it rewrites or drops the specs' checks and key values that
    conflict with the scope, the answers and the contracts, and the behavior paragraphs. When
    any decision is answered, the contracts are planned a second time with the answers (`plan2`
    in the manifest and the result; the decision list stays the first plan's). `kitplan.lint`'s
    messages go to CONTRACTS.md (Notes), the specs (Contract notes) and the warnings. None of
    these passes blocks the export.

    `planner` picks how the steps are planned: "idea" (default; two Opus calls from the idea
    and the stack, catalog parts unused; `plan_runner` serves both) or "parts" (one step per
    catalog part, the passes above). Unset, $REINCARNATE_PLANNER decides; `--no-plan` means
    "parts". A bad value raises ValueError."""
    dry_run = dry_run or decisions_only
    which = idea_planner.choose(planner)
    proposal = composition.proposal or {}
    cap_rows = sorted(proposal.get("capabilities") or [], key=lambda r: r.get("order", 0))
    gaps = list(proposal.get("gaps") or [])
    warnings: list[str] = []
    if which == "idea" and not plan:
        which = "parts"
        warnings.append("--no-plan uses the parts planner")
    refusal: list[str] = []

    def refuse(msg: str) -> None:
        if not dry_run:
            raise ValueError(msg)
        refusal.append(msg)

    def stop_preview() -> dict:
        return {"target": {"dir": "", "kind": "", "stack": [], "test_cmd": ""}, "steps": [],
                "files": {}, "warnings": warnings, "refusal": refusal, "decisions": [],
                "plan": {"status": "off", "note": "", "cached": False, "cost_usd": 0.0},
                "plan_cost": 0.0,
                "plan2": {"status": "off", "cached": False, "cost_usd": 0.0}, "plan2_cost": 0.0,
                "reconcile": {"status": "off", "note": "", "cached": False, "cost_usd": 0.0,
                              "changes": 0},
                "reconcile_cost": 0.0}

    if which == "parts" and not cap_rows and not gaps:
        refuse("composition has no capabilities and no gaps; nothing to build")
        return stop_preview()

    try:
        target_dir, spec_target, project = _resolve_target(store, target, composition, warnings)
    except ValueError as e:
        if not dry_run:
            raise
        refusal.append(str(e))
        return stop_preview()
    if which == "idea":
        policy = _target_policy(store, project, target_dir, proposal.get("stack") or "")
        warnings += _kind_warnings(composition, policy, spec_target)
        return _export_idea(
            store, composition, target, policy=policy, target_dir=target_dir,
            spec_target=spec_target, cap_rows=cap_rows, zip_it=zip_it, deepen=deepen, force=force,
            dry_run=dry_run, decisions_only=decisions_only, progress=progress,
            write_progress=write_progress, answers=answers, runner=plan_runner,
            warnings=warnings, refusal=refusal, refuse=refuse)
    part_problems: list[str] = []
    caps = _cap_steps(cap_rows, store, part_problems)
    if part_problems and not allow_missing:
        refuse("the composition no longer matches the catalog (fix it, or export "
               "anyway with --allow-missing): " + "; ".join(part_problems))
    problems = part_problems + [str(p) for p in proposal.get("problems") or []]
    warnings += problems
    if not caps and not gaps:
        refuse("no buildable steps: every referenced capability is missing")
        return stop_preview()
    deepen_result = None
    todo = _not_deepened(caps)
    if deepen and todo and (decisions_only or not dry_run):
        deepen_result = _run_deepen(store, todo)
        for st in caps:
            st["cap"] = store.get_capability(st["cap"].id) or st["cap"]
    if progress is not None and not dry_run:
        progress()
    glue_src = notes.strip() if notes and notes.strip() else (proposal.get("glue") or "").strip()
    steps = _order_steps(caps, gaps, _glue_steps(proposal, caps, glue_src))
    left = len(_not_deepened(caps))
    if left:
        warnings.append(f"{left} parts are not deepened: reincarnate deepen --composition "
                        f"{composition.slug}, then re-export the kit")
    policy = _target_policy(store, project, target_dir, proposal.get("stack") or "")
    is_new = policy["kind"] == "new"
    stack, test_cmd = policy["stack"], policy["test_cmd"]
    ui_tools = salvage.ui_test_tools(stack) if is_new else []
    warnings += _kind_warnings(composition, policy, spec_target)

    first = 1
    if is_new:
        steps.insert(0, {"kind": "scaffold"})
        first = 0
    for n, st in enumerate(steps, first):
        st["n"] = n
    in_kit = {st["cap"].id: (st["cap"].name, st["n"]) for st in steps if st["kind"] == "cap"}
    for st in steps:
        if st["kind"] == "gap":
            gap = st["gap"]
            ccap = (store.get_capability(_as_int(gap.get("consumer_capability_id")))
                    if gap.get("consumer_capability_id") is not None else None)
            st["surface"] = (str(gap.get("token") or "").startswith(("http:", "ui:"))
                             or (ccap is not None and (_provides(ccap, "http:")
                                                       or _provides(ccap, "ui:"))))
    resolve = _id_resolver(store, in_kit)
    auth = _auth_step(steps)
    noted = [st["n"] for st in steps if _needs_auth_note(st, auth)]

    kit_dir = target_dir / ".reincarnate"
    decided: list[dict] = []  # the plan's open decisions with their answers, set after the pass
    captured: dict[int, dict] = {}  # step -> the checks and key values its spec renders
    override: dict[int, dict] = {}  # step -> the reconcile pass's replacements for them
    spec_changes: dict[int, list[dict]] = {}  # step -> what the pass changed, for its spec
    lint_notes: dict = {"kit": [], "steps": {}}  # the contract lints: CONTRACTS.md and the specs
    labels = {st["n"]: (schema.clip_words(_name(st["cap"].name), 60) if st["kind"] == "cap" else
                        {"scaffold": "scaffold", "glue": "glue"}.get(st["kind"], "build fresh"))
              for st in steps}

    def render(pl: dict | None):
        """The kit files, task titles and preview rows; `pl` is the validated contracts plan."""
        for e in (pl or {}).get("steps") or []:  # glue and build-fresh checks live in the plan
            if e["checks"]:
                captured.setdefault(e["n"], {"checks": list(e["checks"]), "key_values": []})
        files: dict[str, str] = {}
        titles: list[tuple[int, str, str]] = []  # (step, task title, spec relpath)
        blueprint_steps: list[str] = []
        preview: list[dict] = []
        for st in steps:
            n = st["n"]
            if st["kind"] == "scaffold":
                fname = "00-scaffold.md"
                text = salvage.build_scaffold_spec(stack, test_cmd, policy["ecosystems"],
                                                   target_name=spec_target, ui_tools=ui_tools,
                                                   auth=auth)
                if pl:  # the builder takes no notes: the plan's sections go on the end
                    sect = kitplan.step_notes(pl, n, "scaffold", labels, decided)
                    text = text.rstrip("\n") + "\n" + ("\n".join(sect) + "\n" if sect else "")
                title = "scaffold the project"
                blueprint_steps.append(f"{n}. Step 0 — scaffold the project "
                                       f"({', '.join(stack) or 'stack to be chosen'}; "
                                       f"tests: `{test_cmd}`)")
            elif st["kind"] == "cap":
                cap = st["cap"]
                fname = f"{n:02d}-{slugify(cap.name)[:SLUG_MAX].strip('-') or 'part'}.md"
                checks = _cap_checks(cap, proposal)
                extra: list[str] = []
                if ui_tools and _provides(cap, "ui:"):
                    checks.append(salvage.COMPONENT_CHECK)
                needs = _needs_lines(st, steps)
                if needs and not (pl and kitplan.has_uses(pl, n)):
                    extra.append(needs)
                if n in noted:
                    extra.append(_auth_note(auth))
                if auth and n == auth["step"]:
                    extra.append("**Authorization guard:** Wire the authorization guard to this "
                                 "step's identity and roles so every endpoint added before this "
                                 "step is gated.")
                    if noted or any(_surface(p) for p in steps if p["n"] < n):
                        checks.append("An anonymous request to an endpoint added before this step "
                                      "is rejected")
                kvs = [kv for kv in getattr(cap, "key_values", None) or []
                       if isinstance(kv, dict)]
                if pl:
                    captured.setdefault(n, {"checks": list(checks), "key_values": kvs,
                                            "prose": salvage.spec_prose(store, cap, spec_target)})
                if n in override:
                    checks, kvs = override[n]["checks"], override[n]["key_values"]
                prose = override[n].get("prose") or {} if n in override else {}
                text = salvage.build_clean_room_spec(store, cap, target_slug=spec_target,
                                                     project=project, include_header=True,
                                                     policy=policy, acceptance=checks,
                                                     key_values=kvs, what=prose.get("what"),
                                                     design_notes=prose.get("design_notes"),
                                                     intent=prose.get("intent"),
                                                     notes=extra + (
                                                         kitplan.step_notes(
                                                             pl, n, "cap", labels, decided)
                                                         if pl else []))
                cname, cdonor = _name(cap.name), _name(cap.donor, 200)
                title = f"{cname} (from {cdonor})"
                role = f" - {_name(st['role'], 200)}" if st["role"] else ""
                blueprint_steps.append(f"{n}. Step {n} — {cname} (from {cdonor}){role}")
            elif st["kind"] == "glue":
                fname = f"{n:02d}-glue-{slugify(st['text'])[:SLUG_MAX].strip('-') or 'step'}.md"
                joins = [f"{_name(in_kit[cid][0])} (step {in_kit[cid][1]})" for cid in st["joins"]
                         if cid in in_kit]
                related = [f"{_name(in_kit[cid][0])} (step {in_kit[cid][1]})"
                           for cid in st.get("related") or [] if cid in in_kit]
                text = salvage.build_glue_spec(st["req"], st["text"], target_slug=spec_target,
                                               policy=policy, glue_lines=st["lines"], joins=joins,
                                               related=related,
                                               notes=([_auth_note(auth)] if n in noted else [])
                                               + (kitplan.step_notes(pl, n, "glue", labels, decided)
                                                  if pl else []) or None)
                gtext = _name(st["text"], 200)
                title = f"Glue: {gtext}"
                blueprint_steps.append(f"{n}. Step {n} — Glue: {gtext}")
            else:
                gap = st["gap"]
                need = _name(gap.get("need") or "unspecified", 200) or "unspecified"
                fname = f"{n:02d}-fresh-{slugify(need)[:SLUG_MAX].strip('-') or 'gap'}.md"
                consumer = None
                cid = _as_int(gap.get("consumer_capability_id"))
                if gap.get("token") and cid is not None:
                    ccap = store.get_capability(cid)
                    if ccap is not None:
                        consumer = {"name": ccap.name, "what": ccap.what,
                                    "step": in_kit.get(cid, (None, None))[1]}
                gnotes = _gap_notes(gap, in_kit)
                if n in noted:
                    gnotes.append(_auth_note(auth))
                text = salvage.build_fresh_spec(gap, target_slug=spec_target, project=project,
                                                store=store, policy=policy, consumer=consumer,
                                                notes=gnotes + (kitplan.step_notes(
                                                    pl, n, "gap", labels, decided)
                                                    if pl else []) or None)
                title = f"build fresh — {need}"
                blueprint_steps.append(f"{n}. Step {n} — build fresh: {need}")
            titles.append((n, title, f"specs/{fname}"))
            if lint_notes["steps"].get(n):
                text = (text.rstrip("\n") + "\n\n**Contract notes:**\n\n"
                        + "\n".join(f"- {x}" for x in lint_notes["steps"][n]) + "\n")
            sect = reconcile.changed_section(spec_changes.get(n) or [])
            if sect:  # the last section: what the reconcile pass removed or rewrote, and why
                text = text.rstrip("\n") + "\n\n" + "\n".join(sect) + "\n"
            files[f"specs/{fname}"] = text = resolve(text)
            is_cap = st["kind"] == "cap"
            preview.append({
                "n": n, "kind": "cap" if is_cap else st["kind"], "title": title,
                "donor": _name(st["cap"].donor, 200) if is_cap else "",
                "deepened": bool(is_cap and getattr(st["cap"], "deepened_at", "")),
                "guard_note": bool(is_cap and auth and n == auth["step"]) or n in noted,
                "needs": _needs_list(st, steps) if is_cap else [],
                "checks": _check_count(text), "spec_head": "\n".join(text.splitlines()[:40])})

        # donors table
        donors: dict[str, dict] = {}
        for st in steps:
            if st["kind"] == "cap":
                d = donors.setdefault(_name(st["cap"].donor, 200), {
                    "license": _name(st["cap"].license or "unknown", schema.MAX_LICENSE),
                    "caps": []})
                d["caps"].append(_name(st["cap"].name))
        donor_rows = [f"| {_table_cell(k)} | {_table_cell(v['license'])} | "
                      f"{_table_cell(', '.join(v['caps']))} |" for k, v in donors.items()]

        flags = []
        for f in proposal.get("license_flags") or []:
            if isinstance(f, dict):
                level = _name(f.get("level") or f.get("severity") or "warn", 20)
                who = _name(f.get("donor") or f.get("capability") or "", 200)
                lic = _name(f.get("license") or "", schema.MAX_LICENSE)
                msg = _name(f.get("message") or f.get("note") or "", 300)
                flags.append(f"- [{level}] " + " ".join(x for x in (who, lic, msg) if x))
            else:
                flags.append(f"- {_name(f, 300)}")
        glue = salvage.gate_text(glue_src)
        risks = [f"- {_name(r, 300)}" for r in proposal.get("risks") or []]
        created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        bp = [f"# {_name(composition.name, 120)}", "", salvage.BANNER, "",
              f"**Pitch:** {salvage.gate_text(composition.pitch)}", "",
              f"**Idea:** {salvage.gate_text(composition.idea)}", "",
              f"**Target:** {spec_target}", "",
              f"**Created:** {created}", "", f"**Engine:** reincarnate {__version__}", "",
              "## Donors", "",
              "| Donor | License | Capabilities used |", "| --- | --- | --- |", *donor_rows, "",
              "## The clean-room air gap", "",
              "Donor source was read only during comprehension. The builder works from the "
              "behavior specs in `specs/`, never from donor code. Donor file paths in the specs "
              "are references only (DO NOT COPY).", ""]
        bp += ["License flags:", *flags, ""] if flags else ["License flags: (none)", ""]
        bp += ["## Steps", "", *blueprint_steps, "", "## Glue", "", glue or "(none)", ""]
        if proposal.get("wiring"):
            bp += ["## Wiring", "", *_wiring_lines(proposal["wiring"]), ""]
        if proposal.get("coverage"):
            bp += ["## Requirement coverage", "", *_coverage_lines(proposal), ""]
        bp += ["## Risks", "", *(risks or ["(none)"]), ""]
        if problems:
            bp += ["## Problems", "", *[f"- {_name(p, 300)}" for p in problems], ""]
        files["BLUEPRINT.md"] = resolve("\n".join(bp))

        allowed = salvage.builder_bash_allowlist(
            policy["ecosystems"] or salvage.manifest_ecosystems(target_dir), test_cmd)
        allowed_text = ", ".join(f"`{a[len('Bash('):-len(':*)')]}`" for a in allowed) or "(none)"
        tests_line = f"Test command: `{test_cmd}`" if test_cmd else "Test command: (none set)"
        if is_new:
            tests_line += " (set in step 0)."
        if ui_tools:
            tests_line += "\n\n" + salvage.component_test_line(ui_tools)
        cl = [f"# Building {_name(composition.name, 120)}", "", salvage.BANNER, "", "## Stack", "",
              *([f"- {x}" for x in stack] or ["- (follow the existing project conventions)"]), "",
              "## Dependencies", "", *salvage.dependency_lines(policy), "",
              "## Tests", "", tests_line, "",
              "## Checks", "",
              "After each step the test command runs, then Code Read's turn check "
              "(`cr check-turn`: secrets, tripwires and new dependencies in the step's diff) "
              "when it is installed. "
              "A failing check stops the run.", "",
              "Do not edit `.coderead/`, `.reincarnate/state.json`, `.reincarnate/checks/` or the "
              "tick marks in `tasks.md`; run.sh ticks a step once it passes.", "",
              f"Outside a sandbox you may run only these Bash commands: {allowed_text}.", "",
              *(["How the parts connect is in BLUEPRINT.md, section Glue.", ""] if glue else []),
              *([kitplan.CONTRACTS_LINE, ""] if pl else []),
              "## Clean-room rules", "", *salvage.constraint_lines(policy), "",
              "Work one step at a time from .reincarnate/specs/; stop after each step for review.",
              ""]
        files["CLAUDE.md"] = "\n".join(cl)
        files["run.sh"] = (RUN_SH.replace("__TEST_CMD__", _shell_quote(test_cmd))
                           .replace("__ALLOWED__", " ".join(_shell_quote(a) for a in allowed))
                           .replace("__NEW_TARGET__", "1" if is_new else "0"))
        if pl:
            contracts = kitplan.contracts_md(pl, labels, salvage.BANNER, decided)
            if lint_notes["kit"]:
                contracts = (contracts.rstrip("\n") + "\n\n## Notes\n\n"
                             + "\n".join(f"- {x}" for x in lint_notes["kit"]) + "\n")
            files["CONTRACTS.md"] = resolve(contracts)
        return files, titles, preview

    # the previous kit in this directory: refuse to clobber edits or progress, before any model
    # call; CONTRACTS.md counts as one of the kit's files whenever the pass is wanted
    files, titles, preview = render(None)
    names = [*files, *(["CONTRACTS.md"] if plan else [])]
    old = _guard_old_kit(kit_dir, names, force=force, dry_run=dry_run, refuse=refuse,
                         warnings=warnings)
    old_manifest = old.manifest

    plan_info = {"status": "off", "note": "", "cached": False, "cost_usd": 0.0}
    pl, plan_res = None, {}
    rec_info = {"status": "off", "note": "", "cached": False, "cost_usd": 0.0, "changes": 0}
    rec_res: dict = {}
    plan2_info = {"status": "off", "cached": False, "cost_usd": 0.0}
    plan2_res: dict = {}
    if not plan:
        if answers:
            raise ValueError("cannot save the answers with --no-plan: the open decisions come "
                             "from the contracts pass")
        plan_info["note"] = "contracts pass not run (--no-plan)"
        rec_info["note"] = "reconcile pass not run (--no-plan)"
    elif refusal and not decisions_only:
        plan_info.update(status="skipped", note="contracts pass not run: the export is refused")
        rec_info.update(status="skipped", note="reconcile pass not run: the export is refused")
    else:
        plan_res = kitplan.plan(store, steps, proposal, policy, runner=plan_runner,
                                cache=(old_manifest or {}).get("plan"))
        plan_info.update(cached=plan_res["cached"], cost_usd=round(plan_res["spent"], 4))
        pl = plan_res["plan"]
        if pl:
            plan_info["status"] = "cached" if plan_res["cached"] else "used"
            warnings += [f"contracts: {x}" for x in plan_res["problems"]]
            stored = (composition.proposal or {}).get("decisions")
            if answers:
                stale: list = []
                stored = answer_decisions(store, composition, answers,
                                          decisions=pl.get("open_decisions") or [],
                                          save=not dry_run, stale=stale)
                warnings += _stale_warnings(stale)
            decided[:] = kitplan.resolve_decisions(pl, stored)
            warnings += _unanswered_warning(decided, composition, target)
            answered = [{"id": d["id"], "question": d["question"], "answer": d["answer"]}
                        for d in decided if d["answer"]]
            if answered and _plan_takes_decisions():
                # the contracts again, now that the decisions are known; the decision list stays
                plan2_res = kitplan.plan(store, steps, proposal, policy, runner=plan_runner,
                                         cache=(old_manifest or {}).get("plan2"),
                                         decisions=answered)
                plan2_info.update(cached=plan2_res["cached"],
                                  cost_usd=round(plan2_res["spent"], 4))
                if plan2_res["plan"]:
                    plan2_info["status"] = "cached" if plan2_res["cached"] else "used"
                    warnings += [f"contracts (after the decisions): {x}"
                                 for x in plan2_res["problems"]]
                    pl = {**plan2_res["plan"], "open_decisions": pl.get("open_decisions") or []}
                else:
                    plan2_info["status"] = "skipped"
                    warnings.append("contracts were not re-planned after the decisions: "
                                    + (plan2_res["reason"] or "claude is not available"))
            files, titles, preview = render(pl)  # also collects the lists to reconcile
            rec_res = reconcile.reconcile(
                store, steps, pl, decided, proposal, captured, runner=reconcile_runner,
                cache=(old_manifest or {}).get("reconcile"))
            rec_info.update(cached=rec_res["cached"], cost_usd=round(rec_res["spent"], 4))
            if rec_res["result"] is None:
                rec_info["status"] = "skipped"
                if rec_res["unavailable"]:
                    rec_info["note"] = "reconcile pass not run: claude is not available"
                else:
                    rec_info["note"] = f"reconcile pass skipped: {rec_res['reason']}"
                    warnings.append(rec_info["note"])
            else:
                rec_info["status"] = "cached" if rec_res["cached"] else "used"
                warnings += [f"reconcile: {x}" for x in rec_res["problems"]]
                applied = reconcile.apply(pl, captured, rec_res["result"])
                rec_info["changes"] = reconcile.count(applied)
                if rec_info["changes"]:
                    override.update(applied["steps"])
                    spec_changes.update(applied["changes"])
                    pl = applied["plan"]
                    warnings.append(f"reconcile pass changed {rec_info['changes']} line(s) to "
                                    "match the decisions and contracts (listed in each spec)")
                    files, titles, preview = render(pl)
            lint = getattr(kitplan, "lint", None)
            if lint is not None:
                try:
                    found = lint(pl, policy, {n: files[rel] for n, _, rel in titles})
                except Exception as e:  # noqa: BLE001 - a lint never blocks the export
                    found = []
                    warnings.append(f"contract lints skipped: {type(e).__name__}: {e}"[:300])
                known = {n for n, _, _ in titles}
                for f in found if isinstance(found, list) else []:
                    msg = salvage.gate_line(f.get("message"), n=400) if isinstance(f, dict) else ""
                    n = f.get("step") if msg else None
                    if not msg:
                        continue
                    if isinstance(n, int) and not isinstance(n, bool) and n in known:
                        lint_notes["steps"].setdefault(n, []).append(msg)
                        warnings.append(f"contract lint, step {n}: {msg}")
                    else:
                        lint_notes["kit"].append(msg)
                        warnings.append(f"contract lint: {msg}")
                if lint_notes["kit"] or lint_notes["steps"]:
                    files, titles, preview = render(pl)
        else:
            plan_info["status"] = "skipped"
            rec_info.update(status="skipped", note="reconcile pass not run: no plan")
            if answers:
                raise ValueError("cannot save the answers: the plan with the open decisions "
                                 f"is not available ({plan_res['reason'] or 'claude is missing'})")
            if plan_res["unavailable"]:
                plan_info["note"] = "contracts pass not run: claude is not available"
            else:
                plan_info["note"] = f"contracts pass skipped: {plan_res['reason']}"
                warnings.append(plan_info["note"])
    if plan and write_progress is not None and not dry_run:
        write_progress()
    decisions_out = _decisions_out(decided)
    place = getattr(salvage, "placeholder_warnings", None)
    for st in steps if place is not None else []:  # values that look like template residue
        if st["kind"] == "cap":
            warnings += [f"{_name(st['cap'].name)}: looks like a placeholder, check it: {w}"
                         for w in place(st["cap"])]

    extra: dict = {}
    if pl:
        extra["plan"] = {"key": plan_res["key"], "model": plan_res["model"],
                         "cost_usd": plan_res["cost_usd"], "data": plan_res["plan"]}
    if plan2_res.get("plan"):
        extra["plan2"] = {"key": plan2_res["key"], "model": plan2_res["model"],
                          "cost_usd": plan2_res["cost_usd"], "data": plan2_res["plan"]}
    if rec_res.get("key") and rec_res.get("result") is not None:
        extra["reconcile"] = {"key": rec_res["key"], "model": rec_res["model"],
                              "cost_usd": rec_res["cost_usd"], "data": rec_res["result"]}
    return _finish(store, composition, old=old, kit_dir=kit_dir, target_dir=target_dir,
                   spec_target=spec_target, policy=policy, files=files, titles=titles,
                   preview=preview, n_steps=len(steps), warnings=warnings, refusal=refusal,
                   decisions_out=decisions_out, plan_info=plan_info, plan2_info=plan2_info,
                   rec_info=rec_info, manifest_extra=extra, planner_name="parts",
                   dry_run=dry_run, zip_it=zip_it, deepen_result=deepen_result)


def run_cli(store, args: Any) -> dict:
    comp = store.get_composition(args.composition)
    if comp is None:
        return {"error": f"composition {args.composition} not found"}
    notes = None
    notes_file = getattr(args, "notes", None)
    if notes_file:
        try:
            notes = Path(notes_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            why = e.strerror if isinstance(e, OSError) and e.strerror else "not UTF-8 text"
            return {"error": f"cannot read --notes file {notes_file}: {why}"}
    answers = None
    answers_file = getattr(args, "answers", None)
    if answers_file:
        try:
            answers = json.loads(Path(answers_file).expanduser().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as e:
            why = e.strerror if isinstance(e, OSError) and e.strerror else "not a JSON file"
            return {"error": f"cannot read --answers file {answers_file}: {why}"}
        if not isinstance(answers, dict):
            return {"error": f"--answers file {answers_file} must hold a JSON object like "
                    '{"D1": "answer"}'}
    try:
        if getattr(args, "decisions", False):
            out = export_kit(store, comp, args.into, decisions_only=True,
                             deepen=bool(getattr(args, "deepen", False)),
                             allow_missing=bool(getattr(args, "allow_missing", False)),
                             planner=getattr(args, "planner", None))
            res = {"decisions": out["decisions"]}
            if out.get("deepen") is not None:
                res["deepen"] = out["deepen"]
            return res
        return export_kit(store, comp, args.into, zip_it=bool(getattr(args, "zip", False)),
                          notes=notes, deepen=bool(getattr(args, "deepen", False)),
                          allow_missing=bool(getattr(args, "allow_missing", False)),
                          force=bool(getattr(args, "force", False)),
                          plan=not getattr(args, "no_plan", False), answers=answers,
                          planner=getattr(args, "planner", None))
    except ValueError as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": " ".join(f"cannot write the kit: {e.strerror or e}".split())
                + (f" ({e.filename})" if e.filename else "")}
