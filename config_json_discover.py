"""Discover admin pages with JSON configuration.

This script logs into an Eightfold tenant, expands the admin navigation, and
discovers pages that expose a JSON config editor (directly or via the Advanced
tab). It writes a manifest of target URLs and labels to a JSON file that the
export script can iterate quickly.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import (  # type: ignore[import-not-found]
    TimeoutError as PlaywrightTimeoutError,
    Page,
    sync_playwright,
)

from config_json_scraper import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_SKIP_GROUPS,
    DEFAULT_SKIP_PATH_FRAGMENTS,
    DEFAULT_SKIP_TEXT,
    DEFAULT_START_PATH,
    EDITOR_SELECTORS,
    UI_ONLY_LABELS,
    USERS_PERMISSIONS_ALLOW,
    USERS_PERMISSIONS_GROUPS,
    collect_nav_links,
    discover_system_links,
    extract_text_from_editor,
    locate_config_editor,
    open_advanced_tab,
    perform_login,
    resolve_headless,
    resolve_base_and_start,
    wait_for_page_ready,
)

TARGETS_FILENAME = "config_json_targets.json"


@dataclass(frozen=True)
class QueueItem:
    url: str
    label: str
    group: str
    source: str


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover admin pages that expose JSON config editors."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--subdomain", help="Tenant subdomain (e.g. app-wu).")
    group.add_argument("--base-url", help="Base tenant URL.")
    group.add_argument("--start-url", help="Full URL to start from.")
    parser.add_argument(
        "--start-path",
        default=DEFAULT_START_PATH,
        help=f"Path to open after login (default: {DEFAULT_START_PATH}).",
    )
    parser.add_argument(
        "--output-file",
        default=TARGETS_FILENAME,
        help="JSON file to write (default: config_json_targets.json).",
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
    parser.add_argument(
        "--skip-group",
        action="append",
        default=list(DEFAULT_SKIP_GROUPS),
        help="Skip nav entries under a group name.",
    )
    parser.add_argument(
        "--skip-text",
        action="append",
        default=list(DEFAULT_SKIP_TEXT),
        help="Skip nav entries whose text contains this value.",
    )
    parser.add_argument(
        "--skip-path",
        action="append",
        default=list(DEFAULT_SKIP_PATH_FRAGMENTS),
        help="Skip URLs whose path contains this fragment.",
    )
    parser.add_argument(
        "--include-text",
        action="append",
        default=[],
        help="Only include nav entries whose label contains this text.",
    )
    parser.add_argument("--max-pages", type=int, default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from an existing manifest and skip already-visited pages.",
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


def normalize_url_for_visit(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("tab_id", None)
    query = "&".join(
        f"{key}={value}" for key in sorted(params) for value in params[key]
    )
    path = parsed.path.rstrip("/") or parsed.path
    return f"{parsed.scheme}://{parsed.netloc}{path}?{query}".rstrip("?")


def should_skip_link(
    label: str,
    group: str,
    url: str,
    skip_groups: Sequence[str],
    skip_text: Sequence[str],
    skip_paths: Sequence[str],
) -> bool:
    label_lower = label.lower()
    group_lower = group.lower()
    group_token = group_lower.strip()
    path_lower = urlparse(url).path.lower()

    if label_lower in UI_ONLY_LABELS:
        return True

    if any(token in group_token for token in USERS_PERMISSIONS_GROUPS):
        if not any(token in label_lower for token in USERS_PERMISSIONS_ALLOW):
            return True

    if any(token in group_lower for token in skip_groups):
        return True
    if any(token in label_lower for token in skip_text):
        return True
    if any(token in path_lower for token in skip_paths):
        return True
    return False


def parse_system_id(url: str) -> Optional[str]:
    params = parse_qs(urlparse(url).query)
    system_id = params.get("system_id") or params.get("systemId")
    if system_id:
        return system_id[0]
    return None


def build_label(base_label: str, system_id: Optional[str]) -> str:
    if system_id:
        return f"{base_label} ({system_id})"
    return base_label


def find_editor_text(page: Page, timeout: float) -> Optional[str]:
    try:
        page.wait_for_selector(", ".join(EDITOR_SELECTORS), timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        return None
    editor = locate_config_editor(page)
    if editor is None:
        return None
    text = extract_text_from_editor(page, editor)
    if text is None or not str(text).strip():
        return None
    return str(text)


def detect_config(
    page: Page, timeout: float, editor_timeout: float
) -> Tuple[Optional[str], Optional[str]]:
    advanced_opened = open_advanced_tab(page, timeout)
    if advanced_opened:
        advanced_text = find_editor_text(page, editor_timeout)
        if advanced_text:
            return page.url, "advanced"

    direct_text = find_editor_text(page, editor_timeout)
    if direct_text:
        return page.url, "direct"

    return None, None


def write_targets_file(path: Path, base_url: str, entries: List[dict]) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "count": len(entries),
        "entries": entries,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def load_manifest(path: Path) -> Tuple[List[dict], set[str], List[str]]:
    if not path.exists():
        return [], set(), []
    data = json.loads(path.read_text(encoding="utf-8"))
    entries = data.get("entries", []) if isinstance(data, dict) else data
    visited = set(data.get("visited", [])) if isinstance(data, dict) else set()
    skipped = list(data.get("skipped", [])) if isinstance(data, dict) else []
    if not visited and isinstance(entries, list):
        for entry in entries:
            url = entry.get("url") or entry.get("config_url")
            if url:
                visited.add(normalize_url_for_visit(url))
    return entries, visited, skipped


def write_manifest(
    path: Path,
    base_url: str,
    entries: List[dict],
    visited: set[str],
    skipped: List[str],
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "count": len(entries),
        "entries": entries,
        "visited": sorted(visited),
        "skipped": skipped,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    username, password = prompt_for_credentials(args)
    headless = resolve_headless(args)
    base_url, start_url = resolve_base_and_start(args)
    output_path = Path(args.output_file)
    if not output_path.is_absolute():
        output_path = Path(__file__).resolve().parent / output_path

    skip_groups = tuple(item.lower() for item in args.skip_group)
    skip_text = tuple(item.lower() for item in args.skip_text)
    skip_paths = tuple(item.lower() for item in args.skip_path)
    include_text = tuple(item.lower() for item in args.include_text)
    slow_hints = tuple(item.lower() for item in args.slow_page_hint)

    entries: List[dict] = []
    visited: set[str] = set()
    skipped: List[str] = []

    if args.resume:
        entries, visited, skipped = load_manifest(output_path)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(start_url)

        perform_login(page, username, password, timeout=args.timeout)
        wait_for_page_ready(page, args.timeout, args.post_login_wait)

        if page.url != start_url:
            page.goto(start_url)
            wait_for_page_ready(page, args.timeout, args.page_wait)

        nav_links = collect_nav_links(page, base_url, args.timeout)
        if not nav_links:
            print("No navigation links discovered.")
            return 1

        queue: List[QueueItem] = []
        for link in nav_links:
            if include_text and not any(token in link.label.lower() for token in include_text):
                continue
            if should_skip_link(link.label, link.group, link.href, skip_groups, skip_text, skip_paths):
                continue
            normalized = normalize_url_for_visit(link.href)
            if args.resume and normalized in visited:
                continue
            queue.append(QueueItem(link.href, link.label, link.group, "nav"))

        while queue:
            item = queue.pop(0)
            normalized = normalize_url_for_visit(item.url)
            if normalized in visited:
                continue
            visited.add(normalized)
            write_manifest(output_path, base_url, entries, visited, skipped)
            if args.max_pages and len(visited) > args.max_pages:
                break

            try:
                page.goto(item.url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                skipped.append(f"{item.url} -> navigation_timeout")
                write_manifest(output_path, base_url, entries, visited, skipped)
                continue

            extra_wait = args.page_wait
            if any(hint in page.url.lower() for hint in slow_hints):
                extra_wait = max(extra_wait, args.slow_page_wait)
            wait_for_page_ready(page, args.timeout, extra_wait)

            system_links = discover_system_links(page, base_url)
            for system_url in system_links:
                system_id = parse_system_id(system_url)
                label = build_label(item.label, system_id)
                normalized_system = normalize_url_for_visit(system_url)
                if args.resume and normalized_system in visited:
                    continue
                queue.append(QueueItem(system_url, label, item.group, "system"))

            config_url, mode = detect_config(page, args.timeout, args.editor_timeout)
            if config_url and mode:
                entry = {
                    "label": item.label,
                    "group": item.group,
                    "url": item.url,
                    "config_url": config_url,
                    "mode": mode,
                    "source": item.source,
                    "discovered_at": datetime.now(timezone.utc).isoformat(),
                }
                entries.append(entry)
                write_manifest(output_path, base_url, entries, visited, skipped)

        browser.close()

    write_manifest(output_path, base_url, entries, visited, skipped)
    print(f"Discovered {len(entries)} config pages.")
    print(f"Saved to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
