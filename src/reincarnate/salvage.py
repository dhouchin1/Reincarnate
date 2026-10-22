"""Clean-room spec builder: the air-gap artifact.

Specs describe behavior only. They never contain donor file contents; donor file *paths*
appear solely in a "context only — DO NOT COPY" line.

A *policy* is the target's build context shared by every spec and the kit's CLAUDE.md:
``{"kind": "new" | "existing", "stack": [...], "test_cmd": str}`` (``donor`` optional).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

NEW_PREFIX = "new:"
MANIFESTS = ("pyproject.toml", "package.json", "go.mod", "Cargo.toml", "Gemfile", "pom.xml",
             "build.gradle")
NO_CHECKS = ("(none recorded: write a test for every behavior above and list them in your "
             "step notes)")
NEW_WORK_CHECK = "The behavior above is implemented and covered by tests"
NO_DEFINITION = "no canonical definition; infer from the consumer"

# Ecosystem -> stack keywords (whole-word, case-insensitive) and its single-suite command.
ECOSYSTEMS: dict[str, tuple[str, ...]] = {
    "node": ("typescript", "javascript", "node", "next.js", "nextjs", "react", "vue", "svelte",
             "express", "trpc", "vite", "deno", "bun"),
    "python": ("python", "fastapi", "django", "flask", "pytest"),
    "go": ("go", "golang"),
    "rust": ("rust", "cargo"),
}
TEST_COMMANDS = {"node": "npm test", "python": "python -m pytest -q", "go": "go test ./...",
                 "rust": "cargo test"}
MULTI_TEST_CMD = "make test"
_ECO_MANIFEST = {"node": "`package.json`", "python": "`pyproject.toml`", "go": "`go.mod`",
                 "rust": "`Cargo.toml`"}
_MANIFEST_ECO = {"package.json": "node", "pyproject.toml": "python", "go.mod": "go",
                 "Cargo.toml": "rust"}


# `c17` capability references in free text; `c2/c177` lists resolve item by item, while path
# segments such as `src/c12.py` or `lib/c3/x` are left alone.
CID_REF = re.compile(r"(?<![\w/.-])c\d+(?:/c\d+)*(?![\w/-]|\.\w)")


def resolve_cids(text: str, lookup) -> str:
    """Replace each `c<id>` with `lookup(id)`; a None result keeps the reference as is."""
    def sub(m: re.Match) -> str:
        out = []
        for ref in m.group(0).split("/"):
            got = lookup(int(ref[1:]))
            out.append(got if got else ref)
        return "/".join(out)

    return CID_REF.sub(sub, text or "")


# Donor file references in dossier prose (`src/app.ts:41`, kanbanView.ts:55-84, AppPing.tsx).
# Only the "Donor reference files" line may name paths; prose gets DONOR_FILE instead.
DONOR_FILE = "a donor source file"
_CODE_EXT = ("py|pyi|ts|tsx|mts|cts|js|jsx|mjs|cjs|go|rs|rb|java|kt|kts|swift|php|c|h|cc|cpp|"
             "hpp|cs|vue|svelte|sh|bash|sql|json|ya?ml|toml|ini|cfg|md|css|scss|less|html?|lua|"
             "ex|exs|erl|scala|dart|zig|nim|clj|r|pl|pm|proto|graphql|gql|lock|xml|gradle")
# Prose words like "Next.js" or "e.g." share these endings, so a bare name with one of them only
# counts as a path when it has a directory or a :line suffix.
_AMBIGUOUS_EXT = {"js", "json", "md", "c", "h", "r", "sh", "html", "htm", "css", "xml", "lock",
                  "ini", "cfg", "pl"}
_LINES = r"(?::\d+(?:\s*[-\u2013]\s*\d+)?)?"
_BACKTICK_FILE = re.compile(rf"`[^`\n]*?[\w-]\.(?:{_CODE_EXT}){_LINES}`", re.IGNORECASE)
_BARE_FILE = re.compile(
    rf"(?<![\w/.@-])(?P<path>(?:[\w.-]+/)*)(?P<stem>[\w-]*[A-Za-z_][\w.-]*)"
    rf"\.(?P<ext>{_CODE_EXT})(?P<lines>{_LINES})(?![\w/-])", re.IGNORECASE)


def scrub_paths(text: str) -> str:
    """Replace donor file references in prose with "a donor source file"."""
    text = _BACKTICK_FILE.sub(DONOR_FILE, text or "")

    def bare(m: re.Match) -> str:
        if (m.group("ext").lower() in _AMBIGUOUS_EXT and not m.group("path")
                and not m.group("lines")):
            return m.group(0)
        return DONOR_FILE

    return _BARE_FILE.sub(bare, text)


# -- target policy ------------------------------------------------------------------------
def split_stack(text: str) -> list[str]:
    """Free-text stack ("TypeScript + Next.js, SQLite") -> items."""
    return [s.strip() for s in re.split(r"[+,]", str(text or "")) if s.strip()]


def ecosystems(stack: list[str]) -> list[str]:
    """Ecosystems named by the stack's keywords, in ECOSYSTEMS order."""
    text = " ".join(str(s) for s in stack or []).lower()
    out = []
    for eco, words in ECOSYSTEMS.items():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", text) for w in words):
            out.append(eco)
    return out


def derive_test_cmd(ecos: list[str]) -> str:
    """One ecosystem -> its runner; several or none -> `make test` (step 0 writes it)."""
    return TEST_COMMANDS[ecos[0]] if len(ecos) == 1 else MULTI_TEST_CMD


def has_manifest(d: Path | str | None) -> bool:
    if not d:
        return False
    p = Path(d).expanduser()
    return p.is_dir() and any((p / m).is_file() for m in MANIFESTS)


def manifest_command(d: Path | str | None) -> str:
    """Test command guessed from the manifests in an existing directory ("" if none)."""
    if not d:
        return ""
    p = Path(d).expanduser()
    ecos = [e for m, e in _MANIFEST_ECO.items() if (p / m).is_file()]
    return derive_test_cmd(ecos) if ecos else ""


def new_policy(stack: list[str]) -> dict:
    stack = [str(s) for s in stack or [] if str(s).strip()]
    return {"kind": "new", "stack": stack, "test_cmd": derive_test_cmd(ecosystems(stack))}


def target_policy(store, target_slug: str, project=None) -> dict:
    """Policy for a single spec: existing when the project is registered, else new (profile
    stack)."""
    if project is None and store is not None and not str(target_slug).startswith(NEW_PREFIX):
        project = store.get_project(target_slug)
    if project is not None and not str(target_slug).startswith(NEW_PREFIX):
        return {"kind": "existing", "stack": list(getattr(project, "stack", None) or []),
                "test_cmd": getattr(project, "test_cmd", "") or ""}
    prof = store.get_profile() if store is not None else None
    return new_policy(list(prof.stack) if prof else [])


def policy_text(policy: dict | None) -> str:
    allowed = ("the dependencies this project already uses"
               if (policy or {}).get("kind") == "existing" else "the stack below")
    return (f"Allowed dependencies: {allowed}. Any other dependency must be small and necessary; "
            "before adding it, record its name, version range and reason in `DEPENDENCIES.md`. "
            "Prefer the standard library.")


def dependency_lines(policy: dict | None) -> list[str]:
    """The dependency bullet; new targets list their stack right below it."""
    out = [f"- {policy_text(policy)}"]
    stack = (policy or {}).get("stack") or []
    if (policy or {}).get("kind") != "existing" and stack:
        out.append(f"  - Stack: {', '.join(stack)}")
    return out


def constraint_lines(policy: dict | None) -> list[str]:
    """The clean-room bullets (shared with the kit's CLAUDE.md)."""
    policy = policy or {}
    donor = policy.get("donor")
    test_cmd = policy.get("test_cmd") or ""
    who = f"`{donor}`" if donor else "any donor repository"
    conventions = ("- Match this project's existing conventions, structure, and code style."
                   if policy.get("kind") == "existing" else
                   "- Follow the layout and conventions set up by step 0 (scaffold) and the "
                   "steps before this one.")
    return [
        f"- Do **not** open, copy, paste, or transliterate source from {who}. "
        "Reimplement from the behavioral description only.",
        *dependency_lines(policy),
        conventions,
        "- Include unit tests" + (f"; the suite runs with `{test_cmd}`." if test_cmd else "."),
    ]


def acceptance_lines(checks: list[str] | None) -> list[str]:
    checks = [str(c).strip() for c in checks or [] if str(c).strip()]
    body = [f"- {c}" for c in checks] if checks else [NO_CHECKS]
    return ["**Acceptance checks:**", "", *body]


def _target_name(target_slug: str) -> str:
    if str(target_slug).startswith(NEW_PREFIX):
        return target_slug[len(NEW_PREFIX):].strip() or "new project"
    return str(target_slug)


def _stack_text(policy: dict) -> str:
    stack = policy.get("stack") or []
    if stack:
        return ", ".join(stack)
    return "the project's existing stack" if policy.get("kind") == "existing" else \
        "the new project's stack"


# -- specs --------------------------------------------------------------------------------
def _integration_intent(dossier, capability_name: str, target: str) -> str:
    name_l = capability_name.lower()
    for row in (dossier.data.get("fit_to_projects") or []) if dossier else []:
        if not isinstance(row, dict):
            continue
        cap = str(row.get("capability") or "").lower()
        if row.get("project") == target and cap and (name_l in cap or cap in name_l):
            return str(row.get("how") or "")
    return ""


def build_clean_room_spec(store, capability, *, target_slug: str, project=None,
                          include_header: bool = True, policy: dict | None = None,
                          acceptance: list[str] | None = None) -> str:
    """`acceptance`: ready check strings (stored checks + requirement coverage); none ->
    the fallback line."""
    dossier = store.get_dossier(capability.dossier_id)
    name = capability.name or "capability"
    what = scrub_paths((capability.what or "").strip())
    pnotes = scrub_paths((capability.portability_notes or "").strip())
    snotes = scrub_paths((capability.security_notes or "").strip())
    files = [str(f) for f in (capability.files or []) if str(f).strip()]
    donor = capability.donor or (dossier.donor if dossier else "unknown")
    license_str = capability.license or (dossier.license if dossier else "") or "unknown"
    date = ((dossier.comprehended_at if dossier else "") or "")[:10] or "unknown date"
    if policy is None:
        policy = target_policy(store, target_slug, project)
    intent = scrub_paths(_integration_intent(dossier, name, target_slug))

    sec_reqs: list[str] = []
    if snotes:
        sec_reqs.append(snotes)
    token = (name.split() or [""])[0].lower()
    security = (dossier.data.get("security") if dossier else None) or {}
    for concern in security.get("concerns") or []:
        c = scrub_paths(str(concern).strip())
        if c and token and token in c.lower() and c not in sec_reqs:
            sec_reqs.append(c)

    lines: list[str] = []
    if include_header:
        lines += [f"## Clean-room reimplementation: {name}", ""]
    lines += [
        f"**Goal:** Reimplement the *behavior* described below natively in "
        f"`{_target_name(target_slug)}` ({_stack_text(policy)}). Work only from this spec.",
        "",
        f"**Provenance:** idea observed in donor repo `{donor}` (license: {license_str}); "
        f"comprehended {date}.",
        "",
        "**Constraints — the clean-room air gap:**",
        *constraint_lines({**policy, "donor": donor}),
        "",
        "**Behavior to implement:**",
        "",
        what or "_(no description recorded)_",
    ]
    if pnotes:
        lines += ["", "**Design notes (how to build it cleanly):**", "", pnotes]
    if sec_reqs:
        lines += ["", "**Security requirements:**", ""]
        lines += [f"- {s}" for s in sec_reqs]
    if intent:
        lines += ["", f"**Integration intent (from the dossier):** {intent}"]
    lines += ["", *acceptance_lines([scrub_paths(str(c)) for c in acceptance or []])]
    if files:
        lines += ["", "**Donor reference files (context only — DO NOT COPY):** "
                  + ", ".join(f"`{f}`" for f in files)]
    return "\n".join(lines) + "\n"


def _consumer_label(consumer: dict | None, gap: dict) -> str:
    if consumer and consumer.get("name"):
        step = consumer.get("step")
        return f"{consumer['name']} (step {step})" if step is not None else consumer["name"]
    need = str(gap.get("need") or "")
    token = str(gap.get("token") or "")
    if token and need.startswith(f"{token} for "):
        return need[len(token) + 5:].strip() or "its consumer"
    return "its consumer"


def build_fresh_spec(gap: dict, *, target_slug: str, project=None, store=None,
                     policy: dict | None = None, consumer: dict | None = None) -> str:
    """A gap with no donor. Wiring gaps (with `token`) name the token, its vocabulary
    definition and the consumer `{name, step, what}` that needs it."""
    need = str(gap.get("need") or "").strip() or "(unspecified need)"
    if policy is None:
        policy = target_policy(store, target_slug, project)
    token = str(gap.get("token") or "").strip()
    if token:
        who = _consumer_label(consumer, gap)
        definition = str(gap.get("definition") or "").strip()
        if not definition and store is not None:
            definition = str(store.get_vocabulary().tokens.get(token, {}).get("definition")
                             or "").strip()
        cname = (consumer or {}).get("name") or who
        title = f"## Build fresh: `{token}` for {cname}"
        behavior = [f"`{token}`: {definition or NO_DEFINITION}", ""]
        what = scrub_paths(str((consumer or {}).get("what") or "").strip())
        behavior.append(f"Consumed by {who}" + (f": {what}" if what else "."))
        checks = [f"Provides `{token}` as defined above",
                  f"{who} can use this step's `{token}` without a stub of its own"]
    else:
        title = f"## Build fresh: {need}"
        behavior = [need]
        checks = [NEW_WORK_CHECK]
    lines = [
        title,
        "",
        f"**Goal:** Build the behavior below natively in `{_target_name(target_slug)}` "
        f"({_stack_text(policy)}). There is no donor - build fresh.",
        "",
        "**Constraints — the clean-room air gap:**",
        *constraint_lines(policy),
        "",
        "**Behavior to implement:**",
        "",
        *behavior,
        "",
        *acceptance_lines(checks),
    ]
    cited = [c for c in gap.get("candidates") or [] if isinstance(c, dict) and c.get("name")]
    if cited:
        lines += ["", "**Catalog parts that also provide this (weak matches, not used):**", ""]
        lines += [f"- {c['name']} ({c.get('donor') or 'unknown donor'}, relevance "
                  f"{float(c.get('relevance') or 0):.2f})" for c in cited]
    donors = [str(d) for d in (gap.get("suggested_donors") or []) if str(d).strip()]
    if donors:
        lines += ["", "**Suggested donors (comprehend later, do not read now):** "
                  + ", ".join(f"`{d}`" for d in donors)]
    return "\n".join(lines) + "\n"


def build_scaffold_spec(stack: list[str], test_cmd: str, ecosystems: list[str], *,
                        target_name: str = "the new project") -> str:
    """Step 0 for a new target: manifests, skeleton, test runner, DEPENDENCIES.md."""
    stack = [str(s) for s in stack or [] if str(s).strip()]
    policy = {"kind": "new", "stack": stack, "test_cmd": test_cmd}
    ecos = [e for e in ecosystems or [] if e in TEST_COMMANDS]
    if ecos:
        manifests = ", ".join(f"{_ECO_MANIFEST[e]} ({e})" for e in ecos)
        manifest_line = (f"- The manifest for each ecosystem: {manifests}, declaring only "
                         "allowed dependencies.")
    else:
        manifest_line = ("- The manifest the stack calls for, declaring only allowed "
                         "dependencies.")
    if test_cmd == MULTI_TEST_CMD:
        suites = "; ".join(f"{e}: `{TEST_COMMANDS[e]}`" for e in ecos) or "every suite"
        runner = (f"- A `Makefile` whose `test` target runs every suite ({suites}), so "
                  f"`{test_cmd}` runs them all.")
    else:
        runner = f"- The test runner wired so `{test_cmd}` runs every suite."
    per_eco = f" ({', '.join(ecos)})" if ecos else ""
    lines = [
        f"## Scaffold: {target_name}",
        "",
        f"**Goal:** Create the empty project skeleton ({', '.join(stack) or 'stack to be chosen'})"
        " so every later step only adds feature code.",
        "",
        "**Constraints — the clean-room air gap:**",
        *constraint_lines(policy),
        "",
        "**Behavior to implement:**",
        "",
        manifest_line,
        "- A minimal skeleton: entry points only, no feature code.",
        runner,
        f"- One passing smoke test per ecosystem{per_eco}.",
        "- A README with a \"Run the tests\" section.",
        "- `DEPENDENCIES.md` listing every declared dependency with its reason (\"in the "
        "stack\" for stack items).",
        "",
        *acceptance_lines([
            f"`{test_cmd}` exits 0 with at least one test per ecosystem{per_eco}",
            "`DEPENDENCIES.md` covers every declared dependency",
            "No feature code from later steps",
        ]),
    ]
    return "\n".join(lines) + "\n"


def run_cli(store, args: Any) -> dict:
    cap = store.get_capability(args.capability_id)
    if cap is None:
        return {"error": f"capability {args.capability_id} not found"}
    target = args.into
    checks = list(getattr(cap, "acceptance", None) or [])
    spec = build_clean_room_spec(store, cap, target_slug=target, acceptance=checks)
    path = None
    out = getattr(args, "out", None)
    if out:
        p = Path(out).expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(spec, encoding="utf-8")
        path = str(p)
    return {"capability_id": cap.id, "name": cap.name, "donor": cap.donor, "target": target,
            "spec": spec, "path": path}
