"""Thin wrapper around headless `claude -p`. Never raises on a non-zero exit code."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def claude_argv(
    prompt: str,
    *,
    model: str,
    max_turns: int,
    tools: str = "Read,Grep,Glob",
    disallowed: str = DEFAULT_DISALLOWED,
) -> list[str]:
    """Build the `claude -p` argv. `$REINCARNATE_CLAUDE_BIN` overrides the binary."""
    binary = os.environ.get("REINCARNATE_CLAUDE_BIN") or shutil.which("claude") or "claude"
    return [
        binary,
        "-p",
        prompt,
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


def _invoke(argv: list[str], cwd: Path | None, env: dict[str, str], timeout: int):
    """Run the subprocess. Separate so tests can stub it. Returns (rc, stdout, stderr)."""
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=env,
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
) -> RunResult:
    argv = claude_argv(prompt, model=model, max_turns=max_turns, tools=tools, disallowed=disallowed)
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    try:
        rc, out, err = _invoke(argv, cwd, env, timeout)
    except subprocess.TimeoutExpired:
        return RunResult(rc=124, stderr=f"claude timed out after {timeout}s")
    except (OSError, subprocess.SubprocessError) as exc:
        return RunResult(rc=127, stderr=f"could not spawn claude: {exc}")
    text, cost, raw = parse_output(out)
    return RunResult(
        text=text,
        cost_usd=cost,
        rc=rc,
        stderr=(err or b"").decode(errors="replace"),
        raw=raw,
    )
