#!/usr/bin/env python3
"""Validate the public CityNexus portfolio, not the original application's runtime.

Checks local page assets, in-repository Markdown targets and the source-linked
static visual viewer. Does NOT test API providers, AI, physical arrival or a
merged runnable product.
"""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
CHECKED_MARKDOWN = [
    "README.md",
    "README.zh-CN.md",
    "docs/PROJECT_PORTAL.md",
    "docs/CLASSROOM_AND_V3.md",
    "docs/DEVELOPMENT_STREAMS.md",
    "docs/DEVELOPMENT_TIMELINE.md",
    "docs/FEATURE_ATLAS.md",
    "docs/EXPERIMENT_RESULTS.md",
    "docs/RESEARCH_ARCHIVE_INDEX.md",
    "docs/DEMO_STORYBOARD.md",
    "docs/FEATURE_VERIFICATION_MATRIX.md",
    "docs/SITE_DEPLOYMENT.md",
    "docs/PUBLICATION_AND_PROVENANCE.md",
    "docs/VISUAL_GALLERY.md",
    "screenshots/README.md",
]
REQUIRED_ASSETS = [
    "v3-first-trip-goal-mobile.png",
    "v3-agent-tool-timeline-mobile.png",
    "v3-active-journey-mobile.png",
    "v3-real-map-mobile.png",
    "v3-classroom-simulation-control.png",
    "v3-classroom-approved-change.png",
    "discovery-desktop.png",
    "copilot-mobile.png",
    "mobility-mobile.png",
    "system-overview.png",
    "agent-user-flow.png",
    "gantt-english.png",
]
errors: list[str] = []
count = 0


def fail(message: str) -> None:
    errors.append(message)


def local_ref(from_file: Path, link: str) -> None:
    global count
    url = link.strip()
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or url.startswith(("#", "mailto:", "data:")):
        return
    if not parsed.path:
        return
    target = unquote(parsed.path)
    # Relative markdown links are intentional. Absolute links from site/Pages
    # should be ordinary HTTPS URLs, not local file paths.
    if target.startswith("/"):
        fail(f"{from_file.relative_to(ROOT)}: rooted path {target!r}")
        return
    result = (from_file.parent / target).resolve()
    if not result.is_relative_to(ROOT):
        fail(f"{from_file.relative_to(ROOT)}: path escapes repository: {target!r}")
        return
    if not result.exists():
        fail(f"{from_file.relative_to(ROOT)}: target missing: {target!r}")
    count += 1


class SiteParser(HTMLParser):
    def __init__(self, base: Path) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.inline_scripts = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"img", "script", "link"} and values.get("src"):
            local_ref(self.base, values["src"] or "")
        if tag in {"link"} and values.get("href"):
            local_ref(self.base, values["href"] or "")
        if tag == "script":
            if values.get("src", "").startswith(("https:", "http:")):
                fail("docs/index.html must not depend on remote scripts")
            self.inline_scripts += 1


def main() -> int:
    site = ROOT / "docs/index.html"
    if not site.is_file():
        fail("docs/index.html is missing")
    else:
        html = site.read_text(encoding="utf-8")
        parsed = SiteParser(site)
        parsed.feed(html)
        for marker in ('id="chapters"', 'id="stageImage"', 'id="nextStage"', 'id="prevStage"', "evidence-led", "stages=["):
            if marker.lower() not in html.lower():
                fail(f"docs/index.html missing interactive-viewer element: {marker}")

    for name in REQUIRED_ASSETS:
        asset = ROOT / "docs/assets" / name
        if not asset.is_file():
            fail(f"Missing visual asset: {asset.relative_to(ROOT)}")
        elif asset.stat().st_size < 1000:
            fail(f"Unexpectedly small image asset: {asset.relative_to(ROOT)}")

    link_pattern = re.compile(r"!?\\[[^\\]]*\\]\\(([^)]+)\\)")
    img_pattern = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
    for name in CHECKED_MARKDOWN:
        source = ROOT / name
        if not source.is_file():
            fail(f"Missing reader-facing document: {name}")
            continue
        content = source.read_text(encoding="utf-8")
        for match in link_pattern.finditer(content):
            local_ref(source, match.group(1))
        for match in img_pattern.finditer(content):
            local_ref(source, match.group(1))

    print(f"Portfolio-only static QA: {len(CHECKED_MARKDOWN)} docs, {len(REQUIRED_ASSETS)} images, {count} local references")
    if errors:
        for e in errors:
            print("FAIL:", e, file=sys.stderr)
        print(f"{len(errors)} problem(s). No statement is made about the main application.", file=sys.stderr)
        return 1

    print("PASS: local portfolio documents and gallery assets are present.")
    print("This does NOT test full-stack build, external Agent, providers, GPS or classroom demo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
