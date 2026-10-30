"""Thin wrapper around headless `claude -p`. Never raises on a non-zero exit code.

The prompt goes to the child on stdin, never in argv: the caller's own stdin cannot leak into
the call, and prompt size is not limited by the argument-length limit."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_DISALLOWED = "Bash,Edit,Write,MultiEdit,NotebookEdit,WebFetch,WebSearch,Task"


@dataclass
class RunResult:
    text: str = ""
    cost_usd: float = 0.0
    rc: int = 0
    stderr: str = ""
    raw: dict = field(default_factory=dict)
    is_error: bool = False
    subtype: str = ""
    errors: list[str] = field(default_factory=list)
    terminal_reason: str = ""
    api_error_status: int | None = None

    @property
    def failed(self) -> bool:
        """True when claude reported an error or exited nonzero (never keyed on `subtype`:
        claude 2.1.301 reports a bad model as is_error with subtype "success")."""
        return self.is_error or self.rc != 0

    def error_message(self) -> str:
        """The best one-line cause of a failure: `result` text when is_error, else `errors`,
        else the first stderr line, else what the return code means."""
        if self.is_error and self.text.strip():
            msg = self.text.strip()
        elif self.errors:
            msg = "; ".join(self.errors)
        elif self.stderr.strip():
            msg = self.stderr.strip().splitlines()[0]
        elif self.rc == 127:
            msg = "claude not found (install Claude Code or set REINCARNATE_CLAUDE_BIN)"
        elif self.rc == 124:
            msg = "claude timed out"
        elif self.rc:
            msg = f"claude exited with code {self.rc}"
        elif self.is_error:
            why = self.terminal_reason or self.subtype or "no detail"
            msg = f"claude reported an error ({why})"
        else:
            return ""
        msg = " ".join(msg.split())[:500]
        if self.api_error_status and str(self.api_error_status) not in msg:
            msg += f" (api status {self.api_error_status})"
        return msg


# Run every model call in a clean room of its own. Without these, `claude -p` loads the
# caller's user CLAUDE.md, memory, every MCP server's tool schemas and the skills list
# (measured: ~243k cached tokens per call on a loaded workstation vs ~3k isolated), and,
# worse, the *donor repo's* CLAUDE.md and .claude/settings.json because comprehension runs
# with the clone as cwd. `--setting-sources ""` loads no settings or CLAUDE.md from any
# scope; the strict empty MCP config drops tool schemas; slash commands/skills are off.
ISOLATION_FLAGS = (
    "--setting-sources", "",
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--no-session-persistence",
)


def claude_argv(
    *,
    model: str,
    max_turns: int,
    tools: str = "Read,Grep,Glob",
    disallowed: str = DEFAULT_DISALLOWED,
    isolated: bool = True,
) -> list[str]:
    """Build the `claude -p` argv (the prompt is sent on stdin, not here).
    `$REINCARNATE_CLAUDE_BIN` overrides the binary.

    `isolated` (default) adds ISOLATION_FLAGS; turn it off only to debug the harness."""
    binary = os.environ.get("REINCARNATE_CLAUDE_BIN") or shutil.which("claude") or "claude"
    return [
        binary,
        *(ISOLATION_FLAGS if isolated else ()),
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--tools",
        tools,
        "--disallowedTools",
        disallowed,
    ]


def _invoke(argv: list[str], cwd: Path | None, env: dict[str, str], timeout: int,
            stdin: bytes):
    """Run the subprocess with `stdin` as its whole input. Separate so tests can stub it.
    Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
        input=stdin,
        capture_output=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _cost(obj: dict) -> float:
    try:
        return float(obj.get("total_cost_usd") or obj.get("cost_usd") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_output(stdout: bytes) -> tuple[str, float, dict]:
    """`--output-format json` stdout -> (text, cost_usd, raw).

    Accepts a single result object, an array of events (last type=="result" wins),
    or bare text (returned as-is with cost 0).
    """
    text = (stdout or b"").decode(errors="replace").strip()
    if not text:
        return "", 0.0, {}
    try:
        val = json.loads(text)
    except ValueError:
        return text, 0.0, {}
    obj: dict | None = None
    if isinstance(val, dict):
        obj = val
    elif isinstance(val, list):
        for e in reversed(val):
            if isinstance(e, dict) and e.get("type") == "result":
                obj = e
                break
    if obj is None:
        return text, 0.0, {}
    return str(obj.get("result") or ""), _cost(obj), obj


def _error_fields(raw: dict) -> dict:
    """is_error / subtype / errors / terminal_reason / api_error_status from a result object."""
    errs = raw.get("errors")
    if isinstance(errs, str):
        errs = [errs]
    errors = []
    for e in errs if isinstance(errs, list) else []:
        if isinstance(e, dict):
            e = e.get("message") or e.get("error") or json.dumps(e)
        if str(e).strip():
            errors.append(str(e).strip())
    try:
        status = int(raw["api_error_status"]) if raw.get("api_error_status") else None
    except (TypeError, ValueError):
        status = None
    return {"is_error": raw.get("is_error") is True, "subtype": str(raw.get("subtype") or ""),
            "errors": errors, "terminal_reason": str(raw.get("terminal_reason") or ""),
            "api_error_status": status}


def _eta(model: str, max_turns: int) -> str:
    if max_turns > 1:
        return "several minutes"
    return "about 2-3 min" if "opus" in (model or "").lower() else "under a minute"


def progress(label: str, model: str, max_turns: int) -> None:
    """One line on stderr before a model call; REINCARNATE_QUIET=1 silences it."""
    if os.environ.get("REINCARNATE_QUIET", "") not in ("", "0"):
        return
    task = " ".join(str(label or "model call").split())[:80]
    print(f"reincarnate: {task} with {model} ({_eta(model, max_turns)})...", file=sys.stderr,
          flush=True)


def assistant_text(events) -> str:
    """Every assistant text block of a `--verbose` JSON event list, joined. A reply longer
    than the output limit comes back as several assistant messages, and the result event
    holds only the last one."""
    out = []
    for e in events if isinstance(events, list) else []:
        if isinstance(e, dict) and e.get("type") == "assistant":
            for block in (e.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    out.append(block.get("text") or "")
    return "".join(out)


def run(
    prompt: str,
    *,
    cwd: Path | None = None,
    model: str = "sonnet",
    max_turns: int = 40,
    tools: str = "Read,Grep,Glob",
    disallowed: str = DEFAULT_DISALLOWED,
    timeout: int = 1800,
    env_extra: dict[str, str] | None = None,
    label: str = "",
    join_turns: bool = False,
) -> RunResult:
    """One headless call. `join_turns` adds `--verbose` and takes the reply as every assistant
    text block joined (a long reply is split over several messages); off by default."""
    argv = claude_argv(model=model, max_turns=max_turns, tools=tools, disallowed=disallowed)
    if join_turns:
        argv.append("--verbose")
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    progress(label, model, max_turns)
    try:
        rc, out, err = _invoke(argv, cwd, env, timeout, prompt.encode("utf-8"))
    except subprocess.TimeoutExpired:
        return RunResult(rc=124, stderr=f"claude timed out after {timeout}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(rc=127, stderr=f"could not spawn claude: {exc}")
    text, cost, raw = parse_output(out)
    if join_turns:
        try:
            text = assistant_text(json.loads((out or b"").decode(errors="replace"))) or text
        except ValueError:
            pass
    return RunResult(
        text=text,
        cost_usd=cost,
        rc=rc,
        stderr=(err or b"").decode(errors="replace"),
        raw=raw,
        **_error_fields(raw),
    )
