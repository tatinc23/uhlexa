"""The web simulator is plain static files: they must exist, stay self-contained and never build DOM from markup."""

import re
from pathlib import Path

SIMULATOR = Path(__file__).resolve().parents[2] / "web" / "simulator"
FILES = ("index.html", "app.js", "styles.css")


def _read(name: str) -> str:
    return (SIMULATOR / name).read_text(encoding="utf-8")


def test_simulator_files_exist():
    for name in FILES:
        assert (SIMULATOR / name).is_file(), f"web/simulator/{name} is missing"


def test_index_references_only_relative_app_and_styles():
    html = _read("index.html")
    refs = re.findall(r"""(?:href|src)\s*=\s*["']([^"']*)["']""", html)
    refs = [r for r in refs if not r.startswith("#")]  # in-page anchors are fine
    assert sorted(refs) == ["app.js", "styles.css"], refs
    assert "<style" not in html.lower()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, flags=re.IGNORECASE), "inline scripts are not allowed"


def test_no_absolute_urls_in_any_simulator_file():
    for name in FILES:
        text = _read(name)
        assert "http://" not in text, f"{name} contains an http:// URL"
        assert "https://" not in text, f"{name} contains an https:// URL"
        assert "//cdn" not in text and "@import" not in text, f"{name} pulls in an external resource"


def test_app_js_never_assigns_markup():
    js = _read("app.js")
    assert not re.search(r"\.innerHTML\s*[+\-]?=", js), "innerHTML assignment found"
    assert "insertAdjacentHTML" not in js
    assert not re.search(r"\.outerHTML\s*[+\-]?=", js), "outerHTML assignment found"
    assert "document.write" not in js
    assert "eval(" not in js and "new Function" not in js


def test_app_js_uses_only_documented_routes():
    js = _read("app.js")
    routes = set(re.findall(r"""['"](/(?:api|demo)/[a-z\-/]*)""", js))
    allowed = {
        "/demo/users",
        "/demo/login",
        "/demo/reset",
        "/api/board",
        "/api/providers",
        "/api/conversations",
        "/api/conversations/",
        "/api/conversations/messages",
        "/api/activity",
        "/api/approvals/",
        "/api/browser-tasks",
        "/api/browser-tasks/",
    }
    assert routes <= allowed, routes - allowed


def test_demo_sentence_chip_is_exact():
    assert "Ask Uhlexa to add milk and eggs to Sunday groceries." in _read("app.js")
