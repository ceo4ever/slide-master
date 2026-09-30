#!/usr/bin/env python3
"""
PPT Master - SVG to PDF Export

Renders the project's self-contained svg_final/ pages into one vector PDF with
Playwright (Chromium), one page per slide at the canvas size. Text stays
selectable and fonts are embedded by Chromium. svg_final/ is refreshed through
finalize_svg.py first when it is missing or older than svg_output/, so the PDF
always reflects the authored SVG pages.

The PDF is a visual export of the page design only: transitions, animations,
speaker notes, and narration are not carried over. Edits made in PowerPoint
after PPTX export are not reflected either (the source is svg_output/).

Usage:
    python3 scripts/svg_to_pdf.py <project_path> [-o output.pdf]

Examples:
    python3 scripts/svg_to_pdf.py projects/my_project
    python3 scripts/svg_to_pdf.py projects/my_project -o /tmp/deck.pdf

Output (default flow):
    exports/<title>_ver<N>.pdf, where N matches the latest exported PPTX
    version (1 when no PPTX exists yet), so the PDF pairs with that deck.

Dependencies:
    playwright (+ chromium: python3 -m playwright install chromium)
"""

from __future__ import annotations

import re
import sys
import argparse
import tempfile
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from console_encoding import configure_utf8_stdio  # noqa: E402
from project_utils import get_project_info  # noqa: E402

configure_utf8_stdio()

_VIEWBOX_RE = re.compile(r'viewBox="\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)\s*"')
_PPTX_VERSION_RE_TEMPLATE = r"^{title}_ver(\d+)(?:_[a-z_]+)?\.pptx$"
FINALIZE_OPTIONS = {"embed_icons": True, "align_images": True, "flatten_text": True}


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _project_title(project_path: Path) -> str:
    try:
        return get_project_info(str(project_path)).get("name", project_path.name)
    except (OSError, ValueError, KeyError):
        return project_path.name


def default_output_path(project_path: Path, title: str) -> Path:
    """Pair the PDF with the latest `<title>_ver<N>` PPTX in exports/."""
    exports_dir = project_path / "exports"
    pattern = re.compile(_PPTX_VERSION_RE_TEMPLATE.format(title=re.escape(title)), re.IGNORECASE)
    latest = 0
    if exports_dir.is_dir():
        for entry in exports_dir.iterdir():
            match = pattern.match(entry.name)
            if match:
                latest = max(latest, int(match.group(1)))
    return exports_dir / f"{title}_ver{max(latest, 1)}.pdf"


def final_is_stale(project_path: Path) -> bool:
    """True when svg_final/ is missing, has a different page set, or is older than svg_output/."""
    output_pages = {p.name: p for p in (project_path / "svg_output").glob("*.svg")}
    final_dir = project_path / "svg_final"
    final_pages = {p.name: p for p in final_dir.glob("*.svg")} if final_dir.is_dir() else {}
    if set(output_pages) != set(final_pages):
        return True
    return any(final_pages[name].stat().st_mtime < src.stat().st_mtime for name, src in output_pages.items())


def canvas_size(pages: list[Path]) -> tuple[float, float]:
    """Return the shared viewBox width/height; a mixed-size deck is an error."""
    sizes = set()
    for page in pages:
        match = _VIEWBOX_RE.search(page.read_text(encoding="utf-8")[:4000])
        if not match:
            raise ValueError(f"{page.name}: root viewBox not found")
        sizes.add((float(match.group(1)), float(match.group(2))))
    if len(sizes) != 1:
        raise ValueError(f"pages use different canvas sizes: {sorted(sizes)}")
    return sizes.pop()


def build_html(pages: list[Path], width: float, height: float) -> str:
    # Each page is an isolated <img> document, so element ids (markers,
    # gradients, filters) can never collide across slides.
    rows = "".join(
        f'<div class="page"><img src="{page.resolve().as_uri()}" '
        f'width="{width:g}" height="{height:g}"></div>'
        for page in pages
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"@page{{size:{width:g}px {height:g}px;margin:0}}"
        "html,body{margin:0;padding:0}"
        f".page{{width:{width:g}px;height:{height:g}px;overflow:hidden;break-after:page}}"
        ".page:last-child{break-after:auto}"
        ".page img{display:block}"
        f"</style></head><body>{rows}</body></html>"
    )


def render_pdf(html: str, output_path: Path, width: float, height: float) -> None:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(prefix="svg_to_pdf_") as tmp:
        html_path = Path(tmp) / "deck.html"
        html_path.write_text(html, encoding="utf-8")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": int(width), "height": int(height)})
                page.goto(html_path.as_uri())
                page.wait_for_load_state("networkidle")
                page.evaluate("document.fonts.ready")
                page.pdf(
                    path=str(output_path),
                    width=f"{width:g}px",
                    height=f"{height:g}px",
                    print_background=True,
                    prefer_css_page_size=True,
                )
            finally:
                browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a project's SVG pages to one vector PDF (Playwright).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("project_path", type=Path, help="Project directory")
    parser.add_argument("-o", "--output", type=Path, help="Output PDF path (default: exports/<title>_ver<N>.pdf)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    project_path = args.project_path.resolve()
    if not (project_path / "svg_output").is_dir():
        _log(f"Error: svg_output/ not found in {project_path}")
        return 1
    if not list((project_path / "svg_output").glob("*.svg")):
        _log("Error: svg_output/ has no SVG pages to export")
        return 1

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        _log("Error: playwright is not installed. Fix:\n"
             "    python3 -m pip install playwright\n"
             "    python3 -m playwright install chromium")
        return 1

    if final_is_stale(project_path):
        _log("[svg_to_pdf] svg_final/ missing or stale — running finalize_svg.py first")
        from finalize_svg import finalize_project

        if not finalize_project(project_path, dict(FINALIZE_OPTIONS), quiet=True):
            _log("Error: finalize_svg.py failed; fix svg_output/ and retry")
            return 1

    pages = sorted((project_path / "svg_final").glob("*.svg"))
    try:
        width, height = canvas_size(pages)
    except ValueError as exc:
        _log(f"Error: {exc}")
        return 1

    output_path = args.output or default_output_path(project_path, _project_title(project_path))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        _log(f"[svg_to_pdf] overwriting existing {output_path.name}")

    try:
        render_pdf(build_html(pages, width, height), output_path, width, height)
    except Exception as exc:  # noqa: BLE001 — browser launch / render failure
        _log(f"Error: PDF render failed: {exc}\n"
             "If Chromium is missing, run:  python3 -m playwright install chromium")
        return 1

    _log(f"[svg_to_pdf] {len(pages)} page(s), {width:g}x{height:g} -> {output_path}")
    print(output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
