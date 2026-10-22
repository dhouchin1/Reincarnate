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


def test_normalize_new_capability_fields():
    d = normalize_dossier({"capabilities": [
        {"name": "A", "provides": [" Data:X ", "data:x", "store:y", "", 5],
         "requires": ["ext:docker"] + [f"data:t{i}" for i in range(10)],
         "novelty": " Distinctive "},
        {"name": "B", "provides": "data:x", "requires": None, "novelty": "legendary"},
        {"name": "C"},
    ]})
    a, b, c = d["capabilities"]
    assert a["provides"] == ["data:x", "store:y", "5"]
    assert len(a["requires"]) == 6 and a["requires"][0] == "ext:docker"
    assert a["novelty"] == "distinctive"
    assert b["provides"] == [] and b["requires"] == [] and b["novelty"] == ""
    assert c["provides"] == [] and c["novelty"] == ""


def test_schema_hint_has_new_fields():
    for k in ("provides", "requires", "novelty"):
        assert f'"{k}"' in SCHEMA_HINT


def test_normalize_data_shapes_limits_and_bad_input():
    from reincarnate.schema import normalize_data_shapes as nds

    assert nds(None) == nds("x") == nds({"name": "a"}) == [] and nds([1, "a", {}]) == []
    shapes = [{"name": f"S{i}", "fields": [{"name": f"f{j}"} for j in range(50)]}
              for i in range(12)]
    out = nds(shapes)
    assert len(out) == 8 and len(out[0]["fields"]) == 40
    one = nds([{"name": " " + "n" * 200, "notes": "z" * 900, "fields": [
        {"name": "f", "type": "t" * 200, "meaning": "m" * 500}, {"type": "no name"}, "bad"]}])[0]
    assert len(one["name"]) == 80 and len(one["notes"]) == 400
    assert one["fields"] == [{"name": "f", "type": "t" * 80, "meaning": "m" * 240}]
    assert nds([{"name": "S", "fields": "oops"}]) == [{"name": "S", "fields": [], "notes": ""}]


def test_normalize_key_values_limits_and_bad_input():
    from reincarnate.schema import normalize_key_values as nkv

    assert nkv(None) == nkv("x") == nkv([3, {}, {"name": "only name"}]) == []
    out = nkv([{"name": f"k{i}", "value": 5, "meaning": "m" * 500} for i in range(30)])
    assert len(out) == 20 and out[0]["value"] == "5" and len(out[0]["meaning"]) == 240
    assert len(nkv([{"name": "n" * 200, "value": "v" * 200}])[0]["name"]) == 80


def test_normalize_dossier_applies_shapes_and_hint_lists_them():
    d = normalize_dossier({"summary": "s", "capabilities": [
        {"name": "a", "data_shapes": [{"name": "Board", "fields": [{"name": "x"}]}],
         "key_values": [{"name": "bp", "value": "768px"}]}, {"name": "b", "data_shapes": 3}]})
    a, b = d["capabilities"]
    assert a["data_shapes"][0]["name"] == "Board" and a["key_values"][0]["value"] == "768px"
    assert b["data_shapes"] == [] and b["key_values"] == []
    assert '"data_shapes"' in SCHEMA_HINT and '"key_values"' in SCHEMA_HINT
