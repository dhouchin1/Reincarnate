import json

from conftest import make_dossier
from reincarnate import claude_runner, review


def _fake(replies, prompts):
    it = iter(replies)

    def invoke(argv, cwd, env, timeout):
        prompts.append(argv[argv.index("-p") + 1])
        assert "--setting-sources" in argv
        assert argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
        return 0, json.dumps({"result": next(it), "total_cost_usd": 0.05}).encode(), b""

    return invoke


def _seed(store, n):
    d = make_dossier(*[(f"cap{i}", f"does {i}") for i in range(n)])
    d["architecture"] = "ARCH-PROSE-MARKER " + "x" * 3000
    store.upsert_dossier("o", "r", "u", "s", d)
    return [c.id for c in store.list_capabilities()]


def test_only_flagged_sent_applied_and_cleared(store, monkeypatch):
    ids = _seed(store, 3)
    store.set_capability_fields(ids[0], confidence=0.2, needs_review=True)
    store.set_capability_fields(ids[2], confidence=0.1, needs_review=True, novelty="commodity")
    prompts = []
    reply = json.dumps({str(ids[0]): {"novelty": "distinctive", "provides": ["auth:session"],
                                      "requires": [], "note": "ok"},
                        str(ids[1]): {"novelty": "pattern"}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake([reply], prompts))
    r = review.review(store)
    assert r["flagged"] == 2 and r["batches"] == 1 and r["reviewed"] == 1
    p = prompts[0]
    assert f"[{ids[0]}]" in p and f"[{ids[2]}]" in p and f"[{ids[1]}]" not in p
    assert "ARCH-PROSE-MARKER" in p and "x" * 1600 not in p
    assert "commodity: a competent coding agent" in p and "auth:" in p
    a = store.get_capability(ids[0])
    assert (a.novelty, a.provides, a.confidence, a.needs_review) == (
        "distinctive", ["auth:session"], 1.0, False)
    assert store.get_capability(ids[1]).novelty == ""  # unflagged id in the reply is ignored
    assert store.get_capability(ids[2]).needs_review is True  # not answered: stays flagged
    assert r["notes"] == {str(ids[0]): "ok"}


def test_garbage_batch_recorded_and_next_applies(store, monkeypatch):
    ids = _seed(store, 2)
    for i in ids:
        store.set_capability_fields(i, confidence=0.1, needs_review=True)
    good = json.dumps({str(ids[1]): {"novelty": "pattern", "provides": [], "requires": []}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake(["no json", good], []))
    r = review.review(store, batch=1)
    assert r["batches"] == 2 and r["reviewed"] == 1
    assert [f["batch"] for f in r["failed_batches"]] == [0]
    assert store.get_capability(ids[0]).needs_review is True
    assert store.get_capability(ids[1]).needs_review is False


def test_nothing_flagged_makes_no_call(store, monkeypatch):
    _seed(store, 2)
    prompts = []
    monkeypatch.setattr(claude_runner, "_invoke", _fake([], prompts))
    r = review.run_cli(store, type("A", (), {})())
    assert r["flagged"] == 0 and prompts == []


def test_limit(store, monkeypatch):
    ids = _seed(store, 3)
    for i in ids:
        store.set_capability_fields(i, confidence=0.1, needs_review=True)
    prompts = []
    monkeypatch.setattr(claude_runner, "_invoke", _fake(["{}"], prompts))
    assert review.review(store, limit=2)["flagged"] == 2
