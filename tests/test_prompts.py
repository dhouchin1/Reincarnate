from reincarnate import prompts
from reincarnate.models import Profile, Project
from reincarnate.schema import SCHEMA_HINT

PROFILE = Profile(name="d", stack=["python", "sqlite"], intents=["ship local-first tools"])
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
    assert "Dan" not in p


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
