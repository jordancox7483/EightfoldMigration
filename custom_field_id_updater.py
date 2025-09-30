#!/usr/bin/env python3
"""Update custom field IDs in a profile display configuration.

This helper reads mappings from source/target CSV exports, then walks a
profile display JSON file to swap any legacy ``custom_field_id`` values with
their replacements. The intent mirrors ``sync_workflow_ids.py`` but with a
single field type and simplified mapping rules.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

DEFAULT_SOURCE_PROFILE = Path("source_profile_display.json")
DEFAULT_TARGET_PROFILE = Path("target_profile_display.json")
DEFAULT_SOURCE_CSV = Path("source.csv")
DEFAULT_TARGET_CSV = Path("target.csv")


class UpdateError(RuntimeError):
    """Raised when the custom field updater encounters unrecoverable issues."""


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update custom_field_id values in a profile display JSON using "
            "mappings from source/target CSV exports."
        )
    )
    parser.add_argument(
        "--source-profile",
        type=Path,
        default=DEFAULT_SOURCE_PROFILE,
        help="Path to the source profile display JSON (for reference only).",
    )
    parser.add_argument(
        "--target-profile",
        type=Path,
        default=DEFAULT_TARGET_PROFILE,
        help="Path to the profile display JSON that should be updated in place.",
    )
    parser.add_argument(
        "--source-csv",
        type=Path,
        default=DEFAULT_SOURCE_CSV,
        help="CSV containing legacy custom field IDs (Field Name + Field ID).",
    )
    parser.add_argument(
        "--target-csv",
        type=Path,
        default=DEFAULT_TARGET_CSV,
        help="CSV containing replacement custom field IDs (Field Name + Field ID).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Optional output path. When omitted, the target profile is "
            "rewritten in place."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform all calculations but leave the target profile unchanged.",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise UpdateError(f"Missing required JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError(f"Invalid JSON in {path}: {exc}") from exc


def write_json(path: Path, content: Any) -> None:
    path.write_text(json.dumps(content, indent=2, sort_keys=False) + "\n", encoding="utf-8")


def load_field_map(csv_path: Path) -> Dict[str, int]:
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise UpdateError(f"CSV file {csv_path} has no headers")

            # Normalise header access (case-insensitive, trimmed).
            normalized_headers = {header.strip().lower(): header for header in reader.fieldnames}
            try:
                name_key = normalized_headers["field name"]
                id_key = normalized_headers["field id"]
            except KeyError as exc:
                raise UpdateError(
                    f"CSV {csv_path} must contain 'Field Name' and 'Field ID' columns"
                ) from exc

            mapping: Dict[str, int] = {}
            for row in reader:
                field_name = row.get(name_key, "").strip()
                field_id_raw = row.get(id_key, "").strip()
                if not field_name or not field_id_raw:
                    continue
                try:
                    field_id = int(field_id_raw)
                except ValueError as exc:
                    raise UpdateError(
                        f"Field ID '{field_id_raw}' in {csv_path} is not numeric"
                    ) from exc

                if field_name in mapping and mapping[field_name] != field_id:
                    raise UpdateError(
                        f"Duplicate field name '{field_name}' with conflicting IDs in {csv_path}"
                    )
                mapping[field_name] = field_id

            if not mapping:
                raise UpdateError(f"CSV {csv_path} does not contain any usable rows")
            return mapping
    except FileNotFoundError as exc:
        raise UpdateError(f"Missing required CSV file: {csv_path}") from exc


def build_id_mapping(
    source_csv: Path, target_csv: Path
) -> tuple[Dict[int, int], set[str], set[str]]:
    source_map = load_field_map(source_csv)
    target_map = load_field_map(target_csv)

    missing_in_target: set[str] = set()
    missing_in_source: set[str] = set()
    mapping: Dict[int, int] = {}

    for field_name, old_id in source_map.items():
        new_id = target_map.get(field_name)
        if new_id is None:
            missing_in_target.add(field_name)
            continue
        mapping[old_id] = new_id

    for field_name in target_map:
        if field_name not in source_map:
            missing_in_source.add(field_name)

    if not mapping:
        raise UpdateError(
            "No overlapping field names between source and target CSVs; cannot build mapping"
        )

    return mapping, missing_in_target, missing_in_source


def replace_custom_field_ids(
    obj: Any,
    id_map: Mapping[int, int],
    stats: Counter[tuple[int, int]],
    unmatched_ids: Counter[int],
) -> Any:
    if isinstance(obj, dict):
        new_dict: Dict[str, Any] = {}
        for key, value in obj.items():
            if key == "custom_field_id":
                updated, changed = _update_custom_field_id(value, id_map, stats, unmatched_ids)
                new_dict[key] = updated
            else:
                new_dict[key] = replace_custom_field_ids(value, id_map, stats, unmatched_ids)
        return new_dict
    if isinstance(obj, list):
        return [replace_custom_field_ids(item, id_map, stats, unmatched_ids) for item in obj]
    return obj


def _update_custom_field_id(
    value: Any,
    id_map: Mapping[int, int],
    stats: Counter[tuple[int, int]],
    unmatched_ids: Counter[int],
) -> tuple[Any, bool]:
    numeric_value: int | None = None
    if isinstance(value, int):
        numeric_value = value
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            numeric_value = int(stripped)
        else:
            return value, False
    else:
        return value, False

    new_value = id_map.get(numeric_value)
    if new_value is None:
        unmatched_ids[numeric_value] += 1
        return value, False

    stats[(numeric_value, new_value)] += 1
    if isinstance(value, int):
        return new_value, True
    return str(new_value), True


def summarise_results(
    mapping: Mapping[int, int],
    stats: Counter[tuple[int, int]],
    unmatched_ids: Counter[int],
    missing_in_target: set[str],
    missing_in_source: set[str],
) -> str:
    lines = []
    total_updates = sum(stats.values())
    lines.append(f"Updated {total_updates} custom_field_id values across {len(stats)} unique mappings.")

    if stats:
        sample = ", ".join(f"{old}->{new} ({count})" for (old, new), count in stats.most_common(5))
        lines.append(f"Sample mappings: {sample}")

    unmapped_total = sum(unmatched_ids.values())
    if unmapped_total:
        ids = ", ".join(f"{id_} ({count})" for id_, count in unmatched_ids.most_common(5))
        lines.append(
            f"Encountered {unmapped_total} custom_field_id values without a mapping. "
            f"Examples: {ids}"
        )

    if missing_in_target:
        lines.append(
            "Fields missing from target CSV: " + ", ".join(sorted(missing_in_target))
        )
    if missing_in_source:
        lines.append(
            "Fields only in target CSV (no legacy match): " + ", ".join(sorted(missing_in_source))
        )

    mapped = len(mapping)
    lines.append(f"Built mapping for {mapped} field names.")
    return "\n".join(lines)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)

    # Load files to ensure they exist; source profile is referenced for parity only.
    source_profile = read_json(args.source_profile)
    target_profile = read_json(args.target_profile)
    _ = source_profile  # The structure may prove useful for future validations.

    id_map, missing_in_target, missing_in_source = build_id_mapping(
        args.source_csv, args.target_csv
    )

    stats: Counter[tuple[int, int]] = Counter()
    unmatched_ids: Counter[int] = Counter()
    updated_profile = replace_custom_field_ids(target_profile, id_map, stats, unmatched_ids)

    summary = summarise_results(
        id_map, stats, unmatched_ids, missing_in_target, missing_in_source
    )
    print(summary)

    if args.dry_run:
        return 0

    output_path = args.output or args.target_profile
    write_json(output_path, updated_profile)
    print(f"Wrote updated profile display configuration to {output_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

