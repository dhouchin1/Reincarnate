"""License normalization, policy levels and the write paths that apply them."""

import json

import pytest

from reincarnate import comprehend, design, licenses
from reincarnate.models import Profile
from test_comprehend import DOSSIER, FakeStore, _excerpt_repo, _stub_claude, sandboxed  # noqa: F401
from test_design import _need_store

AGPL_NOTE = ("AGPL-3.0 (NOTE: package.json incorrectly states 'MIT'; the actual LICENSE file in "
             "the repo root is GNU AGPL v3. A clean-room reimplementation avoids this entirely.)")
FSL = ("FSL-1.1-MIT (Functional Source License v1.1, MIT Future License) — source-available, "
       "NOT OSI open source. Personal/internal use. Converts to plain MIT two years after each "
       "release.")
PROP1 = ("Proprietary dual-use — free for non-commercial only; commercial use requires paid "
         "license ($79–$1,999). Contact the author.")
PROP2 = ("Dual-use proprietary — free for non-commercial; paid commercial license required "
         "($29–$499). NOT MIT/Apache.")
PROP3 = ("Copyright (c) 2025 MS3PP. All rights reserved. (proprietary, no open-source license "
         "file present)")

NORMALIZE = [
    ("MIT", "MIT"), ("Apache-2.0", "Apache-2.0"), ("BSD-3-Clause", "BSD-3-Clause"),
    ("GPL-3.0", "GPL-3.0"), ("GPL-2.0", "GPL-2.0"), ("AGPL-3.0", "AGPL-3.0"),
    ("unknown", "unknown"),
    ("unknown (no LICENSE file present in the repository)", "unknown"),
    ("MIT OR Apache-2.0 (dual-licensed)", "MIT OR Apache-2.0"),
    ("BSD-3-Clause (declared in pyproject.toml; LICENSE.txt present)", "BSD-3-Clause"),
    (AGPL_NOTE, "AGPL-3.0"), (FSL, "FSL-1.1-MIT"),
    (PROP1, "proprietary"), (PROP2, "proprietary"), (PROP3, "proprietary"),
    ("GPL-3.0-or-later", "GPL-3.0-or-later"), ("GPL-3.0-only", "GPL-3.0-only"),
    ("LGPL-2.1+", "LGPL-2.1+"), ("Apache License 2.0", "Apache-2.0"),
    ("GNU Affero General Public License v3.0", "AGPL-3.0"),
    ("GNU Affero General Public License v3", "AGPL-3.0"),
    ("GNU General Public License v2", "GPL-2.0"),
    ("Mozilla Public License 2.0", "MPL-2.0"), ("ISC License", "ISC"),
    ("The Unlicense", "Unlicense"), ("BSD 3-Clause", "BSD-3-Clause"),
    ("mit", "MIT"), ("apache-2.0", "Apache-2.0"), ("agpl-3.0", "AGPL-3.0"),
    ("MIT License", "MIT"), ("MIT.", "MIT"), ("", "unknown"), (None, "unknown"),
    ("NOASSERTION", "unknown"), ("Other", "unknown"), ("None found", "unknown"),
    ("N/A", "unknown"), ("See the README for terms", "unknown"),
    ("BSD-2-Clause OR MIT", "BSD-2-Clause OR MIT"), ("mit or apache-2.0", "MIT OR Apache-2.0"),
    ("Apache-2.0 WITH LLVM-exception", "Apache-2.0 WITH LLVM-exception"),
    ("BUSL-1.1", "BUSL-1.1"), ("SSPL-1.0", "SSPL-1.0"), ("CC-BY-NC-4.0", "CC-BY-NC-4.0"),
    ("X" * 80, "unknown"),
]


@pytest.mark.parametrize("raw,expected", NORMALIZE)
def test_normalize(raw, expected):
    assert licenses.normalize(raw) == expected
    assert licenses.normalize(expected) == expected  # idempotent


def test_family():
    assert licenses.family("GPL-3.0-or-later") == "gpl-3.0"
    assert licenses.family("GPL-3.0-only") == "gpl-3.0"
    assert licenses.family("LGPL-2.1+") == "lgpl-2.1"
    assert licenses.family("MIT") == "mit"


POLICY = {"deny": ["AGPL-3.0"], "warn": ["unknown"]}


@pytest.mark.parametrize("raw", [AGPL_NOTE, "AGPL-3.0-only", "AGPL-3.0-or-later", "agpl-3.0",
                                 "AGPL-3.0+", "GNU Affero General Public License v3.0",
                                 "MIT AND AGPL-3.0"])
def test_level_denies_agpl_variants(raw):
    assert licenses.level(raw, POLICY) == "deny"


def test_level_policy_entries_are_normalized():
    assert licenses.level("AGPL-3.0", {"deny": ["agpl-3.0-only"]}) == "deny"
    assert licenses.level("GPL-3.0-or-later", {"deny": ["GPL-3.0"]}) == "deny"
    assert licenses.level("GPL-2.0", {"deny": ["GPL"]}) is None  # a bare name matches only itself
    assert licenses.level("GPL", {"deny": ["gpl"]}) == "warn"  # unrecognised text is unknown


def test_level_or_and_with():
    assert licenses.level("MIT OR AGPL-3.0", POLICY) is None  # the user picks MIT
    assert licenses.level("AGPL-3.0 OR GPL-3.0", {"deny": ["AGPL-3.0"], "warn": ["GPL-3.0"]}) \
        == "warn"
    assert licenses.level("MIT AND AGPL-3.0", POLICY) == "deny"
    assert licenses.level("AGPL-3.0 WITH Some-exception", POLICY) == "deny"


def test_level_unknown_proprietary_warn_mit_clear():
    assert licenses.level("unknown (no LICENSE file)", {"deny": []}) == "warn"
    assert licenses.level("", {}) == "warn"
    assert licenses.level(PROP2, {"deny": [], "warn": []}) == "warn"
    assert licenses.level("MIT", POLICY) is None
    assert licenses.level(FSL, POLICY) is None  # review, not policy


@pytest.mark.parametrize("raw", [FSL, PROP1, "GPL-3.0", "GPL-3.0-or-later", "unknown", "",
                                 "AGPL-3.0", "LGPL-2.1+", "MPL-2.0", "SSPL-1.0", "BUSL-1.1",
                                 "CC-BY-NC-4.0", "EUPL-1.2", "MIT AND GPL-3.0"])
def test_is_review(raw):
    assert licenses.is_review(raw)


@pytest.mark.parametrize("raw", ["MIT", "Apache-2.0", "BSD-3-Clause", "ISC", "MIT OR GPL-3.0",
                                 "BSD-3-Clause (declared in pyproject.toml)", "BSL-1.0"])
def test_not_review(raw):
    assert not licenses.is_review(raw)


# -- design -------------------------------------------------------------------------------
def test_design_flags_block_annotated_agpl():
    caps = [{"license": AGPL_NOTE, "donor": "a/b"}, {"license": "GPL-3.0-or-later", "donor": "c/d"},
            {"license": FSL, "donor": "e/f"}, {"license": "MIT", "donor": "g/h"}]
    prof = Profile(name="d", license_policy={"deny": ["AGPL-3.0"], "warn": []})
    assert design._license_flags(caps, prof) == [
        "License blocked: a/b (AGPL-3.0)",
        "License review: c/d (GPL-3.0-or-later), e/f (FSL-1.1-MIT)"]
    prof = Profile(name="d", license_policy={"deny": ["GPL-3.0"], "warn": []})
    assert design._license_flags(caps[1:2], prof) == [
        "License blocked: c/d (GPL-3.0-or-later)"]


def test_annotated_agpl_never_autofilled(store):
    p, _ids = _need_store(store, ("News reader", "reads rss news feed items", "me/reader"), [
        ("Feed parser", "parses rss news feed items into entries", "other/feeds", AGPL_NOTE)])
    assert p["auto_filled"] == []
    gap = next(g for g in p["gaps"] if g.get("token") == "data:feed")
    assert [c["name"] for c in gap["candidates"]] == ["Feed parser"]


# -- write paths --------------------------------------------------------------------------
def test_upsert_dossier_stores_normalized_id_and_keeps_note(store):
    data = {"summary": "s", "dependencies": {"license": AGPL_NOTE}, "capabilities": []}
    d = store.upsert_dossier("o", "r", "https://github.com/o/r", "s1", data)
    assert d.license == "AGPL-3.0"
    assert d.data["dependencies"]["license"] == "AGPL-3.0"
    assert d.data["dependencies"]["license_note"] == AGPL_NOTE
    # a clean id carries no note; a model-supplied note is not overwritten
    d = store.upsert_dossier("o", "q", "https://github.com/o/q", "s1", {
        "dependencies": {"license": "MIT"}, "capabilities": []})
    assert d.license == "MIT" and "license_note" not in d.data["dependencies"]
    d = store.upsert_dossier("o", "p", "https://github.com/o/p", "s1", {
        "dependencies": {"license": "MIT License", "license_note": "mine"}, "capabilities": []})
    assert d.license == "MIT" and d.data["dependencies"]["license_note"] == "mine"


def test_upsert_dossier_without_license_is_unknown(store):
    d = store.upsert_dossier("o", "r", "https://github.com/o/r", "s1", {"capabilities": []})
    assert d.license == "unknown"


def test_comprehend_stores_normalized_license_and_note(sandboxed, monkeypatch):  # noqa: F811
    _excerpt_repo(sandboxed)
    d = {**DOSSIER, "dependencies": {"license": AGPL_NOTE}}
    _stub_claude(monkeypatch, "```json\n" + json.dumps(d) + "\n```")
    store = FakeStore()
    comprehend.comprehend_url(store, "o/r")
    deps = store.upserts[0]["data"]["dependencies"]
    assert deps["license"] == "AGPL-3.0" and deps["license_note"] == AGPL_NOTE


def test_render_shows_license_note(store):
    from reincarnate import render
    d = store.upsert_dossier("o", "r", "https://github.com/o/r", "s1", {
        "dependencies": {"license": AGPL_NOTE}, "capabilities": []})
    md = render.render_dossier_markdown(d)
    assert "license: AGPL-3.0\n" in md and f"- **License note:** {AGPL_NOTE}" in md


def test_cli_deny_license_stores_normalized(capsys, home):
    from reincarnate.cli import main
    assert main(["profile", "set", "--name", "d", "--deny-license", "agpl-3.0-only",
                 "--deny-license", "GNU Affero General Public License v3"]) == 0
    capsys.readouterr()
    main(["profile", "show", "--json"])
    assert json.loads(capsys.readouterr().out)["license_policy"]["deny"] == [
        "AGPL-3.0-only", "AGPL-3.0"]
