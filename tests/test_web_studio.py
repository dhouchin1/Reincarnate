"""Static checks on the studio page (src/reincarnate/web/studio.html)."""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "reincarnate" / "web"
PAGE = WEB / "studio.html"
INGEST = WEB / "ingest.html"

ENDPOINTS = [
    "/api/catalog",
    "/api/fit",
    "/api/estimate/design",
    "/api/designs",
    "/pick",
    "/api/kits/preview",
    "/api/kits",
    "/api/jobs",
    "/retry",
]


def _text() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_page_exists():
    assert PAGE.is_file()
    assert "<title>" in _text()


def test_no_html_injection_sinks():
    text = _text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in text, sink


def test_no_external_scripts():
    for src in re.findall(r"<script[^>]*\ssrc\s*=\s*[\"']?([^\"'\s>]+)", _text(), re.I):
        assert not re.match(r"(https?:)?//", src), src
    # the only external host is the Google Fonts stylesheet
    hosts = set(re.findall(r"(?:href|src)\s*=\s*[\"']https?://([\w.-]+)", _text()))
    assert hosts <= {"fonts.googleapis.com"}, hosts


def test_references_every_contract_endpoint():
    text = _text()
    for ep in ENDPOINTS:
        assert ep in text, ep


def test_token_handling():
    text = _text()
    assert "X-Reincarnate-Token" in text
    assert "history.replaceState" in text
    assert "application/json" in text
    assert "hashchange" in text
    assert "sessionStorage" in text


def test_has_the_three_routes():
    text = _text()
    for route in ("/design/", "/kit/"):
        assert route in text, route


def test_demo_mode_marker():
    text = _text()
    assert "#demo" in text
    assert re.search(r"DEMO\s*=", text)


def test_ingest_links_to_studio():
    text = INGEST.read_text(encoding="utf-8")
    assert 'href="/studio"' in text
    assert "coming in P2" not in text
    assert "sessionStorage" in text
