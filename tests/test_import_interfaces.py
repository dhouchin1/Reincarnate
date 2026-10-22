from pathlib import Path
from types import SimpleNamespace

from conftest import make_dossier
from reincarnate.importers import interfaces as imp

FIX = Path(__file__).parent / "fixtures" / "capability-interfaces.json"


def test_import_matches_and_reports(store):
    store.upsert_dossier("Acme", "Widget", "u", "s", make_dossier(
        ("Health Pinger", "pings"), ("icon library", "svgs"), ("untouched", "x")))
    store.conn.execute("INSERT INTO vocab_aliases VALUES ('data:fixture-alias', 'x:dup')")
    r = imp.import_interfaces(store, FIX)
    assert (r["items"], r["matched"]) == (3, 2)
    assert r["unmatched"] == ["40-Code/repos/ghost-repo.md::nothing here"]
    assert r["aliases_added"] == 1  # one already present, the null-drop alias is new
    caps = {c.name: c for c in store.list_capabilities()}
    assert caps["Health Pinger"].provides == ["data:service-status"]
    assert caps["Health Pinger"].requires == ["http:api-server", "store:service-registry"]
    assert caps["icon library"].provides == ["ui:icon-set"]
    assert caps["untouched"].provides == [] and caps["Health Pinger"].novelty == ""


def test_run_cli(store):
    assert "error" in imp.run_cli(store, SimpleNamespace(file="/nonexistent.json"))
    assert imp.run_cli(store, SimpleNamespace(file=str(FIX)))["matched"] == 0
