#!/usr/bin/env python3
"""Update question dependencies in target form and question libraries.

This utility builds a mapping between legacy question IDs and their replacements
in the target libraries by comparing the source and target data. It then
traverses the target form library and question bank, replacing any lingering
references (including nested structures and string templates) with the correct
target question IDs. Updated JSON files are emitted alongside the originals.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Set

DEFAULT_SOURCE_FORMS = Path("source_forms_library.json")
DEFAULT_TARGET_FORMS = Path("target_forms_library.json")
DEFAULT_SOURCE_QUESTIONS = Path("source_questions_bank.json")
DEFAULT_TARGET_QUESTIONS = Path("target_questions_bank.json")
DEFAULT_UPDATED_FORMS = Path("Updated_target_forms_library.json")
DEFAULT_UPDATED_QUESTIONS = Path("Updated_target_questions_bank.json")

NUMERIC_PATTERN = re.compile(r"(?<!\\d)(\\d+)(?!\\d)")
EMBEDDED_TEMPLATE_PATTERN = re.compile(r"(\{\{[^{}<]*<)(\d+)(>[^{}]*\}\})")


class DependencyUpdateError(RuntimeError):
    """Raised when question ID dependencies cannot be synchronized."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DependencyUpdateError(f"Missing required file: {path}") from exc


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def to_int(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise DependencyUpdateError(f"Expected numeric ID, received {value!r}")


def canonicalize(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sanitize_question(question: Mapping[str, Any]) -> str:
    payload = {key: question[key] for key in question if key != "id"}
    return canonicalize(payload)


def build_question_map(
    source_forms: Sequence[Mapping[str, Any]],
    target_forms: Sequence[Mapping[str, Any]],
    source_questions: Mapping[int, Mapping[str, Any]],
    target_questions: Mapping[int, Mapping[str, Any]],
) -> Dict[int, int]:
    target_forms_by_name = {form["display_name"]: form for form in target_forms}
    question_map: Dict[int, int] = {}

    for src_form in source_forms:
        name = src_form.get("display_name")
        target_form = target_forms_by_name.get(name)
        if not target_form:
            continue

        src_ids = [to_int(qid) for qid in src_form.get("data_json", {}).get("question_ids", [])]
        tgt_ids = [to_int(qid) for qid in target_form.get("data_json", {}).get("question_ids", [])]

        if len(tgt_ids) < len(src_ids):
            raise DependencyUpdateError(
                f"Form '{name}' has only {len(tgt_ids)} question IDs in the target but {len(src_ids)} in the source."
            )

        tgt_full_signatures: Dict[int, str] = {}
        tgt_loose_signatures: Dict[int, tuple[str | None, str | None]] = {}
        for tgt_id in tgt_ids:
            tgt_question = target_questions.get(tgt_id)
            if tgt_question:
                tgt_full_signatures[tgt_id] = sanitize_question(tgt_question)
                tgt_loose_signatures[tgt_id] = (
                    tgt_question.get("label"),
                    tgt_question.get("question_type"),
                )

        used_target_ids: Set[int] = {qid for qid in question_map.values() if qid in tgt_ids}

        for index, src_id in enumerate(src_ids):
            if src_id in question_map:
                mapped = question_map[src_id]
                if mapped in tgt_ids:
                    used_target_ids.add(mapped)
                continue

            src_question = source_questions.get(src_id)
            signature = sanitize_question(src_question) if src_question else None
            loose_signature = (
                (src_question.get("label"), src_question.get("question_type"))
                if src_question
                else None
            )
            candidate_id = None

            if index < len(tgt_ids):
                direct_candidate = tgt_ids[index]
                if direct_candidate not in used_target_ids:
                    direct_full = tgt_full_signatures.get(direct_candidate)
                    direct_loose = tgt_loose_signatures.get(direct_candidate)
                    if signature is None or direct_full == signature:
                        candidate_id = direct_candidate
                    elif loose_signature is not None and direct_loose == loose_signature:
                        candidate_id = direct_candidate

            if candidate_id is None and signature is not None:
                candidates = [
                    tgt_id
                    for tgt_id in tgt_ids
                    if tgt_id not in used_target_ids and tgt_full_signatures.get(tgt_id) == signature
                ]
                if len(candidates) == 1:
                    candidate_id = candidates[0]
                elif len(candidates) > 1:
                    raise DependencyUpdateError(
                        f"Ambiguous matches for source question {src_id} in form '{name}': {candidates}"
                    )

            if candidate_id is None and loose_signature is not None:
                candidates = [
                    tgt_id
                    for tgt_id in tgt_ids
                    if tgt_id not in used_target_ids and tgt_loose_signatures.get(tgt_id) == loose_signature
                ]
                if len(candidates) == 1:
                    candidate_id = candidates[0]
                elif len(candidates) > 1:
                    raise DependencyUpdateError(
                        f"Ambiguous matches for source question {src_id} in form '{name}': {candidates}"
                    )

            if candidate_id is None:
                raise DependencyUpdateError(
                    f"Unable to locate a matching target question for {src_id} in form '{name}'."
                )

            question_map[src_id] = candidate_id
            used_target_ids.add(candidate_id)

    return question_map

def extend_question_map_with_signatures(
    question_map: Dict[int, int],
    source_questions: Mapping[int, Mapping[str, Any]],
    target_questions: Mapping[int, Mapping[str, Any]],
) -> List[int]:
    used_target_ids: Set[int] = set(question_map.values())
    source_signature_map: Dict[str, List[int]] = defaultdict(list)
    target_signature_map: Dict[str, List[int]] = defaultdict(list)

    for src_id, question in source_questions.items():
        if src_id not in question_map:
            source_signature_map[sanitize_question(question)].append(src_id)

    for tgt_id, question in target_questions.items():
        if tgt_id not in used_target_ids:
            target_signature_map[sanitize_question(question)].append(tgt_id)

    unresolved: List[int] = []
    for signature, src_ids in source_signature_map.items():
        target_ids = target_signature_map.get(signature, [])
        available_targets = [tid for tid in target_ids if tid not in used_target_ids]
        if not available_targets:
            unresolved.extend(src_ids)
            continue

        if len(src_ids) == len(available_targets):
            for src_id, tgt_id in zip(sorted(src_ids), sorted(available_targets)):
                question_map[src_id] = tgt_id
                used_target_ids.add(tgt_id)
        elif len(src_ids) == 1:
            tgt_id = available_targets[0]
            question_map[src_ids[0]] = tgt_id
            used_target_ids.add(tgt_id)
        else:
            unresolved.extend(src_ids)

    return unresolved


def replace_dict_key(
    key: str,
    mapping: Mapping[int, int],
    stats: Counter,
    known_ids: Set[int],
    missing_ids: Set[int],
) -> str:
    if key.isdigit():
        old_id = int(key)
        new_id = mapping.get(old_id)
        if new_id is not None:
            stats[(old_id, new_id)] += 1
            return str(new_id)
        if old_id in known_ids:
            missing_ids.add(old_id)
    return key


def replace_numeric_int(
    value: int,
    mapping: Mapping[int, int],
    stats: Counter,
    known_ids: Set[int],
    missing_ids: Set[int],
) -> int:
    new_value = mapping.get(value)
    if new_value is not None:
        stats[(value, new_value)] += 1
        return new_value
    if value in known_ids:
        missing_ids.add(value)
    return value


def replace_embedded_template_ids(
    value: str,
    mapping: Mapping[int, int],
    stats: Counter,
    known_ids: Set[int],
    missing_ids: Set[int],
) -> str:
    def repl(match: re.Match[str]) -> str:
        prefix, old_id_text, suffix = match.groups()
        old_id = int(old_id_text)
        new_id = mapping.get(old_id)
        if new_id is not None:
            stats[(old_id, new_id)] += 1
            return f"{prefix}{new_id}{suffix}"
        if old_id in known_ids:
            missing_ids.add(old_id)
        return match.group(0)

    return EMBEDDED_TEMPLATE_PATTERN.sub(repl, value)


def replace_string_value(
    value: str,
    mapping: Mapping[int, int],
    stats: Counter,
    known_ids: Set[int],
    missing_ids: Set[int],
) -> str:
    if value.isdigit():
        old_id = int(value)
        new_id = mapping.get(old_id)
        if new_id is not None:
            stats[(old_id, new_id)] += 1
            return str(new_id)
        if old_id in known_ids:
            missing_ids.add(old_id)
        return value

    updated_value = replace_embedded_template_ids(value, mapping, stats, known_ids, missing_ids)
    if updated_value != value:
        value = updated_value

    def repl(match: re.Match[str]) -> str:
        old_id = int(match.group(1))
        new_id = mapping.get(old_id)
        if new_id is not None:
            stats[(old_id, new_id)] += 1
            return str(new_id)
        if old_id in known_ids:
            missing_ids.add(old_id)
        return match.group(0)

    return NUMERIC_PATTERN.sub(repl, value)


def replace_question_ids(
    obj: Any,
    mapping: Mapping[int, int],
    stats: Counter,
    known_ids: Set[int],
    missing_ids: Set[int],
) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return replace_numeric_int(obj, mapping, stats, known_ids, missing_ids)
    if isinstance(obj, str):
        return replace_string_value(obj, mapping, stats, known_ids, missing_ids)
    if isinstance(obj, list):
        return [replace_question_ids(item, mapping, stats, known_ids, missing_ids) for item in obj]
    if isinstance(obj, dict):
        updated: Dict[Any, Any] = {}
        for raw_key, value in obj.items():
            key = replace_dict_key(raw_key, mapping, stats, known_ids, missing_ids) if isinstance(raw_key, str) else raw_key
            new_value = replace_question_ids(value, mapping, stats, known_ids, missing_ids)
            if key in updated and updated[key] != new_value:
                raise DependencyUpdateError(
                    f"Conflicting values encountered when updating key '{key}'"
                )
            updated[key] = new_value
        return updated
    return obj


def summarize_stats(label: str, stats: Counter) -> None:
    total = sum(stats.values())
    if not total:
        print(f"No {label} references required updating.")
        return

    print(f"Updated {label} references: {total}")
    for (old, new), count in stats.most_common(10):
        print(f"  {old} -> {new} ({count} occurrences)")
    if len(stats) > 10:
        print(f"  ... {len(stats) - 10} additional mappings")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Update question dependencies in target form and question libraries."
    )
    parser.add_argument("--source-forms", type=Path, default=DEFAULT_SOURCE_FORMS)
    parser.add_argument("--target-forms", type=Path, default=DEFAULT_TARGET_FORMS)
    parser.add_argument("--source-questions", type=Path, default=DEFAULT_SOURCE_QUESTIONS)
    parser.add_argument("--target-questions", type=Path, default=DEFAULT_TARGET_QUESTIONS)
    parser.add_argument("--updated-forms", type=Path, default=DEFAULT_UPDATED_FORMS)
    parser.add_argument("--updated-questions", type=Path, default=DEFAULT_UPDATED_QUESTIONS)
    parser.add_argument("--dry-run", action="store_true", help="Only report changes without writing files.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    source_forms = load_json(args.source_forms)
    target_forms = load_json(args.target_forms)
    source_questions_list = load_json(args.source_questions)
    target_questions_list = load_json(args.target_questions)

    source_questions = {to_int(q["id"]): q for q in source_questions_list}
    target_questions = {to_int(q["id"]): q for q in target_questions_list}

    question_map = build_question_map(source_forms, target_forms, source_questions, target_questions)
    unresolved = extend_question_map_with_signatures(question_map, source_questions, target_questions)

    known_ids = set(source_questions)

    form_stats: Counter = Counter()
    form_missing: Set[int] = set()
    updated_forms = replace_question_ids(target_forms, question_map, form_stats, known_ids, form_missing)

    question_stats: Counter = Counter()
    question_missing: Set[int] = set()
    updated_questions = replace_question_ids(
        target_questions_list, question_map, question_stats, known_ids, question_missing
    )

    missing_ids = form_missing | question_missing
    if missing_ids:
        preview = ", ".join(str(item) for item in sorted(missing_ids)[:10])
        suffix = " ..." if len(missing_ids) > 10 else ""
        raise DependencyUpdateError(
            "Found references to source question IDs without target mappings: "
            f"{preview}{suffix}"
        )

    if not args.dry_run:
        write_json(args.updated_forms, updated_forms)
        write_json(args.updated_questions, updated_questions)
        print(f"Wrote updated forms to {args.updated_forms}")
        print(f"Wrote updated question bank to {args.updated_questions}")
    else:
        print("Dry run: no files written.")

    print(f"Mapped {len(question_map)} question IDs.")
    if unresolved:
        print(f"Skipped {len(unresolved)} source questions without a matching target counterpart.")

    summarize_stats("form", form_stats)
    summarize_stats("question", question_stats)


if __name__ == "__main__":
    main()

