import json

from conftest import make_dossier
from reincarnate import estimate


def _seed(store, n, model="claude-haiku-5-5", cost=0.02, source="claude-p"):
    for i in range(n):
        store.upsert_dossier("o", f"r{i}-{model}-{source}", "https://x", f"sha{i}",
                             make_dossier(("a", "does a")), model=model, cost_usd=cost + i / 100,
                             source=source)


def test_constants_when_history_is_short(store):
    _seed(store, 2)  # two samples are not enough
    e = estimate.estimate_comprehend(store, 3, "excerpt", "haiku", False)
    assert (e["low"], e["high"]) == (0.03, 0.09)
    assert e["per_repo"] == 0.02
    assert "constants" in e["basis"] and "2 recorded" in e["basis"]
    assert e["minutes"] == {"low": 3, "high": 9}


def test_agentic_and_sonnet_constants(store):
    a = estimate.estimate_comprehend(store, 1, "agentic", "haiku", False)
    assert (a["low"], a["high"]) == (0.02, 0.05)
    assert a["minutes"] == {"low": 2, "high": 5}
    s = estimate.estimate_comprehend(store, 1, "excerpt", "sonnet", False)
    assert (s["low"], s["high"]) == (0.1, 0.3)


def test_history_median_and_range_for_the_same_model(store):
    _seed(store, 3)  # 0.02, 0.03, 0.04
    _seed(store, 5, model="claude-sonnet-5-5", cost=1.0)  # another model never mixes in
    _seed(store, 5, source="session", cost=9.0)  # nor does a session-authored dossier
    e = estimate.estimate_comprehend(store, 2, "excerpt", "haiku", False)
    assert (e["low"], e["high"], e["per_repo"]) == (0.04, 0.08, 0.03)
    assert "3 recorded claude-haiku-5-5 runs" in e["basis"] and "median" in e["basis"]


def test_tier2_adds_per_part_cost_from_catalog_mean(store):
    # empty catalog: 8 parts expected
    e = estimate.estimate_comprehend(store, 1, "excerpt", "haiku", True)
    assert e["low"] == round(0.01 + 8 * 0.0002, 4)
    assert "Tier 2" in e["basis"]
    d = make_dossier(*[(f"c{i}", f"does {i}") for i in range(4)])
    store.upsert_dossier("a", "b", "https://x", "s1", d, model="m", cost_usd=0, source="session")
    e = estimate.estimate_comprehend(store, 1, "excerpt", "haiku", True)
    assert e["high"] == round(0.03 + 4 * 0.0002, 4)


def test_estimate_deepen():
    e = estimate.estimate_deepen(10)
    assert e["low"] == e["high"] == 0.11
    json.dumps(e)


def test_estimate_design_scales_with_options():
    three = estimate.estimate_design(3, [1, 2])
    assert three["low"] == round(0.36 * 0.6, 4) and three["high"] == round(0.36 * 1.4, 4)
    assert three["minutes"] == {"low": 1, "high": 3}
    assert "Opus" in three["basis"] and "2 pinned" in three["basis"]
    two, four = estimate.estimate_design(2), estimate.estimate_design(4)
    assert two["high"] < three["high"] < four["high"]
    assert "pinned" not in two["basis"]
    assert estimate.estimate_design(9)["high"] == four["high"]  # clamped to 2-4
    json.dumps(four)
