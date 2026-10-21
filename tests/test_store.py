from conftest import make_dossier
from reincarnate.models import Composition, Profile, Project

URL = "https://github.com/o/r"


def test_upsert_and_get_by_all_ref_forms(store):
    d = store.upsert_dossier("o", "r", URL, "abc123", make_dossier(("Parser", "parses things")))
    assert d.donor == "o/r" and d.license == "MIT" and len(d.capabilities) == 1
    assert store.get_dossier(d.id).sha == "abc123"
    assert store.get_dossier("o/r").id == d.id
    assert store.get_dossier("o/r@abc123").id == d.id
    assert store.get_dossier("o/r@nope") is None
    assert store.get_dossier(999) is None


def test_upsert_same_sha_replaces_capabilities(store):
    store.upsert_dossier("o", "r", URL, "a", make_dossier(("One", "x"), ("Two", "y")))
    store.upsert_dossier("o", "r", URL, "a", make_dossier(("Three", "z")))
    caps = store.list_capabilities()
    assert [c.name for c in caps] == ["Three"]
    assert store.search_capabilities("One") == []


def test_current_only_across_shas(store):
    old = store.upsert_dossier("o", "r", URL, "old", make_dossier(("Old", "x")))
    store.conn.execute("UPDATE dossiers SET comprehended_at='2020-01-01T00:00:00Z' WHERE id=?",
                       (old.id,))
    new = store.upsert_dossier("o", "r", URL, "new", make_dossier(("New", "y")))
    assert [c.name for c in store.list_capabilities()] == ["New"]
    assert {c.name for c in store.list_capabilities(current_only=False)} == {"Old", "New"}
    assert [c.name for c in store.list_capabilities(dossier_id=old.id)] == ["Old"]
    assert store.get_dossier("o/r").id == new.id
    assert [d.sha for d in store.list_dossiers()] == ["new"]
    assert len(store.list_dossiers(current_only=False)) == 2


def test_capability_carries_donor_and_license(store):
    store.upsert_dossier("o", "r", URL, "a", make_dossier(("Parser", "x")))
    c = store.list_capabilities()[0]
    assert (c.donor, c.license) == ("o/r", "MIT")
    assert store.get_capability(c.id).files == ["a.py"]


def test_fts_search_and_delete_sync(store):
    d = store.upsert_dossier("o", "r", URL, "a", make_dossier(
        ("Rate limiter", "token bucket throttling"), ("Parser", "markdown parsing")))
    assert [c.name for c in store.search_capabilities("throttl")] == ["Rate limiter"]
    assert store.search_capabilities("zzzz") == []
    assert store.delete_dossier(d.id)
    assert store.search_capabilities("throttling") == []
    assert store.list_capabilities(current_only=False) == []


def test_fits_seeded_only_for_existing_projects(store):
    store.add_project(Project(slug="mine", path="/x"))
    fit = [{"project": "mine", "capability": "parser", "how": "swap in"},
           {"project": "ghost", "capability": "Parser", "how": "n/a"},
           {"project": "mine", "capability": "missing", "how": "n/a"}]
    store.upsert_dossier("o", "r", URL, "a", make_dossier(("Parser", "x"), fit=fit))
    fits = store.list_fits()
    assert len(fits) == 1
    f = fits[0]
    assert (f.project_slug, f.score, f.source, f.how) == ("mine", 0.8, "dossier", "swap in")
    assert store.list_fits(project_slug="ghost") == []


def test_votes(store):
    store.upsert_dossier("o", "r", URL, "a", make_dossier(("Parser", "x")))
    cid = store.list_capabilities()[0].id
    store.set_vote(cid, 1)
    assert store.get_votes() == {cid: 1}
    store.set_vote(cid, -1)
    assert store.get_votes() == {cid: -1}
    store.set_vote(cid, 0)
    assert store.get_votes() == {}


def test_profile_roundtrip(store):
    assert store.get_profile() is None
    p = store.save_profile(Profile(name="Dan", stack=["python"], intents=["ship"],
                                   license_policy={"deny": ["GPL-3.0"], "warn": []}))
    got = store.get_profile()
    assert got.to_dict() == p.to_dict()
    assert got.license_policy["deny"] == ["GPL-3.0"] and got.created and got.updated


def test_projects_and_compositions(store):
    store.add_project(Project(slug="a", stack=["go"]))
    assert store.get_project("a").stack == ["go"]
    assert store.remove_project("a") and not store.remove_project("a")
    c = store.save_composition(Composition(slug="x", name="X", proposal={"glue": "g"}))
    c2 = store.save_composition(Composition(slug="x", name="X2", status="emitted"))
    assert c.id == c2.id and store.get_composition("x").name == "X2"
    assert store.get_composition(c.id).status == "emitted"
    assert len(store.list_compositions()) == 1
    assert store.add_kit(c.id, "/tmp/k") > 0


def test_explicit_db_path(tmp_path):
    from reincarnate.store import Store
    s = Store(tmp_path / "sub" / "x.db")
    assert (tmp_path / "sub" / "x.db").exists()
    s.close()
