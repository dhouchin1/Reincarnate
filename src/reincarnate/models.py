"""Dataclasses shared by every module. All are JSON-friendly via to_dict()/from_dict()."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


class _Base:
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Profile(_Base):
    name: str = ""
    stack: list[str] = field(default_factory=list)
    intents: list[str] = field(default_factory=list)
    license_policy: dict = field(default_factory=lambda: {"deny": [], "warn": ["unknown"]})
    created: str = ""
    updated: str = ""


@dataclass
class Project(_Base):
    slug: str = ""
    path: str = ""
    repo_url: str = ""
    stack: list[str] = field(default_factory=list)
    goal: str = ""
    test_cmd: str = ""
    created: str = ""


@dataclass
class Dossier(_Base):
    id: int = 0
    owner: str = ""
    repo: str = ""
    url: str = ""
    sha: str = ""
    comprehended_at: str = ""
    model: str = ""
    cost_usd: float = 0.0
    source: str = "claude-p"
    summary: str = ""
    license: str = ""
    data: dict = field(default_factory=dict)

    @property
    def donor(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def capabilities(self) -> list[dict]:
        return self.data.get("capabilities", [])

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["donor"] = self.donor
        return d


@dataclass
class Capability(_Base):
    id: int = 0
    dossier_id: int = 0
    idx: int = 0
    name: str = ""
    what: str = ""
    files: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    portability: str = "medium"
    portability_notes: str = ""
    security_notes: str = ""
    donor: str = ""
    license: str = ""
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    novelty: str = ""
    confidence: float | None = None
    needs_review: bool = False
    acceptance: list[str] = field(default_factory=list)


@dataclass
class Fit(_Base):
    capability_id: int = 0
    project_slug: str = ""
    score: float = 0.0
    how: str = ""
    source: str = "overlay"


@dataclass
class Composition(_Base):
    id: int = 0
    slug: str = ""
    name: str = ""
    pitch: str = ""
    idea: str = ""
    target_kind: str = ""
    target_slug: str = ""
    proposal: dict = field(default_factory=dict)
    status: str = "proposed"
    created: str = ""


@dataclass
class Design(_Base):
    id: int = 0
    slug: str = ""
    idea: str = ""
    title: str = ""
    requirements: list[dict] = field(default_factory=list)
    options: list[dict] = field(default_factory=list)
    engine: str = ""
    meta: dict = field(default_factory=dict)
    created: str = ""
