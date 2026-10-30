"""Cost and time estimates shown before a job runs. Recorded runs win over constants."""

from __future__ import annotations

import statistics

MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5"}
MIN_SAMPLES = 3
# measured on this pipeline: (low, high) dollars per small repo
_HAIKU = {"excerpt": (0.01, 0.03), "agentic": (0.02, 0.05)}
_SONNET_FACTOR = 10
_TIER2_PER_PART = 0.0002  # measured: 351 parts cost $0.0728
_DEEPEN_PER_PART = 0.011  # measured: 9 parts cost $0.1024
_DEFAULT_PARTS = 8
_MINUTES = {"excerpt": (1, 3), "agentic": (2, 5)}


def model_id(model: str | None) -> str:
    """`haiku` / `sonnet` or a full model id to the full id."""
    m = str(model or "haiku").strip()
    return MODELS.get(m.lower(), m)


def _history(store, model: str) -> list[float]:
    rows = store.conn.execute(
        "SELECT cost_usd FROM dossiers WHERE source='claude-p' AND model=? AND cost_usd > 0",
        (model,)).fetchall()
    return [float(r[0]) for r in rows]


def expected_parts(store) -> float:
    """Mean parts per current dossier in the catalog; 8 when the catalog is empty."""
    dossiers = store.list_dossiers(current_only=True)
    if not dossiers:
        return float(_DEFAULT_PARTS)
    return len(store.list_capabilities(current_only=True)) / len(dossiers) or float(_DEFAULT_PARTS)


def estimate_comprehend(store, n_repos: int, mode: str, model: str, enrich: bool) -> dict:
    """{low, high, per_repo, basis, minutes}: dollars for the whole batch, per_repo being the
    typical cost of one repo; minutes is {low, high} for the batch (one job at a time)."""
    n = max(0, int(n_repos))
    full = model_id(model)
    samples = _history(store, full)
    if len(samples) >= MIN_SAMPLES:
        lo, hi, mid = min(samples), max(samples), statistics.median(samples)
        basis = (f"median ${mid:.3f} and range ${lo:.3f}-${hi:.3f} per repo over {len(samples)} "
                 f"recorded {full} runs (all modes)")
    else:
        lo, hi = _HAIKU.get(mode, _HAIKU["excerpt"])
        if "sonnet" in full.lower():
            lo, hi = lo * _SONNET_FACTOR, hi * _SONNET_FACTOR
        mid = (lo + hi) / 2
        basis = (f"constants measured on this pipeline ({mode}, {full}): ${lo:.2f}-${hi:.2f} "
                 f"per small repo; {len(samples)} recorded {full} run(s), {MIN_SAMPLES} needed")
    if enrich:
        parts = expected_parts(store)
        extra = parts * _TIER2_PER_PART
        lo, hi, mid = lo + extra, hi + extra, mid + extra
        basis += f"; Tier 2 adds ${extra:.4f} per repo (about {parts:.0f} parts at $0.0002 each)"
    m_lo, m_hi = _MINUTES.get(mode, _MINUTES["excerpt"])
    return {"low": round(lo * n, 4), "high": round(hi * n, 4), "per_repo": round(mid, 4),
            "basis": basis, "minutes": {"low": m_lo * n, "high": m_hi * n}}


def estimate_deepen(n_parts: int) -> dict:
    n = max(0, int(n_parts))
    cost = round(n * _DEEPEN_PER_PART, 4)
    return {"low": cost, "high": cost, "per_part": _DEEPEN_PER_PART,
            "basis": "about $0.011 per part (measured: 9 parts cost $0.1024)"}


_DESIGN_BASE = 0.35  # one Opus design call for 3 options (measured)
_VERIFY = 0.01  # the Haiku link-verification call
_DESIGN_SPREAD = 0.4


def estimate_design(n_options: int, pins=None) -> dict:
    """{low, high, minutes: {low, high}, basis}: dollars for one design run. The Opus call
    scales with the option count; pinned parts add a few lines to the prompt, not cost."""
    n = max(2, min(4, int(n_options or 3)))
    mid = _DESIGN_BASE * n / 3 + _VERIFY
    pinned = len(list(pins or []))
    basis = (f"about ${_DESIGN_BASE:.2f} for 3 options with Opus, scaled to {n} option(s), plus "
             f"about ${_VERIFY:.2f} to verify the links; range is +/-{int(_DESIGN_SPREAD * 100)}%"
             + (f"; {pinned} pinned part(s) do not change the estimate" if pinned else ""))
    return {"low": round(mid * (1 - _DESIGN_SPREAD), 4),
            "high": round(mid * (1 + _DESIGN_SPREAD), 4),
            "minutes": {"low": 1, "high": 3}, "basis": basis}


_PLAN_BASE = 0.03  # the contracts pass: one Sonnet call, grows with the step count
_PLAN_PER_STEP = 0.006
_PLAN_SPREAD = 0.5


def estimate_plan(n_steps: int) -> dict:
    """{low, high, basis}: dollars for the kit contracts and scope pass (one Sonnet call)."""
    n = max(0, int(n_steps))
    mid = _PLAN_BASE + _PLAN_PER_STEP * n
    return {"low": round(mid * (1 - _PLAN_SPREAD), 4), "high": round(mid * (1 + _PLAN_SPREAD), 4),
            "basis": f"about ${_PLAN_BASE:.2f} plus ${_PLAN_PER_STEP:.3f} per step with Sonnet "
                     f"({n} steps); range is +/-{int(_PLAN_SPREAD * 100)}%"}


_RECONCILE_BASE = 0.04  # the reconcile pass: one Sonnet call over the specs' checks and values
_RECONCILE_PER_STEP = 0.01


def estimate_reconcile(n_steps: int) -> dict:
    """{low, high, basis}: dollars for the kit reconcile pass (one Sonnet call)."""
    n = max(0, int(n_steps))
    mid = _RECONCILE_BASE + _RECONCILE_PER_STEP * n
    return {"low": round(mid * (1 - _PLAN_SPREAD), 4), "high": round(mid * (1 + _PLAN_SPREAD), 4),
            "basis": f"about ${_RECONCILE_BASE:.2f} plus ${_RECONCILE_PER_STEP:.2f} per step with "
                     f"Sonnet ({n} steps); range is +/-{int(_PLAN_SPREAD * 100)}%"}
