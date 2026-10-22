"""Jev, TypeSafe's "System One" typed decision model, as a small stdlib client.

Jev never writes text. It answers typed questions about a piece of ``state`` with
probabilities on every option:

  noul    yes/no, answered as P(yes) in [0, 1] (no confidence field; we use |2p-1|)
  choice  one of up to 255 named options (``criteria`` maps option -> when it applies)
  score   position on an ordered rubric (``criteria`` is a list of levels)

Wire format (``POST {base_url}``, ``Authorization: Bearer <key>``)::

    {"model": "jev-latest", "state": "...",
     "questions": {"<key>": {"type": "choice", "instructions": "...",
                             "criteria": {"opt": "when it applies"}}}}
    -> {"model": "jev-1.13.0",
        "answers": {"<key>": {"type": "choice", "choice": "opt", "confidence": 0.7,
                              "probabilities": {"opt": 0.7}}},
        "usage": {"input_tokens": 428, "output_tokens": 73}}

Verified against docs.typesafe.ai on 2026-10-22 and pinned by tests/test_jev.py with a mocked
transport. It has never run against the live API from here (no key on this machine).

Two backends behind one ``ask(state, questions)`` interface:
  HttpJevClient  the real API (needs ``TYPESAFE_API_KEY``).
  SimJevClient   a lexical stand-in so every caller runs end to end with no key.
                 It is NOT evidence of Jev's answer quality or latency.

Used by ``enrich.py`` (Tier 2: canonicalize interface tokens, rate novelty, with confidence that
gates escalation to Tier 3 ``review.py``) and by ``shortlist`` for the compose pool.
"""

from __future__ import annotations

import concurrent.futures
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://api.typesafe.ai/v1/systemone"
MAX_QUESTIONS_PER_REQUEST = 13  # not documented by TypeSafe; matches their cookbook batching
RETRY_STATUSES = (429, 529)
KEY_ENV = "TYPESAFE_API_KEY"

_sleep = time.sleep  # tests replace this so backoff costs nothing


class JevError(RuntimeError):
    """A Jev request that could not be answered (auth, validation, exhausted retries)."""


@dataclass
class JevQuestion:
    key: str
    type: str  # "noul" | "choice" | "score"
    instructions: str
    criteria: dict[str, str] | list[str] | None = None
    # SimJevClient only, never sent on the wire: the text compared against ``state``
    # (noul) or against each criterion (choice), and fixed scores for options with no
    # comparable text.
    sim_subject: str = ""
    sim_floor: dict[str, float] = field(default_factory=dict)

    def wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        if self.criteria is not None:
            out["criteria"] = self.criteria
        return out


@dataclass
class JevAnswer:
    key: str
    type: str
    value: Any  # P(yes) for noul, 0..1 position for score, option key for choice
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float | None = None

    def level(self, n_levels: int) -> int:
        """Score answers: the argmax level index (falls back to rounding the 0..1 value)."""
        if self.probabilities:
            try:
                best = max(self.probabilities, key=lambda k: self.probabilities[k])
                return max(0, min(n_levels - 1, int(best)))
            except ValueError:
                pass
        return max(0, min(n_levels - 1, round(float(self.value or 0.0) * (n_levels - 1))))


def _new_stats() -> dict[str, Any]:
    return {"calls": 0, "questions": 0, "input_tokens": 0, "seconds": 0.0}


class _ClientBase:
    name = ""

    def __init__(self) -> None:
        self.stats = _new_stats()
        self._lock = threading.Lock()

    def _record(self, *, questions: int, input_tokens: int, seconds: float) -> None:
        with self._lock:
            self.stats["calls"] += 1
            self.stats["questions"] += questions
            self.stats["input_tokens"] += input_tokens
            self.stats["seconds"] = round(self.stats["seconds"] + seconds, 6)

    def ask(self, state: str, questions: Sequence[JevQuestion]) -> dict[str, JevAnswer]:
        raise NotImplementedError


def parse_answers(payload: dict[str, Any]) -> dict[str, JevAnswer]:
    """A ``/v1/systemone`` response body -> ``{key: JevAnswer}``. Unknown types are skipped."""
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers, dict):
        raise JevError(f"response has no 'answers' object: {str(payload)[:200]}")
    out: dict[str, JevAnswer] = {}
    for key, a in answers.items():
        if not isinstance(a, dict):
            continue
        qtype = str(a.get("type") or "")
        probs_raw = a.get("probabilities") if isinstance(a.get("probabilities"), dict) else {}
        probs = {str(k): float(v) for k, v in probs_raw.items() if isinstance(v, (int, float))}
        conf = a.get("confidence")
        conf = float(conf) if isinstance(conf, (int, float)) else None
        if qtype == "noul":
            p = float(a.get("noul") or 0.0)
            out[str(key)] = JevAnswer(str(key), "noul", p, probs, abs(2 * p - 1))
        elif qtype == "choice":
            out[str(key)] = JevAnswer(str(key), "choice", str(a.get("choice") or ""), probs, conf)
        elif qtype == "score":
            out[str(key)] = JevAnswer(str(key), "score", float(a.get("score") or 0.0), probs, conf)
    return out


def _error_detail(body: bytes) -> str:
    text = (body or b"").decode("utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(obj, dict):
        err = obj.get("error", obj.get("detail", obj))
        return (err if isinstance(err, str) else json.dumps(err))[:500]
    return text[:500]


class HttpJevClient(_ClientBase):
    """The real TypeSafe API over urllib. 429 and 529 retry with exponential backoff."""

    name = "jev"

    def __init__(self, api_key: str, base_url: str = BASE_URL, model: str = "jev-latest",
                 timeout_s: float = 20, max_questions: int = MAX_QUESTIONS_PER_REQUEST,
                 retries: int = 3) -> None:
        super().__init__()
        self._key = api_key
        self.base_url = base_url
        self.model = model
        self.timeout_s = timeout_s
        self.max_questions = max(1, int(max_questions))
        self.retries = retries

    def __repr__(self) -> str:  # never leak the key
        return f"HttpJevClient(model={self.model!r}, base_url={self.base_url!r})"

    def _request(self, url: str, body: dict[str, Any] | None) -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}
        delay = 0.5
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method="POST" if body is not None else "GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in RETRY_STATUSES and attempt < self.retries:
                    _sleep(delay)
                    delay *= 2
                    continue
                raise JevError(f"jev HTTP {e.code}: {_error_detail(e.read())}") from e
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
                raise JevError(f"jev request failed: {type(e).__name__}: {e}") from e
        raise JevError("jev retries exhausted")

    def ask(self, state: str, questions: Sequence[JevQuestion]) -> dict[str, JevAnswer]:
        out: dict[str, JevAnswer] = {}
        for i in range(0, len(questions), self.max_questions):
            chunk = questions[i:i + self.max_questions]
            body = {"model": self.model, "state": state,
                    "questions": {q.key: q.wire() for q in chunk}}
            t0 = time.monotonic()
            payload = self._request(self.base_url, body)
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            self._record(questions=len(chunk), input_tokens=int(usage.get("input_tokens") or 0),
                         seconds=time.monotonic() - t0)
            out.update(parse_answers(payload))
        return out

    def list_models(self) -> Any:
        base = self.base_url.rstrip("/")
        url = (base.rsplit("/", 1)[0] if base.endswith("/systemone") else base) + "/models"
        return self._request(url, None)


# -- offline stand-in ---------------------------------------------------------------------
_STOP = frozenset("a an and are as at be by for from in into is it of on or that the this to "
                  "with when where which who its their than then use used using via".split())
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 1}


def _cosine(a: set[str], b: set[str]) -> float:
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def _softmax(scores: dict[str, float], scale: float) -> dict[str, float]:
    if not scores:
        return {}
    top = max(scores.values())
    exps = {k: math.exp((v - top) * scale) for k, v in scores.items()}
    total = sum(exps.values()) or 1.0
    return {k: v / total for k, v in exps.items()}


class SimJevClient(_ClientBase):
    """Offline lexical stand-in for Jev. NOT evidence of Jev quality or latency.

    noul    logistic over word-set cosine(state, sim_subject or instructions)
    choice  softmax over word-set cosine(sim_subject or state, option key + criterion text);
            ``sim_floor`` options (e.g. "keep") get a fixed score instead
    score   softmax over word-set cosine(state, level text), probabilities keyed "0".."n-1"

    Deterministic and stdlib only. It exists so the Jev code path is runnable and testable
    before a key exists; its speed and answers say nothing about the real model.
    """

    name = "jev-sim"

    def ask(self, state: str, questions: Sequence[JevQuestion]) -> dict[str, JevAnswer]:
        t0 = time.monotonic()
        st = _words(state)
        out: dict[str, JevAnswer] = {}
        for q in questions:
            if q.type == "noul":
                cos = _cosine(st, _words(q.sim_subject or q.instructions))
                p = 1.0 / (1.0 + math.exp(-(cos - 0.25) * 14.0))
                out[q.key] = JevAnswer(q.key, "noul", p, {"true": p, "false": 1 - p},
                                       abs(2 * p - 1))
            elif q.type == "choice" and isinstance(q.criteria, dict) and q.criteria:
                anchor = _words(q.sim_subject) if q.sim_subject else st
                scores = {k: (q.sim_floor[k] if k in q.sim_floor
                              else _cosine(anchor, _words(f"{k} {text}")))
                          for k, text in q.criteria.items()}
                probs = _softmax(scores, 12.0)
                best = max(probs, key=lambda k: (probs[k], k))
                out[q.key] = JevAnswer(q.key, "choice", best, probs, probs[best])
            elif q.type == "score" and isinstance(q.criteria, list) and q.criteria:
                scores = {str(i): _cosine(st, _words(t)) for i, t in enumerate(q.criteria)}
                probs = _softmax(scores, 12.0)
                best = max(probs, key=lambda k: (probs[k], k))
                value = int(best) / max(1, len(q.criteria) - 1)
                out[q.key] = JevAnswer(q.key, "score", value, probs, probs[best])
        self._record(questions=len(questions), input_tokens=0, seconds=time.monotonic() - t0)
        return out


def get_client(prefer: str = "auto"):
    """``jev`` needs the key (JevError otherwise); ``sim`` is the offline stand-in; ``auto``
    is the real client when the key is set, else None."""
    key = os.environ.get(KEY_ENV, "").strip()
    if prefer == "sim":
        return SimJevClient()
    if prefer == "jev":
        if not key:
            raise JevError(f"{KEY_ENV} is not set")
        return HttpJevClient(key)
    if prefer == "auto":
        return HttpJevClient(key) if key else None
    raise JevError(f"unknown jev backend {prefer!r} (use auto, jev or sim)")


def ask_many(client, jobs: Sequence[tuple[str, Sequence[JevQuestion]]], *,
             workers: int = 8) -> list[dict[str, JevAnswer] | JevError]:
    """Run ``(state, questions)`` asks concurrently; results (or the JevError) in input order."""
    def one(job):
        try:
            return client.ask(job[0], job[1])
        except JevError as exc:
            return exc

    if not jobs:
        return []
    if workers <= 1 or len(jobs) == 1:
        return [one(j) for j in jobs]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        return list(ex.map(one, jobs))


# -- compose shortlist --------------------------------------------------------------------
def _cap_state(c) -> str:
    return (f"{c.name}\n{c.what}\nprovides: {', '.join(c.provides) or '-'}\n"
            f"requires: {', '.join(c.requires) or '-'}")


def shortlist(idea: str, caps: Sequence, *, client, keep: int = 40,
              threshold: float = 0.5) -> list:
    """Cheaply narrow ``caps`` before an expensive composer call.

    One noul per capability ("useful building block for <idea>"), state = the capability's
    catalog text. Keeps P(yes) >= threshold sorted by P descending, then pads from the input
    order up to ``keep`` when fewer pass. At most ``keep`` returned. A capability whose
    request fails counts as P=0 (it can still be padded in)."""
    caps = list(caps)
    q = JevQuestion("fit", "noul",
                    f"This capability would be a useful building block for: {idea}",
                    sim_subject=idea)
    results = ask_many(client, [(_cap_state(c), [q]) for c in caps])
    prob: dict[int, float] = {}
    for i, r in enumerate(results):
        a = r.get("fit") if isinstance(r, dict) else None
        prob[i] = float(a.value) if a else 0.0
    passed = sorted((i for i in prob if prob[i] >= threshold), key=lambda i: (-prob[i], i))
    chosen = passed[:keep]
    if len(chosen) < keep:
        taken = set(chosen)
        chosen += [i for i in range(len(caps)) if i not in taken][:keep - len(chosen)]
    return [caps[i] for i in chosen]


# -- CLI ----------------------------------------------------------------------------------
def run_cli(store, args) -> dict:
    action = getattr(args, "action", "status")
    has_key = bool(os.environ.get(KEY_ENV, "").strip())
    if action == "status":
        backend = "jev" if has_key else "none"
        return {"api_key_set": has_key, "key_env": KEY_ENV, "auto_backend": backend,
                "model": "jev-latest", "text": (
                    f"{KEY_ENV}: {'set' if has_key else 'not set'}\n"
                    f"auto resolves to: {backend}"
                    + ("" if has_key else " (use --engine jev-sim for the offline stand-in)"))}
    if action == "models":
        try:
            client = get_client("jev")
            models = client.list_models()
        except JevError as exc:
            return {"error": str(exc)}
        return {"models": models, "text": json.dumps(models, indent=2)}
    return {"error": f"unknown action {action!r}"}
