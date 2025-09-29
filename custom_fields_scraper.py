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
from typing import Iterable, List, Optional

from playwright.sync_api import (  # type: ignore[import-not-found]
    TimeoutError as PlaywrightTimeoutError,
    Locator,
    Page,
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
    parser.add_argument(
        "--subdomain",
        required=True,
        help=(
            "Subdomain portion of the Eightfold URL (e.g. 'app-wu' for "
            "https://app-wu.eightfold.ai)."
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
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.headless and args.headed:
        parser.error("--headless and --headed cannot be used together")

    return args


def prompt_for_credentials() -> tuple[str, str]:
    """Interactively prompt the user for credentials."""

    username = input("Eightfold username: ").strip()
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


def extract_headers(page: Page) -> List[str]:
    """Return the table header labels."""

    header_locator = page.locator("table thead tr th")
    headers: List[str] = []
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
    row_locator = page.locator("table tbody tr")
    for row_num in range(row_locator.count()):
        row = row_locator.nth(row_num)
        cells = row.locator("td")
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
    page.wait_for_selector("table", timeout=timeout * 1000)
    headers = extract_headers(page)
    name_idx = find_column_index(headers, ["Field Name", "Name", "Field"])
    id_idx = find_column_index(headers, ["ID", "Field ID", "Identifier"])

    all_records: List[FieldRecord] = []

    while True:
        page.wait_for_timeout(300)
        all_records.extend(extract_page_rows(page, name_idx, id_idx))

        next_button = _find_next_button(page)
        if next_button is None or not next_button.is_enabled():
            break

        snapshot = page.locator("table tbody").inner_text()
        next_button.click()
        try:
            page.wait_for_function(
                "(prev) => document.querySelector('table tbody').innerText !== prev",
                arg=snapshot,
                timeout=timeout * 1000,
            )
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


def write_csv(path: str, records: Iterable[FieldRecord]) -> None:
    """Write the field records to a CSV file."""

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Field Name", "Field ID"])
        for record in records:
            writer.writerow([record.name, record.identifier])


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    username, password = prompt_for_credentials()

    headless = resolve_headless(args)
    url = f"https://{args.subdomain}.eightfold.ai/integrations/custom_fields"

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url)

        perform_login(page, username, password, timeout=args.timeout)

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
