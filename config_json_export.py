"""Export JSON configuration using a pre-built target list.

Reads a discovery manifest (config_json_targets.json by default) and visits
each config URL to extract the JSON editor content. Output files are named
using the page ID from the URL.
"""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from playwright.sync_api import (  # type: ignore[import-not-found]
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeoutError,
    Page,
    sync_playwright,
)

from config_json_scraper import (
    DEFAULT_OUTPUT_DIR,
    EDITOR_SELECTORS,
    build_output_filename,
    extract_text_from_editor,
    locate_config_editor,
    normalize_json_text,
    open_advanced_tab,
    perform_login,
    resolve_headless,
    wait_for_page_ready,
    save_debug_artifacts,
)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export JSON configs using a discovery manifest."
    )
    parser.add_argument(
        "--targets-file",
        default="config_json_targets.json",
        help="Manifest file produced by config_json_discover.py.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR}).",
    )
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--editor-timeout", type=float, default=10.0)
    parser.add_argument("--post-login-wait", type=float, default=12.0)
    parser.add_argument("--page-wait", type=float, default=3.0)
    parser.add_argument("--slow-page-wait", type=float, default=12.0)
    parser.add_argument(
        "--slow-page-hint",
        action="append",
        default=["workflow_config"],
        help="URL substring that marks a page as slow.",
    )
    parser.add_argument("--max-configs", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip configs that already exist in the output directory.",
    )
    parser.add_argument("--username")
    parser.add_argument("--password")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.headless and args.headed:
        parser.error("--headless and --headed cannot be used together")
    return args


def prompt_for_credentials(args: argparse.Namespace) -> Tuple[str, str]:
    import os

    username = (args.username or os.environ.get("EIGHTFOLD_USERNAME") or "").strip()
    password = args.password or os.environ.get("EIGHTFOLD_PASSWORD")
    if not username:
        username = input("Eightfold username: ").strip()
    if not password:
        password = getpass.getpass("Eightfold password: ")
    return username, password


def load_targets(path: Path) -> List[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("entries", [])


def find_editor_text(page: Page, timeout: float) -> Optional[str]:
    try:
        page.wait_for_selector(", ".join(EDITOR_SELECTORS), timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        return None
    editor = locate_config_editor(page)
    if editor is None:
        return None
    return extract_text_from_editor(page, editor)


def export_config_from_page(
    page: Page,
    timeout: float,
    editor_timeout: float,
    debug_dir: Path,
    enable_debug: bool,
) -> Optional[str]:
    # Prefer Advanced tab config if available.
    open_advanced_tab(page, timeout)
    text = find_editor_text(page, editor_timeout)
    if not text:
        if enable_debug:
            save_debug_artifacts(page, debug_dir, page.url, "editor_not_found")
        return None
    normalized = normalize_json_text(text)
    if normalized is None:
        return None
    return normalized


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    username, password = prompt_for_credentials(args)
    headless = resolve_headless(args)

    targets_path = Path(args.targets_file)
    if not targets_path.is_absolute():
        targets_path = Path(__file__).resolve().parent / targets_path
    targets = load_targets(targets_path)
    if not targets:
        print(f"No targets found in {targets_path}")
        return 1

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = output_dir / "debug"
    slow_hints = tuple(item.lower() for item in args.slow_page_hint)

    exported = 0
    skipped: List[str] = []

    with sync_playwright() as playwright:
        browser = None
        context = None
        page = None

        def launch_and_login(start_url: str) -> Page:
            nonlocal browser, context, page
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            browser = playwright.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()
            page.goto(start_url)
            perform_login(page, username, password, timeout=args.timeout)
            wait_for_page_ready(page, args.timeout, args.post_login_wait)
            return page

        first_url = targets[0].get("config_url") or targets[0].get("url")
        if not first_url:
            print("Invalid targets file: missing URLs.")
            return 1

        page = launch_and_login(first_url)

        relaunch_attempts = 0
        for entry in targets:
            url = entry.get("config_url") or entry.get("url")
            if not url:
                continue

            try:
                page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                skipped.append(f"{url} -> navigation_timeout")
                continue
            except PlaywrightError:
                relaunch_attempts += 1
                if relaunch_attempts > 3:
                    skipped.append(f"{url} -> browser_crashed")
                    break
                page = launch_and_login(first_url)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except Exception:
                    skipped.append(f"{url} -> navigation_failed")
                    continue

            try:
                extra_wait = args.page_wait
                if any(hint in page.url.lower() for hint in slow_hints):
                    extra_wait = max(extra_wait, args.slow_page_wait)
                wait_for_page_ready(page, args.timeout, extra_wait)
            except PlaywrightError:
                relaunch_attempts += 1
                if relaunch_attempts > 3:
                    skipped.append(f"{url} -> browser_crashed")
                    break
                page = launch_and_login(first_url)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    extra_wait = args.page_wait
                    if any(hint in page.url.lower() for hint in slow_hints):
                        extra_wait = max(extra_wait, args.slow_page_wait)
                    wait_for_page_ready(page, args.timeout, extra_wait)
                except Exception:
                    skipped.append(f"{url} -> navigation_failed")
                    continue

            config_text = export_config_from_page(
                page, args.timeout, args.editor_timeout, debug_dir, args.debug
            )
            if not config_text:
                skipped.append(f"{page.url} -> no_editor")
                continue

            filename = build_output_filename(page.url)
            destination = output_dir / filename
            if args.resume and destination.exists():
                skipped.append(f"{page.url} -> already_exported")
                continue
            destination.write_text(config_text, encoding="utf-8")
            exported += 1
            print(f"Saved: {destination}")

            if args.max_configs and exported >= args.max_configs:
                break

        if browser:
            try:
                browser.close()
            except Exception:
                pass

    print(f"Exported {exported} configs.")
    if skipped:
        print("Skipped:")
        for entry in skipped:
            print(f"  {entry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
