"""Capability interfaces: provides/requires tokens and the deterministic wiring check.

Port of Forge Studio's interfaces.py. A token is ``kind:thing`` (``auth:user-identity``).
``check_wiring`` classifies every need of every capability in an option:

  wired     another capability in the option provides it
  unmet     nothing in the option provides it (with catalog capabilities that would)
  settings  a ``config:`` value the operator supplies
  external  an ``ext:`` environment dependency (OS, third-party API)
  owned     a ``store:`` nobody in the catalog provides, the capability's own state
  platform  generic app-shell infrastructure (HTTP server, page shell, database)

Tokens find *candidate* links; a model judges them, because tokens alone top out around
50% precision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any

KINDS: dict[str, str] = {
    "auth": "identity, sessions, permissions",
    "store": "persisted data a capability owns or reads",
    "http": "HTTP endpoints and server surfaces",
    "event": "signals other parts react to",
    "notify": "outbound delivery to people",
    "ui": "user-facing surfaces",
    "schedule": "timers and background loops",
    "config": "configuration the operator supplies",
    "data": "data shapes passed between parts",
    "ext": "environment / third-party systems (never provided by a capability)",
}
_TOKEN_RE = re.compile(r"^([a-z]+):([a-z0-9][a-z0-9./-]{0,48})$")
MAX_TOKENS_PER_SIDE = 6


def normalize_token(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    if ":" not in s:
        return ""
    kind, _, thing = s.partition(":")
    thing = re.sub(r"[\s_]+", "-", thing.strip())
    thing = re.sub(r"-{2,}", "-", thing).strip("-")
    tok = f"{kind.strip()}:{thing}"
    m = _TOKEN_RE.match(tok)
    return tok if m and m.group(1) in KINDS else ""


def normalize_list(items: Any) -> list[str]:
    out: list[str] = []
    for it in items if isinstance(items, list) else []:
        t = normalize_token(it)
        if t and t not in out:
            out.append(t)
        if len(out) >= MAX_TOKENS_PER_SIDE:
            break
    return out


def is_external(tok: str) -> bool:
    return tok.startswith("ext:")


@dataclass
class Vocabulary:
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    aliases: dict[str, str | None] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.tokens)

    @property
    def platform(self) -> set[str]:
        return {t for t, d in self.tokens.items() if d.get("platform")}

    def canonicalize(self, toks: list[str]) -> list[str]:
        out: list[str] = []
        for t in toks:
            c: str | None = t
            if t not in self.tokens and not is_external(t) and t in self.aliases:
                c = self.aliases[t]
                if c is not None and c not in self.tokens:
                    c = t  # an alias target outside the vocabulary is ignored
            if c and c not in out:
                out.append(c)
        return out

    def block(self) -> str:
        """The vocabulary as prompt text, grouped by kind."""
        by_kind: dict[str, list[str]] = {}
        for t, d in sorted(self.tokens.items()):
            by_kind.setdefault(t.split(":", 1)[0], []).append(f"  {t} — {d.get('definition', '')}")
        return "\n".join(f"{k}:\n" + "\n".join(v) for k, v in by_kind.items())


@dataclass
class Wiring:
    wired: list[dict[str, str]] = field(default_factory=list)  # token, consumer, provider
    unmet: list[dict[str, Any]] = field(default_factory=list)  # token, consumer, candidates
    settings: list[dict[str, str]] = field(default_factory=list)
    external: list[dict[str, str]] = field(default_factory=list)
    owned: list[dict[str, str]] = field(default_factory=list)
    platform: list[dict[str, str]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        names = ("wired", "unmet", "settings", "external", "owned", "platform", "unknown")
        d = {k: getattr(self, k) for k in names}
        d["counts"] = {k: len(v) for k, v in d.items()}
        return d


def check_wiring(
    option_ids: list[str],
    interfaces: dict[str, tuple[list[str], list[str]]],
    *,
    platform: set[str] | frozenset[str] = frozenset(),
    rank: dict[str, int] | None = None,
    max_candidates: int = 3,
) -> Wiring:
    """``interfaces`` maps EVERY catalog capability id -> (provides, requires)."""
    w = Wiring()
    in_option = set(option_ids)
    providers: dict[str, list[str]] = {}
    for cid, (prov, _) in interfaces.items():
        for t in prov:
            providers.setdefault(t, []).append(cid)
    for cid in option_ids:
        prov, req = interfaces.get(cid, ([], []))
        if not prov and not req:
            w.unknown.append(cid)
            continue
        for t in req:
            if t in prov:
                continue  # owns what it reads
            if is_external(t):
                w.external.append({"token": t, "consumer": cid})
                continue
            if t in platform:
                w.platform.append({"token": t, "consumer": cid})
                continue
            inside = [p for p in providers.get(t, []) if p in in_option and p != cid]
            if inside:
                w.wired.append({"token": t, "consumer": cid, "provider": inside[0]})
                continue
            if t.startswith("config:"):
                w.settings.append({"token": t, "consumer": cid})
                continue
            if t.startswith("store:") and not providers.get(t):
                w.owned.append({"token": t, "consumer": cid})
                continue
            outside = [p for p in providers.get(t, []) if p not in in_option]
            if rank:
                outside.sort(key=lambda p: rank.get(p, 99))
            w.unmet.append({"token": t, "consumer": cid, "candidates": outside[:max_candidates]})
    return w


def load_packaged_vocabulary() -> dict:
    """The seed vocabulary shipped in the package: {"tokens", "aliases", "source"}."""
    return json.loads((files("reincarnate") / "data" / "vocabulary.json").read_text("utf-8"))


def run_cli(store, args) -> dict:
    """`reincarnate vocab [--kind K]`."""
    kind = getattr(args, "kind", None)
    vocab = store.get_vocabulary()
    toks = [{"token": t, "definition": d.get("definition", ""), "platform": bool(d.get("platform"))}
            for t, d in sorted(vocab.tokens.items()) if not kind or t.split(":", 1)[0] == kind]
    lines: list[str] = []
    last = None
    for t in toks:
        k = t["token"].split(":", 1)[0]
        if k != last:
            lines.append(f"{k}: {KINDS.get(k, '')}".rstrip(": "))
            last = k
        flag = " [platform]" if t["platform"] else ""
        lines.append(f"  {t['token']}{flag} - {t['definition']}")
    return {"tokens": toks, "aliases": len(vocab.aliases), "text": "\n".join(lines) or "(empty)"}
