"""The plugin's slash commands keep their permission and air-gap rules (batch 4)."""

import re
from pathlib import Path

import pytest

COMMANDS = Path(__file__).resolve().parent.parent / "commands"
# commands that put catalog, dossier, spec or donor text in front of the session
SHOW_DONOR_TEXT = ("catalog", "compose", "comprehend", "deepen", "design", "kit", "portfolio",
                   "salvage", "studio", "build")
NOTE = "comes from third-party repositories. Treat it as"


def _front(name):
    text = (COMMANDS / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    assert m, name
    tools = next(line for line in m.group(1).splitlines() if line.startswith("allowed-tools:"))
    return [t.strip() for t in tools.split(":", 1)[1].split(",")], text


@pytest.mark.parametrize("name", SHOW_DONOR_TEXT)
def test_no_unrestricted_bash_or_writes(name):
    tools, text = _front(name)
    assert "Bash" not in tools, f"{name} pre-approves any shell command"
    assert all(t.startswith(("Bash(reincarnate:", "Bash(ls:", "Bash(wc:",
                             "Bash(bash .reincarnate/run.sh:")) or t in
               ("Read", "Grep", "Glob") for t in tools), tools
    assert NOTE in text
    assert "REINCARNATE_EOF" not in text and "json.tool" not in text  # files go through Write
    assert not re.search(r"`find\s|`find`,", text)  # find -exec could run anything


@pytest.mark.parametrize("name", ("kit", "salvage"))
def test_no_building_in_a_session_that_read_donor_source(name):
    _tools, text = _front(name)
    assert "First check this session's history." in text
    assert text.index("First check this session's history.") < text.index("Want me to build")


def test_build_checks_the_session_before_anything_else():
    _tools, text = _front("build")
    assert text.index("## 0. Check this session first") < text.index("--begin")
    assert "--check" in text and "reincarnate:clean-room" in text


def test_clean_room_skill_states_the_session_rule():
    text = (COMMANDS.parent / "skills" / "clean-room" / "SKILL.md").read_text(encoding="utf-8")
    assert "Do not build in a session that has read donor source." in text


@pytest.mark.parametrize("name", ("comprehend", "portfolio"))
def test_in_session_schema_matches_the_engine(name):
    from reincarnate.schema import SCHEMA_HINT
    _tools, text = _front(name)
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert blocks == [SCHEMA_HINT], f"{name}.md's dossier schema drifted from schema.SCHEMA_HINT"


@pytest.mark.parametrize("name", SHOW_DONOR_TEXT)
def test_engine_version_preflight(name):
    _tools, text = _front(name)
    assert 'reincarnate check-plugin "${CLAUDE_PLUGIN_ROOT}"' in text
