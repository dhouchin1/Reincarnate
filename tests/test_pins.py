"""Pinned parts in `design`: forced into the pool, the prompt and every option."""

import json
from types import SimpleNamespace

import pytest

from p2_catalog import seed
from reincarnate import claude_runner, design


@pytest.fixture(autouse=True)
def no_claude(monkeypatch):
    def boom(argv, cwd, env, timeout, stdin):
        raise AssertionError("unexpected claude call")
    monkeypatch.setattr(claude_runner, "_invoke", boom)


@pytest.fixture
def ids(store):
    return seed(store)


def _reply(payload, cost=0.01):
    return 0, json.dumps({"type": "result", "result": json.dumps(payload),
                          "total_cost_usd": cost}).encode(), b""


def _model_reply(ids):
    c = {k: f"c{v}" for k, v in ids.items()}
    return {"title": "Boards", "requirements": [{"id": "r1", "text": "dashboard"}],
            "options": [
                {"slot": "lean", "name": "Lite", "capabilities": [
                    {"id": c["Session login"], "role": "auth"},
                    {"id": c["Team dashboard"], "role": "ui"}]},
                {"slot": "complete", "name": "Full", "capabilities": [
                    {"id": c["Layout grid"], "role": "layout"},
                    {"id": c["Activity feed"], "role": "events"},
                    {"id": c["Team dashboard"], "role": "ui"}]}]}


def _scripted(monkeypatch, payloads):
    prompts = []

    def fake(argv, cwd, env, timeout, stdin):
        prompts.append(stdin.decode())
        return _reply(payloads[len(prompts) - 1])
    monkeypatch.setattr(claude_runner, "_invoke", fake)
    return prompts


def test_offline_engine_includes_the_pins_even_outside_the_top_of_the_pool(store, ids):
    d = design.propose_design(store, "team dashboard of tiles", engine="offline",
                              pins=[ids["Activity feed"]],
                              roles={ids["Activity feed"]: "event source"})
    for o in d.options:
        feed = next(c for c in o["capabilities"] if c["name"] == "Activity feed")
        assert feed["pinned"] is True and feed["role"] == "component"
    assert d.meta["pins"] == [ids["Activity feed"]]


def test_a_pin_the_model_left_out_is_added_with_its_role_and_noted(store, ids, monkeypatch):
    prompts = _scripted(monkeypatch, [_model_reply(ids), {"verdicts": {}}])
    pin = ids["Retry helper"]  # commodity: outside the default pool, forced in by the pin
    d = design.propose_design(store, "team dashboard", pins=[pin, f"c{ids['Layout grid']}"],
                              roles={str(pin): "retries", ids["Layout grid"]: "tile layout"})
    assert "REQUIRED PARTS" in prompts[0]
    assert f"c{pin} (retries)" in prompts[0]
    assert f"c{ids['Layout grid']} (tile layout)" in prompts[0]
    for o in d.options:
        by_name = {c["name"]: c for c in o["capabilities"]}
        assert by_name["Retry helper"]["pinned"] and by_name["Retry helper"]["role"] == "retries"
        assert by_name["Layout grid"]["pinned"]
    lean = next(o for o in d.options if o["slot"] == "lean")
    assert next(c for c in lean["capabilities"] if c["name"] == "Layout grid")["role"] \
        == "tile layout"
    assert any("Retry helper" in n and "missing from option Lite" in n for n in d.meta["notes"])
    full = next(o for o in d.options if o["slot"] == "complete")
    assert not any("Layout grid" in n and "Full" in n for n in d.meta["notes"])  # already in
    assert next(c for c in full["capabilities"] if c["name"] == "Layout grid")["pinned"]
    assert next(c for c in lean["capabilities"] if c["name"] == "Session login").get("pinned") \
        is None
    assert lean["wiring"]  # wiring was computed with the added parts


def test_an_unroled_pin_gets_the_default_role(store, ids, monkeypatch):
    _scripted(monkeypatch, [_model_reply(ids), {"verdicts": {}}])
    d = design.propose_design(store, "team dashboard", pins=[ids["Activity feed"]])
    lean = next(o for o in d.options if o["slot"] == "lean")
    assert next(c for c in lean["capabilities"] if c["name"] == "Activity feed")["role"] \
        == "pinned"


def test_session_engine_adds_missing_pins(store, ids, tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(_model_reply(ids)))
    d = design.propose_design(store, "team dashboard", engine="session", proposal_path=str(p),
                              verify=False, pins=[ids["Activity feed"]])
    for o in d.options:
        assert any(c["name"] == "Activity feed" and c["pinned"] for c in o["capabilities"])


def test_pins_that_cannot_be_honoured_are_refused_up_front(store, ids):
    with pytest.raises(ValueError, match="does not exist"):
        design.propose_design(store, "x", engine="offline", pins=[99999])
    with pytest.raises(ValueError, match="license your profile denies"):
        design.propose_design(store, "x", engine="offline", pins=[ids["Email notifier"]])
    store.conn.execute("UPDATE capabilities SET retired_at='2026-01-01' WHERE id=?",
                       (ids["Layout grid"],))
    with pytest.raises(ValueError, match="is retired"):
        design.propose_design(store, "x", engine="offline", pins=[ids["Layout grid"]])
    with pytest.raises(ValueError, match="not a capability id"):
        design.propose_design(store, "x", engine="offline", pins=["seventeen"])
    assert store.list_designs() == []


def test_a_refused_pin_never_calls_the_model(store, ids):
    # the autouse fixture raises on any claude call
    with pytest.raises(ValueError):
        design.propose_design(store, "x", pins=[99999])


def test_no_pins_keeps_the_old_behaviour(store, ids, monkeypatch):
    prompts = _scripted(monkeypatch, [_model_reply(ids), {"verdicts": {}}])
    d = design.propose_design(store, "team dashboard")
    assert "REQUIRED PARTS" not in prompts[0]
    assert all("pinned" not in c for o in d.options for c in o["capabilities"])


def test_parse_pins_takes_both_forms():
    assert design.parse_pins(["17,c22", "c17", 5, " 6 "]) == [17, 22, 5, 6]
    assert design.parse_pins(None) == []
    assert design.parse_pins("3,c4") == [3, 4]
    with pytest.raises(ValueError):
        design.parse_pins(["17,x"])
    with pytest.raises(ValueError):
        design.parse_pins([True])


def test_cli_pin_flag(store, ids, capsys):
    a = design.run_cli(store, SimpleNamespace(
        idea=["team", "dashboard"], options=3, engine="offline", proposal=None, model=None,
        include_commodity=False, no_verify=True,
        pin=[f"{ids['Layout grid']},c{ids['Activity feed']}"]))
    assert "error" not in a
    d = store.get_design(a["slug"])
    assert d.meta["pins"] == [ids["Layout grid"], ids["Activity feed"]]
    bad = design.run_cli(store, SimpleNamespace(
        idea=["team"], options=3, engine="offline", proposal=None, model=None,
        include_commodity=False, no_verify=True, pin=["zzz"]))
    assert bad["error"].startswith("--pin:")


def test_cli_parser_accepts_pin():
    from reincarnate import cli
    args = cli.build_parser().parse_args(["design", "an idea", "--pin", "17,c22", "--pin", "5"])
    assert args.pin == ["17,c22", "5"]
