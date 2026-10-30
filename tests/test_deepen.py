import json
from types import SimpleNamespace

from reincarnate import catalog, claude_runner, comprehend, deepen, render
from reincarnate.models import Composition


def _reply(what="Pinned behavior.", **extra):
    body = {"what": what,
            "data_shapes": [{"name": "Board", "fields": [{"name": "apps", "type": "list",
                                                          "meaning": "tiles"}]}],
            "key_values": [{"name": "md", "value": "768px", "meaning": "breakpoint"}],
            "acceptance": ["At 768px the layout switches to two columns"], **extra}
    return "```json\n" + json.dumps(body) + "\n```"


def _setup(store, tmp_path, monkeypatch, replies, clone_fail=()):
    """Two dossiers: o/r has caps a, b; p/q has cap c. Returns ids and the call log."""
    store.upsert_dossier("o", "r", "https://github.com/o/r", "s1", {
        "summary": "Dash.", "capabilities": [
            {"name": "a", "what": "old a", "files": ["x.ts"]},
            {"name": "b", "what": "old b", "files": ["y.ts"]}]})
    store.upsert_dossier("p", "q", "https://github.com/p/q", "s2", {
        "summary": "Other.", "capabilities": [{"name": "c", "what": "old c"}]})
    clones, prompts_seen = [], []

    def fake_clone(url, **kw):
        clones.append(url)
        if url in clone_fail:
            raise RuntimeError("git clone failed: nope")
        return {"path": str(tmp_path), "sha": "deadbeef", "owner": "x", "repo": "y"}

    queue = list(replies)

    def fake_invoke(argv, cwd, env, timeout, stdin):
        prompts_seen.append((argv, cwd, stdin.decode()))
        out = json.dumps({"result": queue.pop(0), "total_cost_usd": 0.01})
        return 0, out.encode(), b""

    monkeypatch.setattr(comprehend, "clone_repo", fake_clone)
    monkeypatch.setattr(claude_runner, "_invoke", fake_invoke)
    return [c.id for c in store.list_capabilities()], clones, prompts_seen


def test_deepen_applies_fields_and_meta(store, tmp_path, monkeypatch):
    ids, clones, seen = _setup(store, tmp_path, monkeypatch, [_reply(), _reply("Pinned b.")])
    r = deepen.deepen(store, ids[:2])
    assert r["deepened"] == ids[:2] and r["failed"] == [] and r["skipped"] == []
    assert r["capabilities"] == 2 and abs(r["cost_usd"] - 0.02) < 1e-9
    assert clones == ["https://github.com/o/r"]  # one clone per dossier
    a = store.get_capability(ids[0])
    assert a.what == "Pinned behavior." and a.key_values[0]["value"] == "768px"
    assert a.data_shapes[0]["name"] == "Board"
    assert a.acceptance == ["At 768px the layout switches to two columns"]
    assert (a.deepened_sha, a.deepened_model) == ("deadbeef", "claude-haiku-5-5")
    assert a.deepened_at
    assert store.get_capability(ids[1]).what == "Pinned b."
    argv, cwd, prompt = seen[0]
    assert cwd == tmp_path and "Read,Grep,Glob" in argv and "--setting-sources" in argv
    assert "x.ts" in prompt and not any("x.ts" in part for part in argv)
    assert "deepened 2 of 2" in r["text"]


def test_deepen_skips_already_deepened_unless_force(store, tmp_path, monkeypatch):
    ids, clones, _ = _setup(store, tmp_path, monkeypatch, [_reply(), _reply("Forced.")])
    deepen.deepen(store, [ids[0]])
    r = deepen.deepen(store, [ids[0]])
    assert r["skipped"] == [ids[0]] and r["deepened"] == [] and len(clones) == 1
    r = deepen.deepen(store, [ids[0]], force=True)
    assert r["deepened"] == [ids[0]] and store.get_capability(ids[0]).what == "Forced."


def test_deepen_garbage_reply_fails_one_and_continues(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, ["I could not do that", _reply()])
    r = deepen.deepen(store, ids[:2])
    assert [f["capability_id"] for f in r["failed"]] == [ids[0]]
    assert r["failed"][0]["reason"] == "no JSON in the model's reply: I could not do that"
    assert r["deepened"] == [ids[1]]
    a = store.get_capability(ids[0])
    assert a.what == "old a" and a.deepened_at == ""


def test_deepen_empty_what_keeps_old_text(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [_reply(what="  ")])
    r = deepen.deepen(store, [ids[0]])
    a = store.get_capability(ids[0])
    assert r["deepened"] == [ids[0]] and a.what == "old a" and a.key_values


def test_deepen_clone_failure_fails_only_that_dossier(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [_reply()],
                       clone_fail=("https://github.com/o/r",))
    r = deepen.deepen(store, ids)
    assert sorted(f["capability_id"] for f in r["failed"]) == sorted(ids[:2])
    assert "clone failed" in r["failed"][0]["reason"]
    assert r["deepened"] == [ids[2]]


def test_deepen_unknown_capability_fails(store, tmp_path, monkeypatch):
    _setup(store, tmp_path, monkeypatch, [])
    r = deepen.deepen(store, [9999])
    assert r["failed"] == [{"capability_id": 9999, "reason": "no such capability"}]


def test_run_cli_ids_composition_and_errors(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [_reply(), _reply()])
    args = SimpleNamespace(ids=[], composition=None, model="claude-haiku-5-5", max_turns=25,
                           force=False, json=False)
    assert "text" in deepen.run_cli(store, args) and "error" in deepen.run_cli(store, args)
    args.composition = "nope"
    r = deepen.run_cli(store, args)
    assert "error" in r and "text" in r
    store.save_composition(Composition(slug="comp", proposal={"capabilities": [
        {"capability_id": ids[0]}, {"capability_id": ids[2]}]}))
    args.composition = "comp"
    r = deepen.run_cli(store, args)
    assert r["deepened"] == [ids[0], ids[2]]
    assert deepen.run_cli(store, SimpleNamespace(
        ids=[ids[0]], composition=None, model="m", max_turns=3, force=False))["skipped"] == [ids[0]]


def test_deepened_fields_reach_catalog_and_render(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [_reply()])
    deepen.deepen(store, [ids[0]])
    e = [x for x in catalog.build_catalog(store) if x.capability_id == ids[0]][0].to_dict()
    assert e["data_shapes"][0]["name"] == "Board" and e["key_values"][0]["name"] == "md"
    d = store.get_dossier(1)
    d.data = {"capabilities": [{"name": "a", "data_shapes": e["data_shapes"],
                                "key_values": e["key_values"]}]}
    md = render.render_dossier_markdown(d)
    assert "Data shape `Board`" in md and "`apps` (list): tiles" in md
    assert "md: `768px` - breakpoint" in md


def _fails(store, tmp_path, monkeypatch, reply):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [reply])
    r = deepen.deepen(store, [ids[0]])
    return ids, r


def test_deepen_empty_or_error_json_is_a_failure_and_changes_nothing(store, tmp_path,
                                                                     monkeypatch):
    for reply in ('```json\n{}\n```', '{"error": "I could not read the files"}'):
        store.upsert_dossier("o", "r", "https://github.com/o/r", "s1", {
            "summary": "Dash.", "capabilities": [{"name": "a", "what": "old a"}]})
        ids, r = _fails(store, tmp_path, monkeypatch, reply)
        assert r["deepened"] == [] and len(r["failed"]) == 1
        assert "nothing was changed" in r["failed"][0]["reason"]
        a = store.get_capability(ids[0])
        assert a.what == "old a" and a.deepened_at == ""


def test_deepen_empty_shapes_keep_the_existing_ones(store, tmp_path, monkeypatch):
    ids, _, _ = _setup(store, tmp_path, monkeypatch, [_reply(), _reply(
        "Second.", data_shapes=[], key_values=[], acceptance=[])])
    deepen.deepen(store, [ids[0]])
    r = deepen.deepen(store, [ids[0]], force=True)
    a = store.get_capability(ids[0])
    assert r["deepened"] == [ids[0]] and a.what == "Second."
    assert a.data_shapes[0]["name"] == "Board" and a.key_values[0]["value"] == "768px"


def test_deepen_partial_reply_applies_only_key_values_and_stamps(store, tmp_path, monkeypatch):
    reply = '{"key_values": [{"name": "k", "value": "v", "meaning": "m"}]}'
    ids, r = _fails(store, tmp_path, monkeypatch, reply)
    a = store.get_capability(ids[0])
    assert r["deepened"] == [ids[0]] and a.deepened_at
    assert a.key_values[0]["name"] == "k" and a.what == "old a" and a.data_shapes == []
    assert a.basis == {"key_values": "observed"}


def test_deepen_stores_quirks_and_they_reach_the_prompt(store, tmp_path, monkeypatch):
    ids, _, seen = _setup(store, tmp_path, monkeypatch,
                          [_reply(quirks=["A migration drops the owner field.", ""])])
    deepen.deepen(store, [ids[0]])
    a = store.get_capability(ids[0])
    assert a.quirks == ["A migration drops the owner field."]
    assert a.data_shapes[0]["name"] == "Board"
    assert "`quirks`" in seen[0][2] and '"quirks": [""]' in seen[0][2]


def test_deepen_reply_with_only_quirks_is_not_a_success(store, tmp_path, monkeypatch):
    ids, r = _fails(store, tmp_path, monkeypatch, '{"quirks": ["data loss on save"]}')
    a = store.get_capability(ids[0])
    assert r["deepened"] == [] and len(r["failed"]) == 1
    assert a.quirks == [] and a.deepened_at == ""
