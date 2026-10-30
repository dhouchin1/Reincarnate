"""Batch 9: tokenized, thresholded, vote-tie-broken search and the catalog around it."""

from types import SimpleNamespace

import pytest

from reincarnate import catalog
from reincarnate.models import Profile
from reincarnate.store import Store, search_tokens


def seed(s, caps, owner="o", repo="r"):
    s.upsert_dossier(owner, repo, "u", "sha1", {"summary": "d", "capabilities": [
        {"name": n, "what": w, "portability": p} for n, w, p in caps]})


def cid(s, name):
    return next(c.id for c in s.list_capabilities() if c.name == name)


@pytest.fixture(params=[True, False], ids=["fts", "like"])
def store(tmp_path, request):
    s = Store(tmp_path / "t.db")
    s.migrate()
    if not request.param:
        s.fts = False
    yield s
    s.close()


def names(store, q, **kw):
    return [c.name for c in store.search_capabilities(q, **kw)]


def test_tokens():
    assert search_tokens("Drag-and-Drop  the GRID drag") == ["drag", "drop", "grid"]
    assert search_tokens("a an of to is it") == []
    assert search_tokens("ui db x yy") == ["ui", "db"]
    assert search_tokens("Café déjà") == ["cafe", "deja"]
    assert search_tokens("") == [] and search_tokens(None) == []


def test_stopword_and_short_only_queries_match_nothing(store):
    seed(store, [("sand and gravel", "and so on and on", "high"), ("grid", "a grid", "high")])
    assert names(store, "and the of") == []
    assert names(store, "a an x") == []
    assert names(store, "") == []
    assert names(store, "   ?!  ") == []


def test_drag_and_drop_grid_ranks_grid_first_and_skips_and_only(store):
    seed(store, [
        ("and joiner", "joins things and things", "high"),
        ("drag drop grid", "drag and drop grid layout", "high"),
        ("drop zone", "a drop target", "high"),
        ("sortable list", "drag handles", "high"),
    ])
    got = names(store, "drag and drop grid")
    assert got[0] == "drag drop grid"
    assert "and joiner" not in got
    # n=3 -> ceil(3/2)=2 tokens: "drop zone" (1) and "sortable list" (1) fall short
    assert got == ["drag drop grid"]


def test_thresholds_by_token_count(store):
    seed(store, [
        ("alpha beta gamma delta", "alpha beta gamma delta", "high"),
        ("alpha beta gamma", "x", "high"),
        ("alpha beta", "x", "high"),
        ("alpha", "x", "high"),
    ])
    assert set(names(store, "alpha")) == {"alpha beta gamma delta", "alpha beta gamma",
                                          "alpha beta", "alpha"}
    # 2 tokens: both required
    assert set(names(store, "alpha delta")) == {"alpha beta gamma delta"}
    # 3 tokens: 2 required
    assert set(names(store, "alpha beta zzzz")) == {"alpha beta gamma delta", "alpha beta gamma",
                                                    "alpha beta"}
    # 4 tokens: 2 required, most matches first
    got = names(store, "alpha beta gamma qqqq")
    assert got[:2] == ["alpha beta gamma delta", "alpha beta gamma"]
    assert set(got) == {"alpha beta gamma delta", "alpha beta gamma", "alpha beta"}


def test_duplicate_tokens_count_once_and_hyphen_query(store):
    seed(store, [("drag drop", "drag drop", "high"), ("drag only", "drag", "high")])
    assert names(store, "drag drag drag drop") == ["drag drop"]
    assert names(store, "drag-and-drop") == ["drag drop"]


def test_unicode_words(store):
    seed(store, [("café menu", "naïve résumé parser", "high"), ("other", "thing", "high")])
    assert names(store, "cafe") == ["café menu"]
    assert names(store, "résumé parser") == ["café menu"]


def test_retired_and_superseded_stay_excluded(store):
    seed(store, [("zebra herd", "x", "high"), ("quokka", "x", "high")])
    seed(store, [("zebra herd", "x", "high")])  # same sha: quokka retired
    assert names(store, "quokka") == []
    assert names(store, "zebra") == ["zebra herd"]
    store.upsert_dossier("o", "r", "u", "sha2", {"capabilities": [
        {"name": "okapi", "what": "x", "portability": "high"}]})
    assert names(store, "zebra") == []
    assert names(store, "okapi") == ["okapi"]


def test_matches_report_words(store):
    seed(store, [("rate limiter", "token bucket throttling", "high")])
    ((c, words),) = store.search_matches("token bucket nothing")
    assert c.name == "rate limiter" and words == ["token", "bucket"]


def test_votes_only_break_ties(store):
    seed(store, [
        ("plain fetcher", "fetch pages", "high"),
        ("loved fetcher", "fetch pages", "high"),
        ("hated fetcher", "fetch pages", "high"),
        ("fetch cache", "fetch pages with cache storage", "low"),
    ])
    store.set_vote(cid(store, "loved fetcher"), 1)
    store.set_vote(cid(store, "hated fetcher"), -1)
    # two tokens: only the row matching both qualifies, whatever the votes
    assert [e.name for e in catalog.build_catalog(store, search="fetch cache")] == ["fetch cache"]
    store.set_vote(cid(store, "fetch cache"), -1)
    got = [e.name for e in catalog.build_catalog(store, search="fetch cache storage pages")]
    assert got[0] == "fetch cache"  # 4 matches beats up-voted 2-match rows
    got = [e.name for e in catalog.build_catalog(store, search="fetch pages")]
    assert got.index("loved fetcher") < got.index("plain fetcher") < got.index("hated fetcher")
    assert len(got) == 4


def test_downvoted_more_relevant_row_still_leads(store):
    seed(store, [("zeta parser", "parse zeta files", "high"), ("zeta", "just zeta", "high")])
    store.set_vote(cid(store, "zeta parser"), -1)
    store.set_vote(cid(store, "zeta"), 1)
    got = [e.name for e in catalog.build_catalog(store, search="zeta files parse")]
    assert got == ["zeta parser"]  # the 1-match row misses the n=3 threshold of 2


def test_filtered_search_reaches_beyond_fifty(store):
    caps = [(f"widget {i:03d}", "widget thing", "high") for i in range(70)]
    seed(store, caps, "big", "repo")
    seed(store, [("widget rare", "widget thing", "low")], "small", "repo")
    es = catalog.build_catalog(store, search="widget", portability="low")
    assert [e.name for e in es] == ["widget rare"]
    es = catalog.build_catalog(store, search="widget", donor="small/repo")
    assert [e.name for e in es] == ["widget rare"]


def test_cli_summary_total_and_shown(store):
    seed(store, [(f"widget {i:03d}", "widget thing", "high") for i in range(70)], "big", "repo")
    args = SimpleNamespace(search="widget", portability="high", donor=None, fit=None,
                           novelty=None, needs_review=False, gaps=False)
    out = catalog.run_cli(store, args)
    assert out["summary"]["total"] == 70
    assert out["summary"]["shown"] == 50
    assert len(out["items"]) == 50
    assert out["text"].splitlines()[-1] == "… 20 more"  # 70 matched, 50 rows shown
    args.search = None
    out = catalog.run_cli(store, args)
    assert out["summary"]["total"] == out["summary"]["shown"] == 70


def test_gaps_ignore_stopword_matches_and_show_words(store):
    seed(store, [("and runner", "runs and runs and runs", "high"),
                 ("health pinger", "pings services and reports health", "high")])
    store.save_profile(Profile(name="d", intents=["the and of", "health pinging services"]))
    items = [g for g in catalog.gaps(store) if g.get("project") is None]
    assert items[0]["matches"] == []
    assert items[1]["matches"] == ["health pinger"]
    assert items[1]["matched"] == [{"name": "health pinger", "words": ["health", "services"]}]
    assert "health pinger (health, services)" in catalog._gaps_text(items)
