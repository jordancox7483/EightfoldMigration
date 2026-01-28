"""Scrape JSON configuration from the Eightfold Admin Console.

This script logs into an Eightfold tenant, discovers admin configuration pages,
opens the Advanced tab when available, and saves the JSON config editor content
to individual files. Output files are named using the page ID from the URL plus
important query parameters (for example system_id).

Usage example:

    python config_json_scraper.py --subdomain app-wu --headed

Credentials are requested at runtime or read from environment variables so they
are not persisted in the source code or shell history.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

from playwright.sync_api import (  # type: ignore[import-not-found]
    TimeoutError as PlaywrightTimeoutError,
    Locator,
    Page,
    sync_playwright,
)

DEFAULT_OUTPUT_DIR = Path(r"C:\Users\Jordan\OneDrive\Documents\EightfoldCOnfigScrape")
DEFAULT_START_PATH = "/integrations"
DEFAULT_SLOW_PAGE_HINTS = ("workflow_config",)
DEFAULT_SKIP_GROUPS = ("home", "surveys", "apps")
DEFAULT_SKIP_TEXT = (
    "email & scheduling templates",
    "file ingestion",
    "api ingestion",
    "sync health",
    "diagnostics",
    "health reports",
    "sftp configuration",
    "talent lake",
    "provision dashboard",
    "provision dashboards",
    "refresh warehouse",
    "refresh data warehouse",
    "auto setup",
    "careerhub branding",
    "career hub branding",
    "manage hrbp users",
    "manage users",
)
DEFAULT_SKIP_PATH_FRAGMENTS = ("/home", "/surveys", "/apps")
EDITOR_SELECTORS = (".ace_editor", ".monaco-editor", ".CodeMirror", "textarea")
UI_ONLY_LABELS = ("forms library", "question bank", "questions library")
USERS_PERMISSIONS_GROUPS = ("users & permissions", "users and permissions")
USERS_PERMISSIONS_ALLOW = ("permission", "role")


@dataclass(frozen=True)
class NavLink:
    label: str
    href: str
    group: str


@dataclass(frozen=True)
class ScrapeResult:
    url: str
    output_path: Optional[Path]
    reason: str


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape JSON config blocks from the Eightfold admin console."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--subdomain",
        help="Subdomain portion of the Eightfold URL (e.g. 'app-wu').",
    )
    group.add_argument(
        "--base-url",
        help="Base URL for the tenant (e.g. 'https://app-wu.eightfold.ai').",
    )
    group.add_argument(
        "--start-url",
        help=(
            "Full URL to start the crawl (e.g. "
            "'https://app-wu.eightfold.ai/integrations/custom_fields')."
        ),
    )
    parser.add_argument(
        "--start-path",
        default=DEFAULT_START_PATH,
        help=(
            "Path to open after login when --subdomain/--base-url is used. "
            f"Defaults to '{DEFAULT_START_PATH}'."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help=(
            "Directory for exported JSON files. Defaults to "
            f"'{DEFAULT_OUTPUT_DIR}'."
        ),
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch the browser in headless mode (default).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Launch the browser with a visible window for troubleshooting.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for page elements before timing out (default: 20).",
    )
    parser.add_argument(
        "--editor-timeout",
        type=float,
        default=20.0,
        help="Seconds to wait for the config editor to appear (default: 20).",
    )
    parser.add_argument(
        "--post-login-wait",
        type=float,
        default=12.0,
        help="Seconds to wait after login for the admin UI to finish loading.",
    )
    parser.add_argument(
        "--page-wait",
        type=float,
        default=3.0,
        help="Extra seconds to wait after navigation for heavy pages.",
    )
    parser.add_argument(
        "--slow-page-wait",
        type=float,
        default=12.0,
        help="Extra seconds to wait for known slow pages (default: 12).",
    )
    parser.add_argument(
        "--slow-page-hint",
        action="append",
        default=list(DEFAULT_SLOW_PAGE_HINTS),
        help=(
            "URL substring that marks a page as slow. Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=0,
        help="Optional cap on the number of pages to visit (0 = no limit).",
    )
    parser.add_argument(
        "--max-configs",
        type=int,
        default=0,
        help="Optional cap on the number of configs to export (0 = no limit).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover pages but do not open the Advanced tab or save files.",
    )
    parser.add_argument(
        "--manual-continue",
        action="store_true",
        help=(
            "Pause after login and initial navigation. Press Enter to continue."
        ),
    )
    parser.add_argument(
        "--username",
        help=(
            "Eightfold username. If omitted, the script reads from the "
            "EIGHTFOLD_USERNAME environment variable or prompts interactively."
        ),
    )
    parser.add_argument(
        "--password",
        help=(
            "Eightfold password. If omitted, the script reads from the "
            "EIGHTFOLD_PASSWORD environment variable or prompts interactively."
        ),
    )
    parser.add_argument(
        "--skip-group",
        action="append",
        default=list(DEFAULT_SKIP_GROUPS),
        help=(
            "Skip nav entries under a group name (case-insensitive). "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--skip-text",
        action="append",
        default=list(DEFAULT_SKIP_TEXT),
        help=(
            "Skip nav entries whose text contains this value (case-insensitive). "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--skip-path",
        action="append",
        default=list(DEFAULT_SKIP_PATH_FRAGMENTS),
        help=(
            "Skip URLs whose path contains this fragment (case-insensitive). "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--include-text",
        action="append",
        default=[],
        help=(
            "Only visit nav links whose label contains this text (case-insensitive). "
            "Can be provided multiple times."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save screenshots/HTML to a debug folder when extraction fails.",
    )
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


def resolve_headless(args: argparse.Namespace) -> bool:
    if args.headed:
        return False
    if args.headless:
        return True
    return True


def resolve_base_and_start(args: argparse.Namespace) -> Tuple[str, str]:
    if args.start_url:
        parsed = urlparse(args.start_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        start_url = args.start_url
        return base_url.rstrip("/"), start_url

    if args.base_url:
        base_url = args.base_url.rstrip("/")
    else:
        base_url = f"https://{args.subdomain}.eightfold.ai"

    start_path = args.start_path
    if not start_path.startswith("/"):
        start_path = "/" + start_path

    return base_url, f"{base_url}{start_path}"


def perform_login(page: Page, username: str, password: str, timeout: float) -> None:
    email_selector = (
        "input[type='email'], input[name*='email'], input[name*='username'], "
        "input#username, input#email"
    )
    password_selector = (
        "input[type='password'], input[name*='password'], input#password"
    )

    try:
        email_input = page.wait_for_selector(email_selector, timeout=timeout * 1000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "Could not find the username/email field on the login page."
        ) from exc

    email_input.fill(username)

    try:
        password_input = page.wait_for_selector(
            password_selector, timeout=timeout * 1000
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Could not find the password field on the login page.") from exc

    password_input.fill(password)
    password_input.press("Enter")

    submit_locator = page.locator(
        "button[type='submit'], button:has-text('Sign in'), button:has-text('Log in')"
    )
    if submit_locator.count() > 0:
        try:
            submit_locator.first.click()
        except PlaywrightTimeoutError:
            pass


def wait_for_page_ready(page: Page, timeout: float, extra_wait: float) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        pass
    if extra_wait > 0:
        page.wait_for_timeout(int(extra_wait * 1000))


def locate_nav_root(page: Page) -> Optional[Locator]:
    for selector in ("nav", "aside", "[role='navigation']", "div[role='navigation']"):
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    return None


def expand_all_nav_sections(page: Page, nav_root: Locator) -> None:
    root_handle = nav_root.element_handle()
    if root_handle is None:
        return

    for _ in range(10):
        clicked = page.evaluate(
            """
            (root) => {
                const selectors = [
                    "button[aria-expanded='false']",
                    "[role='button'][aria-expanded='false']"
                ];
                const toggles = root.querySelectorAll(selectors.join(","));
                toggles.forEach((el) => el.click());
                return toggles.length;
            }
            """,
            root_handle,
        )
        if not clicked:
            break
        page.wait_for_timeout(200)


def extract_anchor_links(
    page: Page, base_url: str, scope: Optional[Locator] = None
) -> List[NavLink]:
    root_handle = scope.element_handle() if scope is not None else None
    if scope is not None and root_handle is None:
        return []

    raw_links = page.evaluate(
        """
        (root) => {
            const results = [];
            const container = root || document;
            const anchors = container.querySelectorAll('a[href]');
            anchors.forEach((anchor) => {
                const href = anchor.getAttribute('href');
                const text = (anchor.textContent || '').replace(/\\s+/g, ' ').trim();
                let group = '';
                let node = anchor;
                while (node && node !== container) {
                    const dataGroup = node.getAttribute && (
                        node.getAttribute('data-group') ||
                        node.getAttribute('data-section') ||
                        node.getAttribute('data-section-name')
                    );
                    if (dataGroup) {
                        group = dataGroup.trim();
                        break;
                    }
                    const heading = node.querySelector && node.querySelector(
                        'h1,h2,h3,h4,h5,h6,.nav-section-title,.section-title,.groupLabel-3rWQv'
                    );
                    if (heading && heading.textContent) {
                        group = heading.textContent.replace(/\\s+/g, ' ').trim();
                        break;
                    }
                    node = node.parentElement;
                }
                results.push({ href, text, group });
            });
            return results;
        }
        """,
        root_handle,
    )

    links: List[NavLink] = []
    seen: set[str] = set()
    for entry in raw_links:
        href = entry.get("href") if isinstance(entry, dict) else None
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.startswith(base_url):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(
            NavLink(
                label=(entry.get("text") or "").strip(),
                href=absolute,
                group=(entry.get("group") or "").strip(),
            )
        )
    return links


def collect_nav_links(page: Page, base_url: str, timeout: float) -> List[NavLink]:
    nav_root = locate_nav_root(page)
    if nav_root is None:
        return []

    try:
        nav_root.wait_for(state="visible", timeout=timeout * 1000)
    except PlaywrightTimeoutError:
        pass

    links: List[NavLink] = []
    seen: set[str] = set()

    def merge(new_links: List[NavLink]) -> None:
        for link in new_links:
            if link.href in seen:
                continue
            seen.add(link.href)
            links.append(link)

    expand_all_nav_sections(page, nav_root)
    merge(extract_anchor_links(page, base_url, nav_root))

    group_headers = nav_root.locator("[role='button'][aria-expanded]")
    header_count = group_headers.count()
    for idx in range(header_count):
        header = group_headers.nth(idx)
        try:
            header.scroll_into_view_if_needed(timeout=1000)
        except PlaywrightTimeoutError:
            pass
        try:
            page.evaluate("(el) => el && el.click()", header)
        except Exception:
            try:
                header.click(timeout=1000, force=True)
            except PlaywrightTimeoutError:
                continue
        page.wait_for_timeout(200)
        expand_all_nav_sections(page, nav_root)
        merge(extract_anchor_links(page, base_url, nav_root))

    if not links:
        merge(extract_anchor_links(page, base_url, None))

    return links


def should_skip_link(
    link: NavLink,
    skip_groups: Sequence[str],
    skip_text: Sequence[str],
    skip_paths: Sequence[str],
) -> bool:
    text = link.label.lower()
    group = link.group.lower()
    path = urlparse(link.href).path.lower()

    if text in UI_ONLY_LABELS:
        return True

    if any(token in group for token in USERS_PERMISSIONS_GROUPS):
        if not any(token in text for token in USERS_PERMISSIONS_ALLOW):
            return True

    if any(token in group for token in skip_groups):
        return True
    if any(token in text for token in skip_text):
        return True
    if any(token in path for token in skip_paths):
        return True
    return False


def normalize_url_for_visit(url: str) -> str:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    params.pop("tab_id", None)
    query = urlencode(
        [(key, value) for key in sorted(params) for value in params[key]],
        doseq=True,
    )
    path = parsed.path.rstrip("/") or parsed.path
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def discover_system_links(page: Page, base_url: str) -> List[str]:
    links: List[str] = []

    for selector in ("a[href*='system_id=']", "a[href*='systemId=']"):
        locator = page.locator(selector)
        for idx in range(locator.count()):
            href = locator.nth(idx).get_attribute("href")
            if href:
                absolute = urljoin(base_url, href)
                if absolute.startswith(base_url):
                    links.append(absolute)

    system_ids = page.evaluate(
        """
        () => {
            const ids = new Set();
            document.querySelectorAll('[data-system-id], [data-system_id], [data-systemid]')
              .forEach((el) => {
                const id = el.getAttribute('data-system-id') ||
                  el.getAttribute('data-system_id') ||
                  el.getAttribute('data-systemid');
                if (id) {
                    ids.add(id);
                }
              });
            return Array.from(ids);
        }
        """
    )

    if system_ids:
        base = urlparse(page.url)
        base_query = parse_qs(base.query)
        base_query.pop("tab_id", None)
        for system_id in system_ids:
            base_query["system_id"] = [system_id]
            query = urlencode(
                [(key, value) for key in sorted(base_query) for value in base_query[key]],
                doseq=True,
            )
            url = urlunparse((base.scheme, base.netloc, base.path, "", query, ""))
            if url.startswith(base_url):
                links.append(url)

    return sorted(set(links))


def open_advanced_tab(page: Page, timeout: float) -> bool:
    tablist = page.locator("[role='tablist']")
    candidates: List[Locator] = []
    if tablist.count() > 0:
        candidates.append(
            tablist.locator("button:has-text('Advanced'), a:has-text('Advanced')")
        )
        candidates.append(tablist.locator("[role='tab']:has-text('Advanced')"))
    candidates.append(page.locator("[role='tab']:has-text('Advanced')"))
    candidates.append(page.locator("button:has-text('Advanced')"))
    candidates.append(page.locator("a:has-text('Advanced')"))

    for locator in candidates:
        if locator.count() == 0:
            continue
        try:
            locator.first.click()
            try:
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                pass
            return True
        except PlaywrightTimeoutError:
            continue
    return False


def _collect_editor_candidates(page: Page) -> List[Tuple[Locator, dict]]:
    candidates: List[Tuple[Locator, dict]] = []
    for selector in EDITOR_SELECTORS:
        locator = page.locator(selector)
        for idx in range(locator.count()):
            element = locator.nth(idx)
            box = element.bounding_box()
            if not box:
                continue
            if box["height"] < 120 or box["width"] < 200:
                continue
            candidates.append((element, box))
    return candidates


def _choose_editor_near_label(page: Page, label_text: str) -> Optional[Locator]:
    label_locator = page.get_by_text(label_text, exact=True)
    if label_locator.count() == 0:
        return None
    label = label_locator.first
    label_box = label.bounding_box()
    if not label_box:
        return None

    candidates = _collect_editor_candidates(page)
    if not candidates:
        return None

    best: Optional[Tuple[float, Locator]] = None
    for element, box in candidates:
        if box["y"] + box["height"] < label_box["y"]:
            continue
        distance = abs(box["x"] - label_box["x"]) + max(0.0, box["y"] - label_box["y"])
        if best is None or distance < best[0]:
            best = (distance, element)
    return best[1] if best else None


def _choose_leftmost_editor(page: Page) -> Optional[Locator]:
    candidates = _collect_editor_candidates(page)
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[1]["x"], item[1]["y"]))
    return candidates[0][0]


def locate_config_editor(page: Page) -> Optional[Locator]:
    for label in ("Config", "Configuration"):
        editor = _choose_editor_near_label(page, label)
        if editor is not None:
            return editor
    return _choose_leftmost_editor(page)


def extract_text_from_editor(page: Page, editor: Locator) -> Optional[str]:
    handle = editor.element_handle()
    if handle is None:
        return None

    text = page.evaluate(
        """
        (el) => {
            if (!el) return null;
            if (el.classList && el.classList.contains('ace_editor') && window.ace) {
                try {
                    return window.ace.edit(el).getValue();
                } catch (err) {
                    return el.innerText || el.textContent || null;
                }
            }
            if (el.classList && el.classList.contains('monaco-editor') && window.monaco && window.monaco.editor) {
                try {
                    if (window.monaco.editor.getEditors) {
                        const editors = window.monaco.editor.getEditors();
                        for (const editor of editors) {
                            const node = editor.getDomNode && editor.getDomNode();
                            if (node === el || (node && node.contains(el)) || (el.contains && el.contains(node))) {
                                return editor.getValue();
                            }
                        }
                    }
                    const models = window.monaco.editor.getModels();
                    if (models.length === 1) {
                        return models[0].getValue();
                    }
                    const nodes = Array.from(document.querySelectorAll('.monaco-editor'));
                    const idx = nodes.indexOf(el);
                    if (idx >= 0 && models[idx]) {
                        return models[idx].getValue();
                    }
                } catch (err) {
                    return el.innerText || el.textContent || null;
                }
            }
            if (el.classList && el.classList.contains('CodeMirror')) {
                try {
                    if (el.CodeMirror) {
                        return el.CodeMirror.getValue();
                    }
                } catch (err) {
                    return el.innerText || el.textContent || null;
                }
            }
            const textarea = el.tagName === 'TEXTAREA' ? el : el.querySelector('textarea');
            if (textarea && textarea.value) {
                return textarea.value;
            }
            const contentEditable = el.querySelector('[contenteditable="true"]');
            if (contentEditable) {
                return contentEditable.innerText;
            }
            return el.innerText || el.textContent || null;
        }
        """,
        handle,
    )
    if text is None:
        return None
    return str(text)


def sanitize_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "config"


def build_output_filename(url: str) -> str:
    parsed = urlparse(url)
    page_id = parsed.path.rstrip("/").split("/")[-1] or "config"

    params = parse_qs(parsed.query)
    params.pop("tab_id", None)

    suffixes: List[str] = []
    for key in sorted(params):
        for value in params[key]:
            suffixes.append(f"{key}={value}")

    name = sanitize_filename(page_id)
    if suffixes:
        name = f"{name}__{sanitize_filename('__'.join(suffixes))}"

    return f"{name}.json"


def normalize_json_text(text: str) -> Optional[str]:
    cleaned = text.strip()
    if not cleaned:
        return None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return cleaned
    return json.dumps(parsed, indent=2, ensure_ascii=False)


def save_debug_artifacts(
    page: Page, debug_dir: Path, slug: str, reason: str
) -> None:
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_slug = sanitize_filename(slug)
    screenshot = debug_dir / f"{safe_slug}_{sanitize_filename(reason)}.png"
    html_path = debug_dir / f"{safe_slug}_{sanitize_filename(reason)}.html"
    try:
        page.screenshot(path=str(screenshot), full_page=True)
    except Exception:
        pass
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def scrape_config_from_page(
    page: Page,
    timeout: float,
    editor_timeout: float,
    debug_dir: Optional[Path],
    enable_debug: bool,
) -> Tuple[Optional[str], str]:
    advanced_opened = open_advanced_tab(page, timeout)

    def read_editor() -> Tuple[Optional[str], Optional[str]]:
        try:
            page.wait_for_selector(
                ", ".join(EDITOR_SELECTORS), timeout=editor_timeout * 1000
            )
        except PlaywrightTimeoutError:
            return None, "editor_not_found"

        editor = locate_config_editor(page)
        if editor is None:
            return None, "editor_not_located"

        text = extract_text_from_editor(page, editor)
        if text is None:
            return None, "editor_empty"

        normalized = normalize_json_text(text)
        if normalized is None:
            return None, "config_empty"
        return normalized, None

    # Prefer Advanced tab if available.
    if advanced_opened:
        text, err = read_editor()
        if text is not None:
            return text, "ok"
        if enable_debug and debug_dir is not None:
            save_debug_artifacts(page, debug_dir, page.url, err or "editor_error")

    # Fall back to direct editor on the page.
    text, err = read_editor()
    if text is not None:
        return text, "ok"

    if enable_debug and debug_dir is not None:
        save_debug_artifacts(page, debug_dir, page.url, err or "editor_error")
    if not advanced_opened:
        return None, "advanced_tab_not_found"
    return None, err or "editor_not_found"


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    username, password = prompt_for_credentials(args)
    headless = resolve_headless(args)
    base_url, start_url = resolve_base_and_start(args)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    debug_dir = output_dir / "debug"

    skip_groups = tuple(item.lower() for item in args.skip_group)
    skip_text = tuple(item.lower() for item in args.skip_text)
    skip_paths = tuple(item.lower() for item in args.skip_path)
    include_text = tuple(item.lower() for item in args.include_text)
    slow_hints = tuple(item.lower() for item in args.slow_page_hint)

    results: List[ScrapeResult] = []
    exported_count = 0

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

        if args.manual_continue:
            print(
                "Logged in. Navigate to the admin console if needed, then press Enter to continue...",
                file=sys.stderr,
            )
            try:
                input()
            except EOFError:
                pass

        nav_links = collect_nav_links(page, base_url, args.timeout)
        if not nav_links:
            print("No navigation links discovered. Check permissions.", file=sys.stderr)
            return 1

        queue: List[str] = []
        for link in nav_links:
            if include_text and not any(token in link.label.lower() for token in include_text):
                continue
            if should_skip_link(link, skip_groups, skip_text, skip_paths):
                continue
            queue.append(link.href)

        visited: set[str] = set()
        while queue:
            url = queue.pop(0)
            normalized = normalize_url_for_visit(url)
            if normalized in visited:
                continue
            visited.add(normalized)

            if args.max_pages and len(visited) > args.max_pages:
                break

            try:
                page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                if args.debug:
                    save_debug_artifacts(page, debug_dir, url, "navigation_timeout")
                results.append(ScrapeResult(url=url, output_path=None, reason="navigation_timeout"))
                continue

            extra_wait = args.page_wait
            if any(hint in page.url.lower() for hint in slow_hints):
                extra_wait = max(extra_wait, args.slow_page_wait)
            wait_for_page_ready(page, args.timeout, extra_wait)

            if not include_text:
                for discovered in discover_system_links(page, base_url):
                    normalized_discovered = normalize_url_for_visit(discovered)
                    if normalized_discovered not in visited:
                        queue.append(discovered)

            if args.dry_run:
                results.append(ScrapeResult(url=page.url, output_path=None, reason="dry_run"))
                continue

            config_text, reason = scrape_config_from_page(
                page, args.timeout, args.editor_timeout, debug_dir, args.debug
            )
            if config_text is None:
                results.append(ScrapeResult(url=page.url, output_path=None, reason=reason))
                continue

            filename = build_output_filename(page.url)
            destination = output_dir / filename
            destination.write_text(config_text, encoding="utf-8")
            exported_count += 1
            results.append(ScrapeResult(url=page.url, output_path=destination, reason="exported"))

            if args.max_configs and exported_count >= args.max_configs:
                break

        browser.close()

    exported = [r for r in results if r.output_path is not None]
    skipped = [r for r in results if r.output_path is None]

    print(f"Visited {len(results)} pages. Exported {len(exported)} configs.")
    for record in exported:
        print(f"Saved: {record.output_path}")
    if skipped:
        print("Skipped pages:")
        for record in skipped:
            print(f"  {record.url} -> {record.reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
