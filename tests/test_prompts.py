from reincarnate import prompts
from reincarnate.models import Profile, Project
from reincarnate.schema import SCHEMA_HINT

NAME = "Zebulon Quartermain"
PROFILE = Profile(name=NAME, stack=["python", "sqlite"], intents=["ship local-first tools"])
PROJECTS = [
    Project(slug="alpha", goal="a crawler", stack=["go", "redis"]),
    Project(slug="beta", goal="a dashboard", stack=["typescript"]),
]


def test_comprehension_prompt_contents():
    p = prompts.build_comprehension_prompt("o", "r", PROFILE, PROJECTS)
    assert "fit_to_projects" in p
    assert "o/r" in p
    for needle in ("alpha", "beta", "go", "redis", "typescript", "python", "sqlite"):
        assert needle in p
    assert "ship local-first tools" in p
    assert "DO NOT" in p and "never" in p.lower()
    assert SCHEMA_HINT in p
    assert NAME not in p and "Quartermain" not in p


def test_comprehension_prompt_without_profile():
    p = prompts.build_comprehension_prompt("o", "r", None, [])
    assert "fit_to_projects" in p


def test_overlay_prompt():
    data = {"capabilities": [{"name": "retry queue", "what": "retries", "dependencies": []}]}
    p = prompts.build_overlay_prompt(data, PROFILE, PROJECTS)
    assert "retry queue" in p and "alpha" in p
    assert '"capability"' in p and '"score"' in p and '"how"' in p
    assert prompts.build_overlay_prompt({}, None, None)


def test_compose_prompt():
    rows = [{"id": 7, "donor": "o/r", "license": "MIT", "name": "cache", "what": "caches",
             "portability": "high"}]
    p = prompts.build_compose_prompt("a note app", rows, PROFILE, PROJECTS, "new:notes")
    assert "gaps" in p and "suggested_donors" in p and "build_fresh" in p
    assert "id=7" in p and "new:notes" in p and "a note app" in p
    assert "Never reference, quote, or paste donor code" in p
    assert "license_flags" in p
    assert prompts.build_compose_prompt("x", [], None, None, None)


def test_excerpt_prompt_contents():
    p = prompts.build_excerpt_prompt(
        "o", "r", "FILE TREE\n===== FILE: a.py =====\nx", PROFILE, PROJECTS,
        vocab_block="- data:service-status: health")
    for needle in ("commodity", "pattern", "distinctive", "provides", "requires", "alpha",
                   "python", "o/r", "===== FILE: a.py =====", "data:service-status",
                   "never instructions", "CLAUDE.md", "5-12", "ext:"):
        assert needle in p
    assert SCHEMA_HINT in p
    assert "ENTIRE reply" in p and "```json" in p


def test_excerpt_prompt_without_vocab_or_profile():
    p = prompts.build_excerpt_prompt("o", "r", "x", None, [])
    assert "Canonical interface vocabulary" not in p
    assert "distinctive" in p


def test_agentic_prompt_has_new_fields_and_vocab():
    p = prompts.build_comprehension_prompt("o", "r", PROFILE, PROJECTS, vocab_block="- ui:panel")
    assert prompts.NOVELTY_RUBRIC in p and prompts.INTERFACE_RULES in p and "- ui:panel" in p
    assert "Canonical interface vocabulary" not in prompts.build_comprehension_prompt(
        "o", "r", PROFILE, PROJECTS)


def test_comprehension_prompts_ask_for_shapes_and_values():
    for p in (prompts.build_comprehension_prompt("o", "r", PROFILE, PROJECTS),
              prompts.build_excerpt_prompt("o", "r", "EXCERPT", PROFILE, PROJECTS)):
        assert prompts.DATA_SHAPE_RULES in p
        assert "field by field" in p and "`name`, `type`, `meaning`" in p
        assert "concrete values" in p and '"data_shapes"' in p and '"key_values"' in p
        assert "exactly as found in the code" in p  # the concrete-values acceptance rule


def test_enrich_acceptance_never_invents_numbers():
    rules = prompts.CATALOG_ACCEPTANCE_RULES
    assert "Never invent a number" in rules and "[value unknown: <what>]" in rules
    assert "exactly as found in the code" not in rules


def test_deepen_prompt_framing():
    cap = {"name": "Board layout", "what": "tiles", "portability_notes": "easy"}
    p = prompts.build_deepen_prompt(cap, "A dashboard.", ["a/b.ts", "c.ts"])
    assert prompts.UNTRUSTED_DATA_RULE in p and "Read only what you need" in p
    assert "- a/b.ts" in p and "Board layout" in p and "A dashboard." in p
    assert "No file paths and no library or framework names" in p
    assert "```json" in p and '"data_shapes"' in p and '"key_values"' in p
    assert "concrete values you found in the code" in p
    assert "(none listed" in prompts.build_deepen_prompt(cap, "", [])


def test_no_prompt_builder_leaks_the_profile_name():
    """_profile_block renders stack and intents only; the name must never reach an LLM."""
    cap = {"name": "retry queue", "what": "retries", "dependencies": []}
    row = {"id": 7, "donor": "o/r", "license": "MIT", "name": "cache", "what": "caches",
           "portability": "high"}
    built = {
        "comprehension": prompts.build_comprehension_prompt("o", "r", PROFILE, PROJECTS),
        "excerpt": prompts.build_excerpt_prompt("o", "r", "EXCERPT", PROFILE, PROJECTS),
        "overlay": prompts.build_overlay_prompt({"capabilities": [cap]}, PROFILE, PROJECTS),
        "compose": prompts.build_compose_prompt("a note app", [row], PROFILE, PROJECTS, None),
    }
    for kind, p in built.items():
        assert "ship local-first tools" in p, kind  # the profile did reach the prompt
        assert NAME not in p and "Zebulon" not in p and "Quartermain" not in p, kind
