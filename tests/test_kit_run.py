"""Batch I: the kit's run.sh driver (stop on failure, resume, Code Read per step, Bash rules),
the builder Bash allowlist, and security notes winning over insecure acceptance checks."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from reincarnate import kit, salvage
from reincarnate.models import Composition, Profile, Project

SYSTEM_PATH = "/usr/bin:/bin"
NO_CR = "Code Read is not installed"


# ---- builder_bash_allowlist -------------------------------------------------------------------

def test_allowlist_per_ecosystem():
    node = salvage.builder_bash_allowlist(["node"], "npm test")
    assert node[0] == "Bash(npm test:*)"
    assert "Bash(npm run build:*)" in node and "Bash(npx tsc:*)" in node
    assert len(node) == len(set(node)) == 6  # npm test is not listed twice
    py = salvage.builder_bash_allowlist(["python"], "python3 -m pytest -q")
    assert py == ["Bash(python3 -m pytest -q:*)", "Bash(python3 -m pytest:*)",
                  "Bash(.venv/bin/python -m pytest:*)", "Bash(pip install:*)",
                  "Bash(.venv/bin/pip install:*)", "Bash(python3 -m venv:*)",
                  "Bash(uv add:*)", "Bash(uv sync:*)"]
    go = salvage.builder_bash_allowlist(["go"], "go test ./...")
    assert "Bash(go mod tidy:*)" in go and "Bash(go vet:*)" in go
    rust = salvage.builder_bash_allowlist(["rust"], "cargo test")
    assert rust == ["Bash(cargo test:*)", "Bash(cargo build:*)", "Bash(cargo check:*)",
                    "Bash(cargo add:*)"]


def test_allowlist_multi_existing_and_empty():
    multi = salvage.builder_bash_allowlist(["rust", "node"], "make test")
    assert multi[0] == "Bash(make test:*)"
    assert "Bash(npm install:*)" in multi and "Bash(cargo add:*)" in multi
    assert multi.index("Bash(npm test:*)") < multi.index("Bash(cargo test:*)")  # stable order
    assert salvage.builder_bash_allowlist([], "") == []
    assert salvage.builder_bash_allowlist(["go"], "")[0] == "Bash(go test:*)"


def test_allowlist_for_existing_target_comes_from_manifests(store, tmp_path):
    comp, d = _comp(store, tmp_path, manifest="package.json", test_cmd="npm test")
    kit.export_kit(store, comp, "proj")
    run = (d / ".reincarnate" / "run.sh").read_text()
    assert "'Bash(npm test:*)'" in run and "'Bash(npm run build:*)'" in run
    assert "'Bash(pip install:*)'" not in run
    claude = (d / ".reincarnate" / "CLAUDE.md").read_text()
    assert "## Checks" in claude and "`npm run build`" in claude and "`npm test`" in claude


def test_claude_md_checks_section(store, tmp_path):
    comp, d = _comp(store, tmp_path)
    kit.export_kit(store, comp, "proj")
    claude = (d / ".reincarnate" / "CLAUDE.md").read_text()
    assert claude.index("## Tests") < claude.index("## Checks") < claude.index("## Clean-room")
    for s in ["cr check-turn", ".coderead/", ".reincarnate/state.json", ".reincarnate/checks/",
              "tick marks in `tasks.md`", "`true`", "`pip install`"]:
        assert s in claude, s


# ---- security notes vs acceptance checks ------------------------------------------------------

def _spec_for(store, tmp_path, checks, security_notes="Validate input."):
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [{"name": "Fetcher", "what": "Fetches pages.", "files": ["a.py"],
                          "portability": "high", "security_notes": security_notes}]})
    cap = store.list_capabilities()[0]
    return salvage.build_clean_room_spec(store, cap, target_slug="new:App", acceptance=checks)


def test_insecure_checks_are_dropped_and_listed(store, tmp_path):
    insecure = ["TLS verification disabled for self-signed hosts",
                "Requests are sent with verify=False",
                "The client sets rejectUnauthorized: false",
                "Uses InsecureSkipVerify in the transport",
                "Skips certificate validation on retry",
                "Reads a hard-coded password from the source",
                "Sets NODE_TLS_REJECT_UNAUTHORIZED=0"]
    normal = ["Returns the page body as text", "Rejects connections when TLS verification "
              "is disabled", "Never uses hard-coded passwords", "Retries three times"]
    spec = _spec_for(store, tmp_path, insecure + normal)
    assert "**Security requirements (do not reproduce the donor's insecure behavior):**" in spec
    assert ("If an acceptance check conflicts with a security requirement, the security "
            "requirement wins.") in spec
    for c in insecure:
        assert f"- Not reproduced: {c}" in spec, c
        assert f"- {c}\n" not in spec.split("**Acceptance checks:**")[1], c
    acc = spec.split("**Acceptance checks:**")[1]
    for c in normal:
        assert f"- {c}" in acc, c
        assert f"Not reproduced: {c}" not in spec


def test_no_security_notes_no_section_and_checks_kept(store, tmp_path):
    spec = _spec_for(store, tmp_path, ["TLS verification disabled"], security_notes="")
    assert "Security requirements" not in spec and "Not reproduced" not in spec
    assert "- TLS verification disabled" in spec


# ---- run.sh end to end ------------------------------------------------------------------------

def _comp(store, tmp_path, *, manifest="pyproject.toml", test_cmd="true"):
    d = tmp_path / "proj"
    d.mkdir(exist_ok=True)
    (d / manifest).write_text("{}\n")
    store.save_profile(Profile(name="dan", stack=["python"]))
    store.add_project(Project(slug="proj", path=str(d), stack=["python"], test_cmd=test_cmd))
    store.upsert_dossier("own", "repo", "https://github.com/own/repo", "abc", {
        "summary": "A tool.", "dependencies": {"license": "MIT"},
        "capabilities": [{"name": "Parser", "what": "Parses.", "files": ["a.py"],
                          "portability": "high"},
                         {"name": "Cache", "what": "Caches.", "files": ["b.py"],
                          "portability": "high"}]})
    caps = store.list_capabilities()
    comp = store.save_composition(Composition(
        slug="app", name="App", target_kind="project", target_slug="proj",
        proposal={"capabilities": [{"capability_id": c.id, "order": i}
                                   for i, c in enumerate(caps, 1)],
                  "gaps": [{"need": "Auth layer", "build_fresh": True}]}))
    return comp, d


FAKE_CLAUDE = """#!/bin/bash
echo "$*" >> "$FAKE_LOG/claude.log"
n=$(echo "$2" | sed -n 's/.*building step \\([0-9]*\\) of.*/\\1/p')
echo built > "built-$n.txt"
[ "${FAKE_CLAUDE_FAIL_STEP:-}" = "$n" ] && exit 3
exit 0
"""
FAKE_CR = """#!/bin/bash
echo "$*" >> "$FAKE_LOG/cr.log"
echo "cr says hello"
exit "${FAKE_CR_RC:-0}"
"""


class Kit:
    def __init__(self, store, tmp_path, *, test_cmd="true", with_cr=True):
        if shutil.which("git", path=SYSTEM_PATH) is None:
            pytest.skip("git not installed")
        comp, self.dir = _comp(store, tmp_path, test_cmd=test_cmd)
        kit.export_kit(store, comp, "proj")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        self.log = tmp_path / "log"
        self.log.mkdir()
        self.write_bin("claude", FAKE_CLAUDE)
        if with_cr:
            self.write_bin("cr", FAKE_CR)
        self.env = {"PATH": f"{self.bin}:{SYSTEM_PATH}", "HOME": str(tmp_path),
                    "FAKE_LOG": str(self.log)}

    def write_bin(self, name, text):
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def run(self, *args, **env):
        e = {**self.env, **env}
        return subprocess.run(["/bin/bash", ".reincarnate/run.sh", *args], cwd=self.dir, env=e,
                              capture_output=True, text=True, timeout=60)

    def calls(self, name):
        f = self.log / f"{name}.log"
        return f.read_text().splitlines() if f.exists() else []

    def tasks(self):
        return (self.dir / ".reincarnate" / "tasks.md").read_text()

    def ticked(self):
        return [int(m) for m in re.findall(r"^- \[x\] Step (\d+):", self.tasks(), re.M)]


@pytest.fixture
def kt(store, tmp_path):
    return Kit(store, tmp_path)


def git_commits(d):
    r = subprocess.run(["git", "rev-list", "--all"], cwd=d, capture_output=True, text=True)
    return r.stdout.split()


def test_all_pass_with_code_read(kt):
    r = kt.run("--yes")
    assert r.returncode == 0, r.stderr
    assert kt.ticked() == [1, 2, 3] and "[ ]" not in kt.tasks()
    assert "unverified" not in kt.tasks()
    assert "Done: built steps 1-3 (3 verified by Code Read, 0 unverified)." in r.stdout
    assert "All steps done" not in r.stdout
    crs = kt.calls("cr")
    assert len(crs) == 3
    for line in crs:
        m = re.fullmatch(r"check-turn --repo \. --since ([0-9a-f]{40}) --format text", line)
        assert m, line
    assert len({re.search(r"[0-9a-f]{40}", c).group(0) for c in crs}) == 3  # new baseline each
    assert "cr says hello" in (kt.dir / ".reincarnate" / "checks" / "step-02.txt").read_text()
    assert '"step": 3' in (kt.dir / ".reincarnate" / "state.json").read_text()
    assert git_commits(kt.dir) == []  # the baseline is a tree, never a commit
    assert (kt.dir / ".git").is_dir()


def test_baseline_tree_holds_the_prior_step(kt):
    kt.run("--yes")
    base3 = re.search(r"[0-9a-f]{40}", kt.calls("cr")[2]).group(0)
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", base3], cwd=kt.dir,
                           capture_output=True, text=True).stdout.split()
    assert "built-1.txt" in files and "built-2.txt" in files and "built-3.txt" not in files


def test_no_code_read_marks_steps_unverified(store, tmp_path):
    k = Kit(store, tmp_path, with_cr=False)
    r = k.run("--yes")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count(NO_CR) == 1 and "WARNING" in r.stdout
    assert k.ticked() == [1, 2, 3]
    assert k.tasks().count("(unverified: Code Read not installed)") == 3
    assert "Done: built steps 1-3 (0 verified by Code Read, 3 unverified)." in r.stdout
    assert r.stdout.count("https://github.com/dhouchin1/coderead") == 1
    assert "REINCARNATE_CR=" in r.stdout


def test_reincarnate_cr_command_with_spaces(kt):
    kt.write_bin("fakenode", FAKE_CR)
    (kt.bin / "cr").unlink()
    r = kt.run("--yes", REINCARNATE_CR="fakenode some/cli.js")
    assert r.returncode == 0, r.stderr
    # the fake logs all its arguments: the script path comes first, then the subcommand
    assert kt.calls("cr")[0].startswith("some/cli.js check-turn --repo . --since ")
    assert "3 verified" in r.stdout


def test_code_read_findings_stop_the_run(kt):
    r = kt.run("--yes", FAKE_CR_RC="1")
    assert r.returncode == 1
    assert "Step 1: Code Read found problems (see .reincarnate/checks/step-01.txt)" in r.stderr
    assert "rerun: .reincarnate/run.sh --from 1" in r.stderr
    assert kt.ticked() == [] and len(kt.calls("claude")) == 1
    assert "Done:" not in r.stdout and "All steps done" not in r.stdout


def test_code_read_cannot_run(kt):
    r = kt.run("--yes", FAKE_CR_RC="2")
    assert r.returncode == 1
    assert "Code Read could not run (exit 2)" in r.stderr and kt.ticked() == []


def test_failing_tests_stop_the_run(store, tmp_path):
    k = Kit(store, tmp_path, test_cmd="false")
    r = k.run("--yes")
    assert r.returncode == 1
    assert "Step 1: the tests failed." in r.stderr and k.ticked() == []
    assert k.calls("cr") == [] and len(k.calls("claude")) == 1


def test_builder_failure_stops_the_run(kt):
    r = kt.run("--yes", FAKE_CLAUDE_FAIL_STEP="2")
    assert r.returncode == 1
    assert "Step 2: the builder failed (exit 3)." in r.stderr
    assert "rerun: .reincarnate/run.sh --from 2" in r.stderr
    assert kt.ticked() == [1] and len(kt.calls("cr")) == 1


def test_resume_and_from(kt):
    assert kt.run("--yes", FAKE_CLAUDE_FAIL_STEP="2").returncode == 1
    r = kt.run("--yes")  # resumes at the first unticked step
    assert r.returncode == 0, r.stderr
    steps = [re.search(r"building step (\d+) of", c).group(1) for c in kt.calls("claude")]
    assert steps == ["1", "2", "2", "3"]  # step 1 once, step 2 failed then resumed
    assert kt.ticked() == [1, 2, 3]
    assert "Done: built steps 2-3 (2 verified" in r.stdout
    again = kt.run("--yes")
    assert again.returncode == 0 and "All steps are already done." in again.stdout
    n = len(kt.calls("claude"))
    r = kt.run("--yes", "--from", "3")
    assert r.returncode == 0 and "Done: built step 3 " in r.stdout
    assert len(kt.calls("claude")) == n + 1 and kt.ticked() == [1, 2, 3]


def test_rerun_of_unverified_step_with_code_read_drops_the_mark(store, tmp_path):
    k = Kit(store, tmp_path, with_cr=False)
    assert k.run("--yes").returncode == 0
    k.write_bin("cr", FAKE_CR)
    assert k.run("--yes", "--from", "2").returncode == 0
    lines = {int(m.group(1)): m.group(0) for m in
             re.finditer(r"^- \[x\] Step (\d+):.*$", k.tasks(), re.M)}
    assert "unverified" in lines[1]
    assert "unverified" not in lines[2] and "unverified" not in lines[3]


def test_bad_arguments(kt):
    for args in (["--bogus"], ["--from"], ["--from", "x"], ["extra"]):
        r = kt.run(*args)
        assert r.returncode == 2 and "Usage: .reincarnate/run.sh [--yes] [--from N]" in r.stderr
    assert kt.calls("claude") == []


def test_claude_missing_is_an_error(kt):
    (kt.bin / "claude").unlink()
    r = kt.run("--yes")
    assert r.returncode == 1 and "claude is not on PATH" in r.stderr


def test_permission_modes(kt):
    kt.run("--yes")
    line = kt.calls("claude")[0]
    assert "--permission-mode acceptEdits" in line and "--allowedTools" in line
    assert "Bash(true:*)" in line and "Bash(pip install:*)" in line
    assert "bypassPermissions" not in line
    assert "--max-turns 60" in line and "--model sonnet" in line
    (kt.log / "claude.log").unlink()
    r = kt.run("--yes", "--from", "1", REINCARNATE_SANDBOX="1")
    assert "sandbox detected" in r.stdout
    line = kt.calls("claude")[0]
    assert "--permission-mode bypassPermissions" in line and "--allowedTools" not in line
    assert "acceptEdits" not in line


def test_prompt_mentions_checks(kt):
    kt.run("--yes")
    assert "checked afterwards by the tests and by Code Read" in kt.calls("claude")[0]


def test_run_sh_is_valid_bash(kt):
    r = subprocess.run(["/bin/bash", "-n", str(kt.dir / ".reincarnate" / "run.sh")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_new_target_inside_another_repo_gets_its_own(store, tmp_path):
    if shutil.which("git", path=SYSTEM_PATH) is None:
        pytest.skip("git not installed")
    outer = tmp_path / "outer"
    inner = outer / "app"
    inner.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True)
    comp, _ = _comp(store, tmp_path)
    kit.export_kit(store, comp, str(inner))  # no manifest: a new target
    assert "NEW_TARGET=1" in (inner / ".reincarnate" / "run.sh").read_text()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(FAKE_CLAUDE)
    (bin_dir / "claude").chmod(0o755)
    log = tmp_path / "log"
    log.mkdir()
    env = {"PATH": f"{bin_dir}:{SYSTEM_PATH}", "HOME": str(tmp_path), "FAKE_LOG": str(log),
           "FAKE_CLAUDE_FAIL_STEP": "0"}
    r = subprocess.run(["/bin/bash", ".reincarnate/run.sh", "--yes"], cwd=inner, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1 and "Step 0: the builder failed" in r.stderr
    assert (inner / ".git").is_dir()
    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=inner,
                         capture_output=True, text=True).stdout.strip()
    assert Path(top).resolve() == inner.resolve()


def test_rerun_after_a_stop_keeps_the_stopped_steps_baseline(kt):
    r = kt.run("--yes", FAKE_CR_RC="1")
    assert r.returncode == 1
    first = re.search(r"[0-9a-f]{40}", kt.calls("cr")[0]).group(0)
    assert '"status": "open"' in (kt.dir / ".reincarnate" / "state.json").read_text()
    r = kt.run("--yes")  # nothing fixed: the diff must still hold the stopped attempt's files
    assert r.returncode == 0, r.stderr
    assert "keeping its baseline" in r.stdout
    assert re.search(r"[0-9a-f]{40}", kt.calls("cr")[1]).group(0) == first
    assert '"status": "passed"' in (kt.dir / ".reincarnate" / "state.json").read_text()
    r = kt.run("--yes", "--from", "3")  # a passed step re-run on purpose gets a fresh baseline
    assert "keeping its baseline" not in r.stdout


# -- /reincarnate:build: --begin N and --check N ------------------------------------------
def test_begin_then_check_ticks_one_step_without_claude(kt):
    kt.write_bin("claude", "#!/bin/bash\nexit 99\n")  # must never be called in these modes
    r = kt.run("--begin", "1")
    assert r.returncode == 0, r.stderr
    assert "Step 1 begun" in r.stdout and ".reincarnate/specs/01-" in r.stdout
    state = (kt.dir / ".reincarnate" / "state.json").read_text()
    assert '"step": 1' in state and '"status": "open"' in state
    (kt.dir / "built-1.txt").write_text("the session built this\n")
    r = kt.run("--check", "1")
    assert r.returncode == 0, r.stderr
    assert "Step 1 done (verified by Code Read)." in r.stdout
    assert kt.ticked() == [1] and kt.calls("claude") == []
    base = (kt.dir / ".reincarnate" / "state.json").read_text()
    assert '"status": "passed"' in base
    since = kt.calls("cr")[0].split("--since ")[1].split()[0]
    assert re.search(r"\b" + since + r"\b", state)  # checked against the --begin baseline
    assert git_commits(kt.dir) == []


def test_check_failures_keep_the_step_open_and_its_baseline(kt):
    kt.run("--begin", "2")
    first = (kt.dir / ".reincarnate" / "state.json").read_text()
    r = kt.run("--check", "2", FAKE_CR_RC="1")
    assert r.returncode == 1 and "Code Read found problems" in r.stderr
    assert "run: .reincarnate/run.sh --check 2" in r.stderr
    assert kt.ticked() == []
    assert (kt.dir / ".reincarnate" / "state.json").read_text() == first
    r = kt.run("--check", "2")
    assert r.returncode == 0 and kt.ticked() == [2]


def test_begin_and_check_refusals(store, tmp_path):
    k = Kit(store, tmp_path, with_cr=False)
    assert k.run("--check", "1").returncode == 1  # not begun
    assert k.run("--begin", "9").returncode == 2  # no such step
    assert k.run("--begin", "1", "--yes").returncode == 2  # modes do not mix
    assert k.run("--begin").returncode == 2
    assert k.run("--begin", "1").returncode == 0
    r = k.run("--check", "1")
    assert r.returncode == 0 and "unverified: Code Read not installed" in r.stdout
    assert "(unverified: Code Read not installed)" in k.tasks()
    r = k.run("--begin", "1")
    assert r.returncode == 1 and "already ticked" in r.stderr


def test_tests_failing_in_check_mode(store, tmp_path):
    k = Kit(store, tmp_path, test_cmd="false")
    k.run("--begin", "1")
    r = k.run("--check", "1")
    assert r.returncode == 1 and "the tests failed" in r.stderr and k.ticked() == []


def test_python_tests_run_in_the_project_venv(store, tmp_path):
    k = Kit(store, tmp_path, test_cmd="python3 -m pytest -q", with_cr=False)
    venv = k.dir / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text('#!/bin/bash\necho "venv python $*" > venv-ran.txt\n')
    (venv / "python").chmod(0o755)
    k.run("--begin", "1")
    r = k.run("--check", "1")
    assert r.returncode == 0, r.stderr
    assert "running tests: .venv/bin/python -m pytest -q" in r.stdout
    assert (k.dir / "venv-ran.txt").read_text() == "venv python -m pytest -q\n"
