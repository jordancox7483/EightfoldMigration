"""Automated scraper for Eightfold custom field definitions.

This script signs into an Eightfold tenant, visits the Custom Fields
integration page, walks through all pages of results, and writes the field
names and identifiers to a CSV file.

Usage example:

    python custom_fields_scraper.py --subdomain app-wu --output fields.csv

Credentials are requested at runtime so they are not persisted in the source
code or shell history.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Any

from playwright.sync_api import (  # type: ignore[import-not-found]
    TimeoutError as PlaywrightTimeoutError,
    Locator,
    Page,
    Frame,
    sync_playwright,
)


@dataclass
class FieldRecord:
    """Represents a single row from the custom fields table."""

    name: str
    identifier: str


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Scrape the Eightfold custom fields page and export field names and "
            "IDs to a CSV file."
        )
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--subdomain",
        help=(
            "Subdomain portion of the Eightfold URL (e.g. 'app-wu' for "
            "https://app-wu.eightfold.ai)."
        ),
    )
    group.add_argument(
        "--url",
        help=(
            "Full URL to the custom fields page (e.g. "
            "'https://app-wu.eightfold.ai/integrations/custom_fields')."
        ),
    )
    parser.add_argument(
        "--output",
        default="custom_fields.csv",
        help="Destination CSV file. Defaults to 'custom_fields.csv'.",
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
        default=15.0,
        help="Seconds to wait for page elements before giving up (default: 15).",
    )
    parser.add_argument(
        "--post-login-wait",
        type=float,
        default=10.0,
        help=(
            "Extra seconds to wait after login for the page to finish loading "
            "before scanning for the table (default: 10)."
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
        "--manual-continue",
        action="store_true",
        help=(
            "Pause after login and navigating to the target page; press Enter "
            "manually to continue once the table is visible."
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.headless and args.headed:
        parser.error("--headless and --headed cannot be used together")

    return args


def prompt_for_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Obtain credentials from args, env vars, or interactively."""

    import os

    username = (args.username or os.environ.get("EIGHTFOLD_USERNAME") or "").strip()
    password = args.password or os.environ.get("EIGHTFOLD_PASSWORD")

    if not username:
        username = input("Eightfold username: ").strip()
    if not password:
        password = getpass.getpass("Eightfold password: ")

    return username, password


def resolve_headless(args: argparse.Namespace) -> bool:
    """Determine whether to launch the browser headless."""

    if args.headed:
        return False
    if args.headless:
        return True
    return True


def perform_login(page: Page, username: str, password: str, timeout: float) -> None:
    """Fill the login form and submit it.

    The selectors cover common field names used by Eightfold's hosted login.
    Users with SSO enabled may need to adapt this function for their flow.
    """

    email_selector = (
        "input[type='email'], input[name*='email'], input[name*='username'], "
        "input#username, input#email"
    )
    password_selector = (
        "input[type='password'], input[name*='password'], input#password"
    )

    try:
        email_input = page.wait_for_selector(email_selector, timeout=timeout * 1000)
    except PlaywrightTimeoutError as exc:  # pragma: no cover - requires live site
        raise RuntimeError(
            "Could not find the username/email field on the login page."
        ) from exc

    email_input.fill(username)

    try:
        password_input = page.wait_for_selector(
            password_selector, timeout=timeout * 1000
        )
    except PlaywrightTimeoutError as exc:  # pragma: no cover - requires live site
        raise RuntimeError("Could not find the password field on the login page.") from exc

    password_input.fill(password)

    # Press enter or click a submit button if present.
    password_input.press("Enter")

    # Some tenants may have an explicit submit button.
    submit_locator = page.locator(
        "button[type='submit'], button:has-text('Sign in'), button:has-text('Log in')"
    )
    if submit_locator.count() > 0:
        try:
            submit_locator.first.click()
        except PlaywrightTimeoutError:  # pragma: no cover - requires live site
            pass


def _locate_grid_root(page: Page, timeout_ms: float) -> Optional[Locator]:
    """Try to locate the table/grid container using multiple selector patterns."""
    candidates = [
        "table",
        "[role='table']",
        "[role='grid']",
        "div[aria-label*='table']",
        "div[aria-label*='grid']",
    ]
    for sel in candidates:
        loc = page.locator(sel)
        if loc.count() > 0:
            try:
                page.wait_for_selector(sel, timeout=timeout_ms)
            except PlaywrightTimeoutError:
                pass
            return loc.first
    return None


def extract_headers(page: Page) -> List[str]:
    """Return the table header labels, supporting semantic tables and ARIA grids."""

    headers: List[str] = []
    root = _locate_grid_root(page, timeout_ms=5000)
    if root is None:
        return headers

    header_locator = root.locator("thead tr th")
    if header_locator.count() == 0:
        header_locator = root.locator("[role='columnheader']")
    for i in range(header_locator.count()):
        headers.append(header_locator.nth(i).inner_text().strip())
    return headers


def find_column_index(headers: List[str], desired: Iterable[str]) -> int:
    """Locate a column by header name.

    Raises ValueError if none of the desired names are present.
    """

    lowered = [header.lower() for header in headers]
    for option in desired:
        option_lower = option.lower()
        if option_lower in lowered:
            return lowered.index(option_lower)
    raise ValueError(f"None of the expected column headers found: {desired}")


def extract_page_rows(page: Page, name_idx: int, id_idx: int) -> List[FieldRecord]:
    """Extract all field records from the current table page."""

    records: List[FieldRecord] = []
    root = _locate_grid_root(page, timeout_ms=5000)
    if root is None:
        return records
    row_locator = root.locator("tbody tr")
    if row_locator.count() == 0:
        # ARIA rows (exclude header rows with columnheader)
        row_locator = root.locator("[role='row']").filter(
            has_not=page.locator("[role='columnheader']")
        )
    for row_num in range(row_locator.count()):
        row = row_locator.nth(row_num)
        cells = row.locator("td")
        if cells.count() == 0:
            cells = row.locator("[role='cell'], [role='gridcell']")
        if cells.count() == 0:
            continue
        name = cells.nth(name_idx).inner_text().strip()
        identifier = cells.nth(id_idx).inner_text().strip()
        if name or identifier:
            records.append(FieldRecord(name=name, identifier=identifier))
    return records


def paginate_and_collect(page: Page, timeout: float) -> List[FieldRecord]:
    """Iterate over the paginated table and collect all field rows."""

    # Wait for the table to appear after login.
    try:
        root = _locate_grid_root(page, timeout_ms=timeout * 1000)
        if root is None:
            raise PlaywrightTimeoutError("Could not locate table/grid root")
    except PlaywrightTimeoutError as exc:  # pragma: no cover - requires live site
        # Capture debug artifacts to help identify the correct selectors.
        try:
            import os
            debug_dir = os.path.abspath("debug")
            os.makedirs(debug_dir, exist_ok=True)
            png_path = os.path.join(debug_dir, "custom_fields_page.png")
            html_path = os.path.join(debug_dir, "custom_fields_page.html")
            page.screenshot(path=png_path, full_page=True)
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"Debug saved: {png_path}", file=sys.stderr)
            print(f"Debug saved: {html_path}", file=sys.stderr)
            print(f"At URL: {page.url}", file=sys.stderr)
        except Exception:
            pass
        raise
    headers = extract_headers(page)
    name_idx = find_column_index(headers, ["Field Name", "Name", "Field"])
    id_idx = find_column_index(headers, ["ID", "Field ID", "Identifier"])

    all_records: List[FieldRecord] = []

    while True:
        page.wait_for_timeout(300)
        all_records.extend(extract_page_rows(page, name_idx, id_idx))

        next_button = _find_next_button2(page)
        if next_button is None or not next_button.is_enabled():
            break

        body = _locate_grid_root(page, timeout_ms=2000)
        snapshot = body.inner_text() if body else ""
        next_button.click()
        try:
            # Allow content to refresh; then compare snapshots to detect change.
            try:
                page.wait_for_load_state("networkidle", timeout=timeout * 1000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(800)
            new_body = _locate_grid_root(page, timeout_ms=2000)
            new_snapshot = new_body.inner_text() if new_body else ""
            if body and snapshot == new_snapshot:
                # No change detected — assume last page.
                break
        except PlaywrightTimeoutError:  # pragma: no cover - requires live site
            # If the content did not change, assume we reached the last page.
            break

    return all_records


def _find_next_button(page: Page) -> Optional[Locator]:
    """Locate the paginator's next button if one exists."""

    candidates = [
        "button[aria-label*='Next']",
        "button[aria-label*='next']",
        "button:has-text('Next')",
        "button:has-text('›')",
        "button:has-text('>')",
        "a[aria-label*='Next']",
    ]

    for selector in candidates:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    return None


def _find_next_button2(page: Page) -> Optional[Locator]:
    """Improved next button locator supporting ARIA and unicode arrows."""

    candidates = [
        "button[aria-label*='Next']",
        "button[aria-label*='next']",
        "button:has-text('Next')",
        "button:has-text('\u203a')",  # '›'
        "button:has-text('>')",
        "a[aria-label*='Next']",
        "a[role='button'][aria-label*='Next']",
        "a.pagination-right",
        "a[role='button'].pagination-right",
        "[role='button']:has-text('Next')",
    ]

    for selector in candidates:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator.first
    return None


def write_csv(path: str, records: Iterable[FieldRecord]) -> None:
    """Write the field records to a CSV file."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Field Name", "Field ID"])
        for record in records:
            writer.writerow([record.name, record.identifier])


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    username, password = prompt_for_credentials(args)

    headless = resolve_headless(args)
    url = (
        args.url
        if getattr(args, "url", None)
        else f"https://{args.subdomain}.eightfold.ai/integrations/custom_fields"
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        perform_login(page, username, password, timeout=args.timeout)

        # Allow dynamic content and redirects to settle.
        try:
            page.wait_for_load_state("networkidle", timeout=args.timeout * 1000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(int(args.post_login_wait * 1000))

        # Some flows land on a dashboard after login; ensure we are at the target page.
        if page.url != url:
            page.goto(url)
            try:
                page.wait_for_load_state("networkidle", timeout=args.timeout * 1000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(500)

        if args.manual_continue:
            print(
                "Logged in. Manually ensure the Custom Fields page is fully loaded, then press Enter to continue...",
                file=sys.stderr,
            )
            try:
                input()
            except EOFError:
                # Non-interactive environment: continue without pause.
                pass

        records = paginate_and_collect(page, timeout=args.timeout)

        if not records:
            print(
                "No records were collected. Please verify that the credentials "
                "are correct and that you have access to the custom fields page.",
                file=sys.stderr,
            )
            return 1

        write_csv(args.output, records)

    print(f"Exported {len(records)} custom field records to {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
