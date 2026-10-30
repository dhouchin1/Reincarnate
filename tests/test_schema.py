import json

import pytest

from reincarnate.schema import (
    SCHEMA_HINT,
    extract_dossier_json,
    extract_json,
    normalize_dossier,
    validate_dossier,
)


def test_schema_hint_is_valid_json_shape():
    assert "fit_to_projects" in SCHEMA_HINT and "fit_to_fleet" not in SCHEMA_HINT
    hint = json.loads(SCHEMA_HINT.replace("|", "/"))  # a|b enum notation is not JSON
    assert isinstance(hint, dict)
    (cap,) = hint["capabilities"]
    assert {"name", "what", "files", "dependencies", "portability", "portability_notes",
            "security_notes", "provides", "requires", "novelty", "acceptance", "data_shapes",
            "key_values"} <= set(cap)


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
    assert one["fields"] == [{"name": "f", "type": "t" * 68 + " [truncated]",
                              "meaning": "m" * 388 + " [truncated]"}]
    assert nds([{"name": "S", "fields": "oops"}]) == [{"name": "S", "fields": [], "notes": ""}]


def test_normalize_key_values_limits_and_bad_input():
    from reincarnate.schema import normalize_key_values as nkv

    assert nkv(None) == nkv("x") == nkv([3, {}, {"name": "only name"}]) == []
    out = nkv([{"name": f"k{i}", "value": 5, "meaning": "m" * 500} for i in range(30)])
    assert len(out) == 20 and out[0]["value"] == "5" and len(out[0]["meaning"]) == 400
    assert out[0]["meaning"].endswith(" [truncated]")
    kv = nkv([{"name": "n" * 200, "value": "v" * 200}])[0]
    assert len(kv["name"]) == 120 and kv["value"] == "v" * 200  # 200 is under the 300 cap


def test_caps_cut_at_word_boundary_with_marker():
    """Over-long text is never cut mid-word without saying so (v0.4 cut checks and URLs)."""
    from reincarnate.schema import normalize_key_values as nkv
    long = " ".join(["https://example.org/icons/" + str(i) for i in range(40)])
    v = nkv([{"name": "icon sources", "value": long}])[0]["value"]
    assert len(v) <= 300 and v.endswith(" [truncated]")
    assert v[: -len(" [truncated]")] in long and long.startswith(v[: -len(" [truncated]")])
    assert v[: -len(" [truncated]")].split(" ")[-1] in long.split(" ")  # last word is whole


def test_normalize_dossier_applies_shapes_and_hint_lists_them():
    d = normalize_dossier({"summary": "s", "capabilities": [
        {"name": "a", "data_shapes": [{"name": "Board", "fields": [{"name": "x"}]}],
         "key_values": [{"name": "bp", "value": "768px"}]}, {"name": "b", "data_shapes": 3}]})
    a, b = d["capabilities"]
    assert a["data_shapes"][0]["name"] == "Board" and a["key_values"][0]["value"] == "768px"
    assert b["data_shapes"] == [] and b["key_values"] == []
    assert '"data_shapes"' in SCHEMA_HINT and '"key_values"' in SCHEMA_HINT


@pytest.mark.parametrize("text,expect,want", [
    ('{"a": 1}', dict, {"a": 1}),
    ('Here:\n```json\n{"a": 1}\n```\n', dict, {"a": 1}),
    ('```json\n{"a": 1}\n```\nlater\n```json\n{"b": 2}\n```', dict, {"b": 2}),
    ('```json\n{"a": 1}\n```\n```\n{"zz": 9}\n```', dict, {"a": 1}),  # json fence beats bare
    ('{"a": 1, "b": [1, 2]}\nNote: use {braces} sparingly {', dict, {"a": 1, "b": [1, 2]}),
    ('I will use {x} and {y} here: {"a": 1}', dict, {"a": 1}),
    ('{"a": "line one\nline two"}', dict, {"a": "line one\nline two"}),
    ('see [1, 2, 3] ok', list, [1, 2, 3]),
    ('{"a": 1}', list, None),
    ('[1, 2]', dict, None),
    ('{"a": 1} then {"b": {"c": 2}}', dict, {"b": {"c": 2}}),
    ("no json here {oops", dict, None),
    ("", dict, None),
])
def test_extract_json_table(text, expect, want):
    assert extract_json(text, expect) == want
