"""Export a buildable `.reincarnate/` kit for a composition."""

from __future__ import annotations

import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths, salvage
from .models import Project

RUN_SH = r'''#!/usr/bin/env bash
# Reincarnate kit driver: builds each step with `claude -p`, tests, pauses for review.
# Usage: .reincarnate/run.sh [--yes]   (REINCARNATE_MODEL overrides the model, default sonnet)
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE/.." || exit 1
YES=0
[ "${1:-}" = "--yes" ] && YES=1
MODEL="${REINCARNATE_MODEL:-sonnet}"
TEST_CMD=__TEST_CMD__
for spec in .reincarnate/specs/*.md; do
  f="$(basename "$spec")"
  N=$((10#${f%%-*}))
  echo "=== Step $N: $f ==="
  PROMPT="You are building step $N of the Reincarnate kit in $(pwd)."
  PROMPT+=" Read .reincarnate/CLAUDE.md and .reincarnate/specs/$f, then implement it."
  PROMPT+=" Follow the clean-room rules exactly. Do not read any donor repository."
  claude -p "$PROMPT" --permission-mode acceptEdits --max-turns 60 --model "$MODEL"
  if [ -n "$TEST_CMD" ]; then
    echo "--- running tests: $TEST_CMD ---"
    bash -c "$TEST_CMD" || echo "!! tests failed after step $N"
  fi
  if [ "$YES" -eq 0 ]; then
    read -r -p "Review step $N, then press Enter to continue or Ctrl-C to stop" _
  fi
done
echo "All steps done."
'''


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "app"


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _resolve_target(store, target: str) -> tuple[Path, str, Project | None]:
    """Return (target dir, spec target slug, project for stack/test info)."""
    target = str(target)
    if target.startswith(salvage.NEW_PREFIX):
        name = target[len(salvage.NEW_PREFIX):].strip()
        if not name:
            raise ValueError("new: target needs a name")
        d = paths.data_dir() / "new" / slugify(name)
        return d, f"{salvage.NEW_PREFIX}{name}", None
    proj = store.get_project(target)
    if proj is not None:
        return Path(proj.path).expanduser(), proj.slug, proj
    d = Path(target).expanduser()
    if d.is_dir():
        prof = store.get_profile()
        synth = Project(slug=d.name, path=str(d), stack=list(prof.stack) if prof else [])
        return d, d.name, synth
    raise ValueError(f"unknown target: {target} (not a project slug, new:<name>, or directory)")


def _table_cell(s: str) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ")


def _ref_name(x) -> str:
    if isinstance(x, dict):
        return str(x.get("name") or x.get("id") or "?")
    return str(x)


def _wiring_lines(w: dict) -> list[str]:
    """`proposal["wiring"]` (from a picked design option) as markdown bullets."""
    out = []
    for x in w.get("wired") or []:
        out.append(f"- wired: {_ref_name(x.get('consumer'))} needs `{x.get('token')}` from "
                   f"{_ref_name(x.get('provider'))}")
    for x in w.get("unmet") or []:
        extra = ""
        if x.get("rejected_provider"):
            extra = f" (link to {_ref_name(x['rejected_provider'])} rejected)"
        elif x.get("candidates"):
            extra = " (catalog candidates: " + ", ".join(
                _ref_name(c) for c in x["candidates"]) + ")"
        out.append(f"- unmet: {_ref_name(x.get('consumer'))} needs `{x.get('token')}`{extra}")
    labels = {"settings": "setting", "external": "external", "owned": "owned state",
              "platform": "platform"}
    for k, label in labels.items():
        for x in w.get(k) or []:
            out.append(f"- {label}: {_ref_name(x.get('consumer'))} needs `{x.get('token')}`")
    return out or ["(no interface needs)"]


def _coverage_lines(proposal: dict) -> list[str]:
    texts = {r.get("id"): r.get("text", "") for r in proposal.get("requirements") or []
             if isinstance(r, dict)}
    names = {f"c{c.get('capability_id')}": c.get("name", "")
             for c in proposal.get("capabilities") or [] if isinstance(c, dict)}
    rows = ["| Requirement | Kind | Capability |", "| --- | --- | --- |"]
    for row in proposal.get("coverage") or []:
        if not isinstance(row, dict):
            continue
        req = row.get("req", "")
        label = f"{req}: {texts[req]}" if texts.get(req) else str(req)
        cap = row.get("cap") or ""
        rows.append(f"| {_table_cell(label)} | {_table_cell(row.get('kind', ''))} | "
                    f"{_table_cell(names.get(cap) or cap or '-')} |")
    return rows


def _split_steps(cap_rows: list[dict], gaps: list[dict], store, problems: list[str]):
    """Capability steps in proposal order; a gap with `before_capability_id` goes right before
    that capability, every other gap after all capability steps."""
    caps = []
    for row in cap_rows:
        cap = store.get_capability(row.get("capability_id"))
        if cap is None:
            problems.append(f"capability {row.get('capability_id')} not found; step skipped")
            continue
        caps.append({"kind": "cap", "cap": cap, "role": row.get("role") or ""})
    kit_ids = {st["cap"].id for st in caps}
    before: dict[int, list[dict]] = {}
    tail: list[dict] = []
    for gap in gaps:
        bid = _as_int(gap.get("before_capability_id")) if isinstance(gap, dict) else None
        st = {"kind": "gap", "gap": gap if isinstance(gap, dict) else {"need": str(gap)}}
        if bid in kit_ids:
            before.setdefault(bid, []).append(st)
        else:
            tail.append(st)
    steps: list[dict] = []
    for st in caps:
        steps += before.pop(st["cap"].id, [])
        steps.append(st)
    return steps + tail


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
            return f"{name} (step {step})"
        if cid not in names:
            cap = store.get_capability(cid)
            names[cid] = cap.name if cap else None
        return names[cid]

    return lambda text: salvage.resolve_cids(text, lookup)


def _target_policy(store, composition, proposal, target_dir: Path,
                   project: Project | None) -> dict:
    """New vs existing target, its stack and test command (see DESIGN "Kit generation")."""
    prof = store.get_profile()
    profile_stack = list(prof.stack) if prof else []
    is_new = composition.target_kind == "new" or not salvage.has_manifest(target_dir)
    if not is_new:
        stack = list(project.stack) if project and project.stack else profile_stack
        test_cmd = (project.test_cmd if project else "") or salvage.manifest_command(target_dir)
        return {"kind": "existing", "stack": stack, "test_cmd": test_cmd, "ecosystems": []}
    stack = salvage.split_stack(proposal.get("stack") or "") or profile_stack
    ecos = salvage.ecosystems(stack)
    return {"kind": "new", "stack": stack, "test_cmd": salvage.derive_test_cmd(ecos),
            "ecosystems": ecos}


def export_kit(store, composition, target, *, zip_it: bool = False,
               notes: str | None = None) -> dict:
    proposal = composition.proposal or {}
    cap_rows = sorted(proposal.get("capabilities") or [], key=lambda r: r.get("order", 0))
    gaps = list(proposal.get("gaps") or [])
    if not cap_rows and not gaps:
        raise ValueError("composition has no capabilities and no gaps; nothing to build")

    target_dir, spec_target, project = _resolve_target(store, target)
    problems: list[str] = []
    steps = _split_steps(cap_rows, gaps, store, problems)
    if not steps:
        raise ValueError("no buildable steps: every referenced capability is missing")
    policy = _target_policy(store, composition, proposal, target_dir, project)
    is_new = policy["kind"] == "new"
    stack, test_cmd = policy["stack"], policy["test_cmd"]

    first = 1
    if is_new:
        steps.insert(0, {"kind": "scaffold"})
        first = 0
    for n, st in enumerate(steps, first):
        st["n"] = n
    in_kit = {st["cap"].id: (st["cap"].name, st["n"]) for st in steps if st["kind"] == "cap"}
    resolve = _id_resolver(store, in_kit)

    kit_dir = target_dir / ".reincarnate"
    specs_dir = kit_dir / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    for old in specs_dir.glob("*.md"):
        old.unlink()

    tasks: list[str] = []
    blueprint_steps: list[str] = []
    for st in steps:
        n = st["n"]
        if st["kind"] == "scaffold":
            fname = "00-scaffold.md"
            text = salvage.build_scaffold_spec(stack, test_cmd, policy["ecosystems"],
                                               target_name=spec_target)
            tasks.append("- [ ] Step 0: scaffold the project")
            blueprint_steps.append(f"{n}. Step 0 — scaffold the project "
                                   f"({', '.join(stack) or 'stack to be chosen'}; "
                                   f"tests: `{test_cmd}`)")
        elif st["kind"] == "cap":
            cap = st["cap"]
            fname = f"{n:02d}-{slugify(cap.name)}.md"
            text = salvage.build_clean_room_spec(store, cap, target_slug=spec_target,
                                                 project=project, include_header=True,
                                                 policy=policy,
                                                 acceptance=_cap_checks(cap, proposal))
            tasks.append(f"- [ ] Step {n}: {cap.name} (from {cap.donor})")
            role = f" - {st['role']}" if st["role"] else ""
            blueprint_steps.append(f"{n}. Step {n} — {cap.name} (from {cap.donor}){role}")
        else:
            gap = st["gap"]
            need = str(gap.get("need") or "unspecified")
            fname = f"{n:02d}-fresh-{slugify(need)[:40].strip('-') or 'gap'}.md"
            consumer = None
            cid = _as_int(gap.get("consumer_capability_id"))
            if gap.get("token") and cid is not None:
                ccap = store.get_capability(cid)
                if ccap is not None:
                    consumer = {"name": ccap.name, "what": ccap.what,
                                "step": in_kit.get(cid, (None, None))[1]}
            text = salvage.build_fresh_spec(gap, target_slug=spec_target, project=project,
                                            store=store, policy=policy, consumer=consumer)
            tasks.append(f"- [ ] Step {n}: build fresh — {need}")
            blueprint_steps.append(f"{n}. Step {n} — build fresh: {need}")
        tasks.append(f"  spec: .reincarnate/specs/{fname}")
        (specs_dir / fname).write_text(resolve(text), encoding="utf-8")

    # donors table
    donors: dict[str, dict] = {}
    for st in steps:
        if st["kind"] == "cap":
            d = donors.setdefault(st["cap"].donor, {"license": st["cap"].license or "unknown",
                                                    "caps": []})
            d["caps"].append(st["cap"].name)
    donor_rows = [f"| {_table_cell(k)} | {_table_cell(v['license'])} | "
                  f"{_table_cell(', '.join(v['caps']))} |" for k, v in donors.items()]

    flags = []
    for f in proposal.get("license_flags") or []:
        if isinstance(f, dict):
            level = f.get("level") or f.get("severity") or "warn"
            who = f.get("donor") or f.get("capability") or ""
            lic = f.get("license") or ""
            msg = f.get("message") or f.get("note") or ""
            flags.append(f"- [{level}] " + " ".join(x for x in (who, lic, msg) if x))
        else:
            flags.append(f"- {f}")
    glue = notes.strip() if notes and notes.strip() else (proposal.get("glue") or "").strip()
    risks = [f"- {r}" for r in proposal.get("risks") or []]
    created = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    bp = [f"# {composition.name}", "", f"**Pitch:** {composition.pitch}", "",
          f"**Idea:** {composition.idea}", "", f"**Target:** {spec_target}", "",
          f"**Created:** {created}", "", "## Donors", "",
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
        bp += ["## Problems", "", *[f"- {p}" for p in problems], ""]
    (kit_dir / "BLUEPRINT.md").write_text(resolve("\n".join(bp)), encoding="utf-8")

    tests_line = f"Test command: `{test_cmd}`" if test_cmd else "Test command: (none set)"
    if is_new:
        tests_line += " (set in step 0)."
    cl = [f"# Building {composition.name}", "", "## Stack", "",
          *([f"- {x}" for x in stack] or ["- (follow the existing project conventions)"]), "",
          "## Dependencies", "", *salvage.dependency_lines(policy), "",
          "## Tests", "", tests_line, "",
          "## Clean-room rules", "", *salvage.constraint_lines(policy), "",
          "Work one step at a time from .reincarnate/specs/; stop after each step for review.",
          ""]
    (kit_dir / "CLAUDE.md").write_text("\n".join(cl), encoding="utf-8")
    (kit_dir / "tasks.md").write_text("\n".join(tasks) + "\n", encoding="utf-8")
    run = kit_dir / "run.sh"
    run.write_text(RUN_SH.replace("__TEST_CMD__", _shell_quote(test_cmd)), encoding="utf-8")
    run.chmod(0o755)

    zip_path = None
    if zip_it:
        kdir = paths.ensure(paths.kits_dir())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        zp = kdir / f"{composition.slug}-{stamp}.zip"
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(kit_dir.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(target_dir).as_posix())
        zip_path = str(zp)

    store.add_kit(composition.id, str(kit_dir))
    composition.status = "emitted"
    store.save_composition(composition)
    files = sorted(f.relative_to(kit_dir).as_posix() for f in kit_dir.rglob("*") if f.is_file())
    return {"kit_dir": str(kit_dir), "files": files, "steps": len(steps), "zip": zip_path,
            "target": spec_target}


def run_cli(store, args: Any) -> dict:
    comp = store.get_composition(args.composition)
    if comp is None:
        return {"error": f"composition {args.composition} not found"}
    notes = None
    notes_file = getattr(args, "notes", None)
    if notes_file:
        notes = Path(notes_file).expanduser().read_text(encoding="utf-8")
    try:
        return export_kit(store, comp, args.into, zip_it=bool(getattr(args, "zip", False)),
                          notes=notes)
    except ValueError as e:
        return {"error": str(e)}
