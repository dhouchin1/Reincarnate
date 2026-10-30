import json

from conftest import make_dossier
from reincarnate import claude_runner, enrich


def _fake(replies, prompts=None):
    it = iter(replies)

    def invoke(argv, cwd, env, timeout, stdin):
        if prompts is not None:
            prompts.append(stdin.decode())
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


# -- Jev engine -----------------------------------------------------------------------------
from reincarnate import jev  # noqa: E402


class FixedClient:
    """Deterministic Jev stand-in: fixed novelty level/confidence and a fixed mapping."""
    name = "fixed"

    def __init__(self, level=2, nov_conf=0.9, map_to=None, map_conf=0.9):
        self.level, self.nov_conf, self.map_to, self.map_conf = level, nov_conf, map_to, map_conf
        self.stats = {"calls": 0, "questions": 0, "input_tokens": 0, "seconds": 0.0}
        self.asked = []

    def ask(self, state, questions):
        self.stats["calls"] += 1
        self.asked.append((state, questions))
        out = {}
        for q in questions:
            if q.type == "score":
                probs = {str(i): (self.nov_conf if i == self.level else 0.05)
                         for i in range(len(q.criteria))}
                out[q.key] = jev.JevAnswer(q.key, "score", self.level / 2, probs, self.nov_conf)
            else:
                v = (self.map_to if q.key.startswith("p:") else None) or "keep"
                out[q.key] = jev.JevAnswer(q.key, "choice", v, {v: self.map_conf}, self.map_conf)
        return out


def test_jev_engine_maps_token_sets_novelty_and_confidence(store):
    ids = _seed(store, 2)
    store.set_capability_fields(ids[0], provides=["auth:zzz-login"], requires=["ext:docker"])
    assert "auth:zzz-login" not in store.get_vocabulary().tokens
    canon = next(t for t in store.get_vocabulary().tokens if t.startswith("auth:"))
    fc = FixedClient(level=2, nov_conf=0.9, map_to=canon, map_conf=0.8)
    r = enrich.enrich(store, all_=True, engine="jev", client=fc)
    c = store.get_capability(ids[0])
    assert c.provides == [canon] and c.requires == ["ext:docker"]
    assert c.novelty == "distinctive" and c.confidence == 0.8 and not c.needs_review
    assert r["engine_used"] == "fixed" and r["mapped_tokens"] == 1
    assert r["flagged_for_review"] == 0 and r["jev_stats"]["calls"] == 2
    # the question offers only same-kind canonical tokens plus keep
    qs = [q for s, qq in fc.asked for q in qq if q.type == "choice"]
    assert {q.key for q in qs} <= {"p:auth:zzz-login", "r:ext:docker"}
    p_q = next(q for q in qs if q.key.startswith("p:"))
    assert set(p_q.criteria) - {"keep"} <= {
        t for t in store.get_vocabulary().tokens if t.startswith("auth:")}
    assert "keep" in p_q.criteria
    assert "portability notes" in fc.asked[0][0] and "donor: o/r" in fc.asked[0][0]


def test_jev_low_confidence_flagged_and_keep_leaves_token(store):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], provides=["data:zzz-novel"])
    fc = FixedClient(level=1, nov_conf=0.9, map_to="keep", map_conf=0.3)
    r = enrich.enrich(store, all_=True, engine="jev", review_threshold=0.6, client=fc)
    c = store.get_capability(ids[0])
    assert c.provides == ["data:zzz-novel"] and c.novelty == "pattern"
    assert c.confidence == 0.3 and c.needs_review is True
    assert r["flagged_for_review"] == 1 and r["mapped_tokens"] == 0
    assert [x.id for x in store.list_capabilities(needs_review=True)] == [ids[0]]
    assert store.list_capabilities(needs_review=False) == []


def test_jev_sim_engine_end_to_end(store):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], provides=["auth:user-session-login"])
    # threshold 0: the sim's confidence is low, and a below-threshold answer is no longer applied
    r = enrich.enrich(store, all_=True, engine="jev-sim", review_threshold=0.0)
    c = store.get_capability(ids[0])
    assert r["engine_used"] == "jev-sim" and r["jev_stats"]["questions"] >= 2
    assert c.novelty in ("commodity", "pattern", "distinctive") and c.confidence is not None
    assert c.needs_review is False
    vocab = store.get_vocabulary()
    assert all(t in vocab.tokens or t == "auth:user-session-login" for t in c.provides)


def test_jev_engine_without_key_returns_error(store, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    _seed(store, 1)
    assert "TYPESAFE_API_KEY" in enrich.enrich(store, all_=True, engine="jev")["error"]
    assert "error" in enrich.enrich(store, all_=True, engine="nope")


def test_jev_choice_over_255_is_trimmed(store):
    store.conn.executemany("INSERT INTO vocab_tokens(token, definition) VALUES (?, ?)",
                           [(f"data:bulk-{i}", f"bulk {i}") for i in range(300)])
    store.conn.commit()
    crit = enrich._choice_criteria(store.get_vocabulary(), "data:bulk-7")
    assert len(crit) == 255 and "keep" in crit and "data:bulk-7" in crit


def test_haiku_engine_clears_confidence(store, monkeypatch):
    ids = _seed(store, 1)
    store.set_capability_fields(ids[0], confidence=0.2, needs_review=True)
    reply = json.dumps({str(ids[0]): {"novelty": "pattern", "provides": [], "requires": []}})
    monkeypatch.setattr(claude_runner, "_invoke", _fake([reply]))
    r = enrich.enrich(store, all_=True)
    c = store.get_capability(ids[0])
    assert c.confidence is None and c.needs_review is False
    assert r["engine_used"] == "haiku" and r["flagged_for_review"] == 0
