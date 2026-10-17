"""Capture the README screenshots with Playwright.

Usage: python scripts/screenshots.py http://127.0.0.1:8000 docs/screenshots [--limit-url URL]

Needs `pip install playwright && playwright install chromium`. The server should
be running (AI_PROVIDER=fake works for layout shots, but use Gemini for the real ones).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def shoot(page: Page, out: Path, name: str, selector: str | None = None) -> None:
    target = page.locator(selector) if selector else page
    target.screenshot(path=str(out / f"{name}.png"))
    print("saved", out / f"{name}.png")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("out", type=Path)
    parser.add_argument("--limit-url", help="A server whose daily limit is used up")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for scheme in ("light", "dark"):
            page = browser.new_page(viewport={"width": 1280, "height": 900},
                                    device_scale_factor=2, color_scheme=scheme)
            page.goto(args.url)
            page.wait_for_selector("#limits:not(:has-text('Loading'))")
            if scheme == "light":
                shoot(page, args.out, "upload")
            page.click("#sample-btn")
            page.wait_for_selector("#results:not([hidden])", timeout=180_000)
            page.wait_for_timeout(800)  # Let the smooth scroll finish.
            page.evaluate("window.scrollTo(0, 0)")
            suffix = "" if scheme == "light" else "-dark"
            shoot(page, args.out, f"results{suffix}", "#results")
            if scheme == "light":
                # Hover a topic bar to show the tooltip.
                box = page.locator("#topic-chart .hit").first.bounding_box()
                assert box is not None
                page.locator("#topic-chart").scroll_into_view_if_needed()
                box = page.locator("#topic-chart .hit").first.bounding_box()
                assert box is not None
                page.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
                page.wait_for_timeout(200)
                shoot(page, args.out, "charts-tooltip", ".grid-2")
                page.click(".chip[data-filter='negative']")
                page.locator("#reviews-body details").first.click()
                shoot(page, args.out, "table", ".table-wrap >> xpath=..")
            page.close()

        if args.limit_url:
            page = browser.new_page(viewport={"width": 1280, "height": 620}, device_scale_factor=2)
            page.goto(args.limit_url)
            page.wait_for_selector("#disabled-reason:not([hidden])")
            shoot(page, args.out, "daily-limit", "#upload-card")
        browser.close()


if __name__ == "__main__":
    main()
