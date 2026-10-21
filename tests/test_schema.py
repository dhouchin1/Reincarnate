import json

from reincarnate.schema import (
    SCHEMA_HINT,
    extract_dossier_json,
    normalize_dossier,
    validate_dossier,
)


def test_schema_hint_is_valid_json_shape():
    assert "fit_to_projects" in SCHEMA_HINT and "fit_to_fleet" not in SCHEMA_HINT


def test_normalize_renames_fit_to_fleet_and_cleans_caps():
    d = normalize_dossier({
        "summary": "s",
        "fit_to_fleet": [{"project": "p", "capability": "c", "how": "h"}],
        "capabilities": [
            {"name": "A", "portability": "HIGH", "files": "x.py"},
            {"name": "B", "portability": "great"},
            {"what": "nameless"},
        ],
    })
    assert "fit_to_fleet" not in d
    assert d["fit_to_projects"][0]["project"] == "p"
    assert [c["name"] for c in d["capabilities"]] == ["A", "B"]
    assert d["capabilities"][0]["portability"] == "high"
    assert d["capabilities"][0]["files"] == ["x.py"]
    assert d["capabilities"][1]["portability"] == "medium"
    assert d["languages"] == [] and d["build_system"] == ""
    assert validate_dossier(d) == []


def test_validate_reports_problems():
    assert validate_dossier({"capabilities": "x"})
    assert validate_dossier([])


def test_extract_uses_last_fence_with_prose():
    text = (
        "intro\n```json\n{\"a\": 1}\n```\nmore\n"
        "```json\n{\"summary\": \"final\"}\n```\ntrailing prose here"
    )
    assert extract_dossier_json(text) == {"summary": "final"}


def test_extract_bare_and_trailing_prose_and_none():
    assert extract_dossier_json(json.dumps({"x": 1})) == {"x": 1}
    assert extract_dossier_json('{"x": 1}\nthanks!') == {"x": 1}
    assert extract_dossier_json("no json at all") is None
    assert extract_dossier_json("") is None
