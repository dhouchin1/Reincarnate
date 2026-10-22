import io
import json
import urllib.error

import pytest

from conftest import make_dossier
from reincarnate import jev
from reincarnate.jev import (
    HttpJevClient,
    JevError,
    JevQuestion,
    SimJevClient,
    get_client,
    parse_answers,
    shortlist,
)

EXAMPLE = {
    "model": "jev-1.13.0",
    "answers": {
        "a": {"type": "choice", "choice": "opt", "confidence": 0.7,
              "probabilities": {"opt": 0.7, "other": 0.3}},
        "b": {"type": "score", "score": 0.67, "confidence": 0.5, "legend": {"0": "x"},
              "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}},
        "c": {"type": "noul", "noul": 0.99},
    },
    "usage": {"input_tokens": 428, "output_tokens": 73},
}


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body):
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(json.dumps(body).encode()))


@pytest.fixture
def wire(monkeypatch):
    """Queue of responses (dict -> 200, Exception -> raised); records every request."""
    calls, queue = [], []

    def urlopen(req, timeout=None):
        calls.append({"url": req.full_url, "method": req.get_method(), "timeout": timeout,
                      "headers": {k.lower(): v for k, v in req.header_items()},
                      "body": json.loads(req.data) if req.data else None})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(jev, "_sleep", lambda s: calls.append({"sleep": s}))
    return calls, queue


def _qs(n):
    return [JevQuestion(f"q{i}", "noul", f"is {i}?") for i in range(n)]


def test_wire_body_and_headers(wire):
    calls, queue = wire
    queue.append(EXAMPLE)
    c = HttpJevClient("sekret-key")
    qs = [JevQuestion("a", "choice", "pick", {"opt": "when x", "other": "when y"}),
          JevQuestion("b", "score", "rate", ["l0", "l1", "l2"]),
          JevQuestion("c", "noul", "yes?")]
    out = c.ask("some state", qs)
    req = calls[0]
    assert req["url"] == "https://api.typesafe.ai/v1/systemone" and req["method"] == "POST"
    assert req["headers"]["authorization"] == "Bearer sekret-key"
    assert req["headers"]["content-type"] == "application/json"
    assert req["timeout"] == 20
    assert req["body"] == {"model": "jev-latest", "state": "some state", "questions": {
        "a": {"type": "choice", "instructions": "pick",
              "criteria": {"opt": "when x", "other": "when y"}},
        "b": {"type": "score", "instructions": "rate", "criteria": ["l0", "l1", "l2"]},
        "c": {"type": "noul", "instructions": "yes?"}}}
    assert set(out) == {"a", "b", "c"}
    assert c.stats["calls"] == 1 and c.stats["questions"] == 3
    assert c.stats["input_tokens"] == 428 and "sekret-key" not in repr(c)


def test_parse_documented_example():
    out = parse_answers(EXAMPLE)
    assert out["a"].value == "opt" and out["a"].confidence == 0.7
    assert out["a"].probabilities == {"opt": 0.7, "other": 0.3}
    assert out["b"].value == 0.67 and out["b"].confidence == 0.5 and out["b"].level(3) == 2
    assert out["c"].type == "noul" and out["c"].value == 0.99
    with pytest.raises(JevError):
        parse_answers({"nope": 1})


def test_noul_confidence_is_distance_from_half():
    a = parse_answers({"answers": {"x": {"type": "noul", "noul": 0.9},
                                   "y": {"type": "noul", "noul": 0.5}}})
    assert a["x"].confidence == pytest.approx(0.8) and a["y"].confidence == 0.0


def test_chunking_at_13_and_configurable(wire):
    calls, queue = wire
    for lo, hi in ((0, 13), (13, 20)):
        queue.append({"answers": {f"q{i}": {"type": "noul", "noul": 0.5} for i in range(lo, hi)}})
    out = HttpJevClient("k").ask("s", _qs(20))
    bodies = [c["body"] for c in calls]
    assert [len(b["questions"]) for b in bodies] == [13, 7] and len(out) == 20
    queue.append({"answers": {}})
    queue.append({"answers": {}})
    HttpJevClient("k", max_questions=5).ask("s", _qs(10))
    assert [len(c["body"]["questions"]) for c in calls[2:]] == [5, 5]


def test_429_then_200_retried_with_backoff(wire):
    calls, queue = wire
    queue.extend([_http_error(429, {"error": "slow down"}),
                  _http_error(529, {"error": "busy"}), EXAMPLE])
    out = HttpJevClient("k").ask("s", _qs(1))
    assert "a" in out
    assert [c["sleep"] for c in calls if "sleep" in c] == [0.5, 1.0]


def test_retries_exhausted_raises(wire):
    calls, queue = wire
    queue.extend([_http_error(429, {"error": "slow"}) for _ in range(4)])
    with pytest.raises(JevError, match="429"):
        HttpJevClient("k").ask("s", _qs(1))
    assert len([c for c in calls if "sleep" in c]) == 3


def test_422_raises_with_detail_no_retry(wire):
    calls, queue = wire
    queue.append(_http_error(422, {"error": "questions.q0.criteria: required for choice"}))
    with pytest.raises(JevError) as ei:
        HttpJevClient("k").ask("s", _qs(1))
    assert "422" in str(ei.value) and "criteria: required" in str(ei.value)
    assert not [c for c in calls if "sleep" in c]


def test_network_error_wrapped(wire):
    calls, queue = wire
    queue.append(urllib.error.URLError("boom"))
    with pytest.raises(JevError, match="request failed"):
        HttpJevClient("k").ask("s", _qs(1))


def test_list_models(wire):
    calls, queue = wire
    queue.append({"data": [{"id": "jev-latest"}]})
    assert HttpJevClient("k").list_models() == {"data": [{"id": "jev-latest"}]}
    assert calls[0]["url"] == "https://api.typesafe.ai/v1/models" and calls[0]["method"] == "GET"


def test_sim_is_deterministic_and_named():
    qs = [JevQuestion("c", "choice", "which", {"auth:session": "login session cookie",
                                               "store:cache": "cache of values",
                                               "keep": "none"},
                      sim_subject="session login", sim_floor={"keep": 0.05}),
          JevQuestion("n", "noul", "q", sim_subject="login session"),
          JevQuestion("s", "score", "r", ["boring plumbing", "a login flow", "novel trick"])]
    a = SimJevClient().ask("user login session handling", qs)
    b = SimJevClient().ask("user login session handling", qs)
    assert {k: (v.value, v.probabilities) for k, v in a.items()} == \
           {k: (v.value, v.probabilities) for k, v in b.items()}
    assert a["c"].value == "auth:session"
    assert a["c"].confidence == a["c"].probabilities["auth:session"]
    assert a["n"].value > 0.5 and a["s"].level(3) == 1
    c = SimJevClient()
    assert c.name == "jev-sim"
    c.ask("x", qs)
    assert c.stats["calls"] == 1 and c.stats["questions"] == 3


def test_get_client(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert get_client("auto") is None
    assert get_client("sim").name == "jev-sim"
    with pytest.raises(JevError):
        get_client("jev")
    monkeypatch.setenv("TYPESAFE_API_KEY", "k")
    assert get_client("auto").name == "jev" and get_client("jev").name == "jev"


class _Caps:
    def __init__(self, i, what):
        self.id, self.name, self.what, self.provides, self.requires = i, f"c{i}", what, [], []


class _FakeClient:
    name = "fake"

    def __init__(self, p):
        self.p = p
        self.stats = {}

    def ask(self, state, questions):
        pr = self.p[state.split("\n")[0]]
        return {questions[0].key: jev.JevAnswer("fit", "noul", pr, {}, abs(2 * pr - 1))}


def test_shortlist_threshold_sort_and_padding():
    caps = [_Caps(i, "w") for i in range(6)]
    fake = _FakeClient({"c0": 0.2, "c1": 0.9, "c2": 0.6, "c3": 0.1, "c4": 0.5, "c5": 0.8})
    assert [c.id for c in shortlist("idea", caps, client=fake, keep=3)] == [1, 5, 2]
    # fewer pass than keep: passers first, then padded from input order
    assert [c.id for c in shortlist("idea", caps, client=fake, keep=5, threshold=0.7)] == \
           [1, 5, 0, 2, 3]
    assert len(shortlist("idea", caps, client=fake, keep=40)) == 6
    assert shortlist("idea", [], client=fake) == []


def test_shortlist_failure_counts_as_zero():
    class Boom:
        name = "boom"
        stats = {}

        def ask(self, state, questions):
            raise JevError("down")

    caps = [_Caps(i, "w") for i in range(3)]
    assert [c.id for c in shortlist("idea", caps, client=Boom(), keep=2)] == [0, 1]


def test_shortlist_with_sim_prefers_overlap(store):
    store.upsert_dossier("o", "r", "u", "s", make_dossier(
        ("session login", "user login and session cookies"),
        ("zebra herder", "herds zebras across plains")))
    caps = store.list_capabilities()
    got = shortlist("login session cookies for users", caps, client=SimJevClient(), keep=1)
    assert [c.name for c in got] == ["session login"]
