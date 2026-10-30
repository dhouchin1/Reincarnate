"""Tests for reincarnate.importers.vault (second-brain dossier note import)."""

from __future__ import annotations

import argparse
from pathlib import Path

from reincarnate.importers import vault

FIXTURES = Path(__file__).parent / "fixtures" / "vault"
HOMARR = FIXTURES / "ajnart-homarr.md"
NOT_A_DOSSIER = FIXTURES / "2026-01-01-not-a-dossier.md"


class FakeStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def find_dossier(self, owner, repo, sha):
        return None

    def upsert_dossier(self, owner, repo, url, sha, data, *, model="", cost_usd=0.0,
                       source="claude-p"):
        self.calls.append({
            "owner": owner, "repo": repo, "url": url, "sha": sha, "data": data,
            "model": model, "cost_usd": cost_usd, "source": source,
        })


def _broken_dossier() -> str:
    # Same shape as a real dossier, but with an invalid JSON escape so parsing fails.
    return (
        "---\ntype: repo-dossier\nowner: acme\nrepo: broken\n"
        "url: \"https://github.com/acme/broken\"\n---\n# acme/broken\n\n"
        "```json\n{\"summary\": \"x\", \"capabilities\": [{\"name\": \"bad \\q\"}]}\n```\n"
    )


def test_parse_frontmatter_scalars_quotes_and_inline_lists():
    text = (
        "---\n"
        "title: ajnart/homarr\n"
        'url: "https://github.com/ajnart/homarr"\n'
        "tags: [captured, github, \"quoted, item\"]\n"
        "empty: []\n"
        "comprehend_cost_usd: 1.1336\n"
        "comprehended: true\n"
        "origin:\n"
        "  source: github\n"
        "  extra:\n"
        "    owner: ajnart\n"
        "---\n"
        "# body\n"
    )
    fm, body = vault.parse_frontmatter(text)
    assert fm["title"] == "ajnart/homarr"
    assert fm["url"] == "https://github.com/ajnart/homarr"
    assert fm["tags"] == ["captured", "github", "quoted, item"]
    assert fm["empty"] == []
    assert fm["comprehend_cost_usd"] == "1.1336"
    assert fm["comprehended"] is True
    assert fm["origin"] == ""  # nested block ignored, not parsed
    assert "source" not in fm and "owner" not in fm
    assert body == "# body\n"


def test_parse_frontmatter_absent_returns_whole_text():
    text = "# no frontmatter here\n"
    assert vault.parse_frontmatter(text) == ({}, text)


def test_parse_frontmatter_unterminated_returns_whole_text():
    text = "---\ntitle: oops\n# never closed\n"
    assert vault.parse_frontmatter(text) == ({}, text)


def test_parse_dossier_fixture_fields():
    note = vault.parse_dossier_note(HOMARR)
    assert note is not None
    assert note["owner"] == "ajnart"
    assert note["repo"] == "homarr"
    assert note["url"] == "https://github.com/ajnart/homarr"
    assert note["sha"] == "unknown"  # no sha key in real notes
    assert note["model"] == "claude-sonnet-4-6"
    assert note["cost_usd"] == 1.1336
    assert note["comprehended_at"] == "2026-07-12"
    assert len(note["data"]["capabilities"]) == 8
    assert note["data"]["summary"].startswith("Homarr (ajnart/homarr")


def test_non_dossier_returns_none():
    assert vault.parse_dossier_note(NOT_A_DOSSIER) is None


def test_import_dir_fixtures_counts_and_upserts():
    store = FakeStore()
    result = vault.import_dir(store, FIXTURES)
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["errors"] == []
    assert result["donors"] == ["ajnart/homarr"]
    assert len(store.calls) == 1
    call = store.calls[0]
    assert (call["owner"], call["repo"], call["sha"]) == ("ajnart", "homarr", "unknown")
    assert call["model"] == "claude-sonnet-4-6"
    assert call["cost_usd"] == 1.1336
    assert call["source"] == "vault-import"
    assert len(call["data"]["capabilities"]) == 8


def test_import_dir_bad_note_is_error_not_exception(tmp_path):
    (tmp_path / "ajnart-homarr.md").write_text(HOMARR.read_text(encoding="utf-8"))
    (tmp_path / "acme-broken.md").write_text(_broken_dossier())
    store = FakeStore()
    result = vault.import_dir(store, tmp_path)
    assert result["imported"] == 1
    assert result["skipped"] == 0
    assert len(result["errors"]) == 1
    assert result["errors"][0]["file"] == "acme-broken.md"
    assert "invalid dossier JSON" in result["errors"][0]["reason"]
    assert [c["owner"] for c in store.calls] == ["ajnart"]


def test_import_dir_missing_directory_reports_error(tmp_path):
    store = FakeStore()
    result = vault.import_dir(store, tmp_path / "does-not-exist")
    assert result["imported"] == 0
    assert result["errors"] and result["errors"][0]["reason"] == "not a directory"
    assert store.calls == []


def test_import_dir_store_failure_is_counted_in_errors(tmp_path):
    (tmp_path / "ajnart-homarr.md").write_text(HOMARR.read_text(encoding="utf-8"))

    class FailingStore:
        def find_dossier(self, owner, repo, sha):
            return None

        def upsert_dossier(self, *args, **kwargs):
            raise RuntimeError("db locked")

    result = vault.import_dir(FailingStore(), tmp_path)
    assert result["imported"] == 0
    assert result["errors"] == [{"file": "ajnart-homarr.md", "reason": "store: db locked"}]


def test_owner_repo_fallback_prefers_url_over_filename(tmp_path):
    # Filename stem is ambiguous ("homarr-labs-homarr" -> owner "homarr"); the URL wins.
    body = "```json\n{\"summary\": \"s\", \"capabilities\": [{\"name\": \"c\"}]}\n```\n"
    (tmp_path / "homarr-labs-homarr.md").write_text(
        "---\ntype: repo-dossier\nurl: \"https://github.com/homarr-labs/homarr\"\n---\n" + body
    )
    note = vault.parse_dossier_note(tmp_path / "homarr-labs-homarr.md")
    assert (note["owner"], note["repo"]) == ("homarr-labs", "homarr")


def test_owner_repo_fallback_to_filename_stem_when_no_url(tmp_path):
    body = "```json\n{\"summary\": \"s\", \"capabilities\": [{\"name\": \"c\"}]}\n```\n"
    (tmp_path / "foo-bar.md").write_text("---\ntype: repo-dossier\n---\n" + body)
    note = vault.parse_dossier_note(tmp_path / "foo-bar.md")
    assert (note["owner"], note["repo"]) == ("foo", "bar")
    assert note["url"] == "https://github.com/foo/bar"
    assert note["sha"] == "unknown"


def test_sha_is_read_when_present(tmp_path):
    body = "```json\n{\"summary\": \"s\", \"capabilities\": [{\"name\": \"c\"}]}\n```\n"
    (tmp_path / "a-b.md").write_text(
        "---\nowner: a\nrepo: b\nsha: abc123\n---\n" + body
    )
    assert vault.parse_dossier_note(tmp_path / "a-b.md")["sha"] == "abc123"


def test_run_cli_uses_args_dir():
    store = FakeStore()
    result = vault.run_cli(store, argparse.Namespace(dir=str(FIXTURES)))
    assert result["imported"] == 1
    assert result["skipped"] == 1
    assert result["donors"] == ["ajnart/homarr"]
