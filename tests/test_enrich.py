import json

from conftest import make_dossier
from reincarnate import claude_runner, enrich


def _fake(replies, prompts=None):
    it = iter(replies)

    def invoke(argv, cwd, env, timeout):
        if prompts is not None:
            prompts.append(argv[argv.index("-p") + 1])
            assert "--setting-sources" in argv and "--max-turns" in argv
        r = next(it)
        return 0, json.dumps({"result": r, "total_cost_usd": 0.01}).encode(), b""

    return invoke


def _seed(store, n):
    caps = [(f"cap{i}", f"does {i}") for i in range(n)]
    store.upsert_dossier("o", "r", "u", "s", make_dossier(*caps))
    return [c.id for c in store.list_capabilities()]


def test_batch_updates_and_ignores_unknown_id(store, monkeypatch):
    ids = _seed(store, 2)
    prompts = []
    reply = json.dumps({str(ids[0]): {"novelty": "distinctive", "provides": ["auth:session"],
                                      "requires": ["ext:docker"]}, "9999": {"novelty": "pattern"}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake(["```json\n" + reply + "\n```"], prompts))
    r = enrich.enrich(store, all_=True)
    assert r["capabilities"] == 2 and r["batches"] == 1 and r["updated"] == 1
    assert r["failed_batches"] == [] and r["cost_usd"] == 0.01
    c = store.get_capability(ids[0])
    assert (c.novelty, c.provides, c.requires) == ("distinctive", ["auth:session"], ["ext:docker"])
    assert store.get_capability(ids[1]).novelty == ""
    p = prompts[0]
    assert "commodity: a competent coding agent" in p and "auth:session" in p
    assert f"{ids[0]} | o/r | cap0 | high | does 0" in p


def test_garbage_batch_recorded_next_applies(store, monkeypatch):
    ids = _seed(store, 3)
    good = json.dumps({str(ids[2]): {"novelty": "pattern", "provides": [], "requires": []}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake(["sorry, no json", good]))
    r = enrich.enrich(store, dossier_ref="o/r", batch=2)
    assert r["batches"] == 2 and r["updated"] == 1
    assert [f["batch"] for f in r["failed_batches"]] == [0]
    assert store.get_capability(ids[2]).novelty == "pattern"


def test_only_missing_selection(store):
    ids = _seed(store, 3)
    store.set_capability_fields(ids[0], provides=["auth:session"], novelty="pattern")
    store.set_capability_fields(ids[1], novelty="pattern")          # no interfaces
    store.set_capability_fields(ids[2], provides=["auth:session"])  # no novelty
    got = {c.id for c in enrich.select(store, all_=True, only_missing=True)}
    assert got == {ids[1], ids[2]}
    assert len(enrich.select(store, all_=True)) == 3
    assert enrich.select(store) == []


def test_run_cli_needs_target(store):
    from types import SimpleNamespace
    r = enrich.run_cli(store, SimpleNamespace(repo=None, all=False))
    assert "error" in r
