#!/usr/bin/env python3
"""Synchronize ID references in the target workflow configuration.

The script compares source and target form/question libraries to build mappings
between legacy IDs and their replacements. It then walks the target workflow
configuration and swaps any outdated IDs with the ones from the target
libraries.
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
DEFAULT_TARGET_WORKFLOW = Path("target_workflow_config.json")
TARGET_BLANK_ATTR_PATTERN = re.compile(r"\s+target=\"_blank\"")


class SyncError(RuntimeError):
    """Raised when the mapping between source and target IDs cannot be built."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"Missing required file: {path}") from exc


def to_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise SyncError(f"Expected numeric ID, received {value!r}")


def build_form_map(
    source_forms: Sequence[Mapping[str, Any]],
    target_forms: Sequence[Mapping[str, Any]],
) -> Dict[int, int]:
    validate_form_entries(source_forms, context="Source forms library")
    target_by_name = index_forms_by_name(target_forms, context="Target forms library")
    form_map: Dict[int, int] = {}
    for src_form in source_forms:
        name = src_form["display_name"]
        target_form = target_by_name.get(name)
        if not target_form:
            continue
        form_map[to_int(src_form["id"])] = to_int(target_form["id"])
    return form_map


def canonicalize(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_label(label: Any) -> Any:
    if isinstance(label, str):
        return TARGET_BLANK_ATTR_PATTERN.sub("", label)
    return label


def sanitize_question(question: Mapping[str, Any]) -> str:
    payload = {key: question[key] for key in question if key != "id"}
    if "label" in payload:
        payload["label"] = normalize_label(payload["label"])
    return canonicalize(payload)


def validate_form_entries(forms: Sequence[Mapping[str, Any]], *, context: str) -> None:
    for index, form in enumerate(forms):
        if not isinstance(form, Mapping):
            raise SyncError(
                f"{context} entry at index {index} is not an object. "
                "Confirm the file is a form library export."
            )
        name = form.get("display_name")
        if not isinstance(name, str) or not name:
            raise SyncError(
                f"{context} entry at index {index} is missing a 'display_name'. "
                "Ensure you selected the form library JSON rather than the questions bank."
            )


def index_forms_by_name(forms: Sequence[Mapping[str, Any]], *, context: str) -> Dict[str, Mapping[str, Any]]:
    validate_form_entries(forms, context=context)
    return {form["display_name"]: form for form in forms}


def build_question_map(
    source_forms: Sequence[Mapping[str, Any]],
    target_forms: Sequence[Mapping[str, Any]],
    source_questions: Mapping[int, Mapping[str, Any]],
    target_questions: Mapping[int, Mapping[str, Any]],
) -> Dict[int, int]:
    validate_form_entries(source_forms, context="Source forms library")
    target_forms_by_name = index_forms_by_name(target_forms, context="Target forms library")
    question_map: Dict[int, int] = {}

    for src_form in source_forms:
        name = src_form["display_name"]
        target_form = target_forms_by_name.get(name)
        if not target_form:
            continue

        source_ids = [to_int(qid) for qid in src_form.get("data_json", {}).get("question_ids", [])]
        target_ids = [to_int(qid) for qid in target_form.get("data_json", {}).get("question_ids", [])]

        if len(source_ids) != len(target_ids):
            raise SyncError(
                f"Form '{name}' has {len(source_ids)} questions in the source but {len(target_ids)} in the target"
            )

        for src_qid, tgt_qid in zip(source_ids, target_ids):
            existing = question_map.get(src_qid)
            if existing is not None and existing != tgt_qid:
                raise SyncError(
                    f"Conflicting mappings for question {src_qid}: {existing} vs {tgt_qid}"
                )

            src_question = source_questions.get(src_qid)
            tgt_question = target_questions.get(tgt_qid)

            if src_question and tgt_question:
                src_label = normalize_label(src_question.get("label"))
                tgt_label = normalize_label(tgt_question.get("label"))
                if (
                    src_label != tgt_label
                    or src_question.get("question_type") != tgt_question.get("question_type")
                ):
                    raise SyncError(
                        "Question mismatch for form '{form}': '{src_label}' ({src_type}) vs '{tgt_label}' ({tgt_type})".format(
                            form=name,
                            src_label=src_question.get("label"),
                            src_type=src_question.get("question_type"),
                            tgt_label=tgt_question.get("label"),
                            tgt_type=tgt_question.get("question_type"),
                        )
                    )
            elif src_question or tgt_question:
                # If only one side is present, we cannot validate the content but
                # still record the positional mapping for this form.
                pass

            question_map[src_qid] = tgt_qid

    return question_map


def extend_question_map_with_signatures(
    question_map: Dict[int, int],
    source_questions: Mapping[int, Mapping[str, Any]],
    target_questions: Mapping[int, Mapping[str, Any]],
) -> list[int]:
    used_target_ids: Set[int] = set(question_map.values())
    source_signature_map: Dict[str, List[int]] = defaultdict(list)
    target_signature_map: Dict[str, List[int]] = defaultdict(list)

    for src_id, question in source_questions.items():
        if src_id not in question_map:
            source_signature_map[sanitize_question(question)].append(src_id)

    for tgt_id, question in target_questions.items():
        if tgt_id not in used_target_ids:
            target_signature_map[sanitize_question(question)].append(tgt_id)

    unresolved: list[int] = []
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


def replace_ids(
    obj: Any,
    form_map: Mapping[int, int],
    question_map: Mapping[int, int],
    stats: Counter,
) -> Any:
    if isinstance(obj, dict):
        return {key: replace_ids(value, form_map, question_map, stats) for key, value in obj.items()}
    if isinstance(obj, list):
        return [replace_ids(item, form_map, question_map, stats) for item in obj]

    def update_numeric(value: int) -> int:
        new_value = question_map.get(value, value)
        # In case the new value itself needs mapping (e.g., chained updates)
        new_value = question_map.get(new_value, new_value)
        new_value = form_map.get(new_value, new_value)
        return new_value

    if isinstance(obj, int):
        new_value = update_numeric(obj)
        if new_value != obj:
            stats[(obj, new_value)] += 1
        return new_value

    if isinstance(obj, str):
        if obj.isdigit():
            numeric = int(obj)
            new_value = update_numeric(numeric)
            if new_value != numeric:
                stats[(numeric, new_value)] += 1
                return str(new_value)
            return obj

        def repl(match: re.Match[str]) -> str:
            numeric = int(match.group(1))
            new_value = update_numeric(numeric)
            if new_value != numeric:
                stats[(numeric, new_value)] += 1
                return str(new_value)
            return match.group(0)

        updated, count = re.subn(r"(?<!\d)(\d+)(?!\d)", repl, obj)
        if count > 0:
            return updated
        return obj

    return obj


def sync_ids(
    source_forms_path: Path,
    target_forms_path: Path,
    source_questions_path: Path,
    target_questions_path: Path,
    target_workflow_path: Path,
    write: bool = True,
) -> Counter:
    source_forms = load_json(source_forms_path)
    target_forms = load_json(target_forms_path)
    source_questions_list = load_json(source_questions_path)
    target_questions_list = load_json(target_questions_path)
    workflow = load_json(target_workflow_path)

    form_map = build_form_map(source_forms, target_forms)
    source_questions = {to_int(q["id"]): q for q in source_questions_list}
    target_questions = {to_int(q["id"]): q for q in target_questions_list}
    question_map = build_question_map(source_forms, target_forms, source_questions, target_questions)
    unresolved = extend_question_map_with_signatures(question_map, source_questions, target_questions)
    if unresolved:
        preview = ", ".join(str(item) for item in sorted(unresolved)[:10])
        suffix = " ..." if len(unresolved) > 10 else ""
        raise SyncError(
            "Unable to find target questions matching the following source IDs by content: "
            f"{preview}{suffix}"
        )

    stats: Counter = Counter()
    updated_workflow = replace_ids(workflow, form_map, question_map, stats)

    if write and stats:
        original_text = target_workflow_path.read_text(encoding="utf-8")
        updated_text = original_text
        for (old, new), count in sorted(stats.items()):
            pattern = re.compile(rf"(?<!\\d){old}(?!\\d)")
            updated_text, replaced = pattern.subn(str(new), updated_text)
            if replaced != count:
                raise SyncError(
                    f"Expected to replace {count} occurrences of {old} but replaced {replaced}"
                )

        # Validate that textual replacements yield the same structure as the computed update.
        if json.loads(updated_text) != updated_workflow:
            raise SyncError("Text replacements did not produce the expected workflow configuration")

        target_workflow_path.write_text(updated_text, encoding="utf-8")
    elif write:
        target_workflow_path.write_text(json.dumps(updated_workflow, indent=2), encoding="utf-8")

    return stats


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync IDs in the target workflow configuration")
    parser.add_argument("--source-forms", type=Path, default=DEFAULT_SOURCE_FORMS)
    parser.add_argument("--target-forms", type=Path, default=DEFAULT_TARGET_FORMS)
    parser.add_argument("--source-questions", type=Path, default=DEFAULT_SOURCE_QUESTIONS)
    parser.add_argument("--target-questions", type=Path, default=DEFAULT_TARGET_QUESTIONS)
    parser.add_argument("--target-workflow", type=Path, default=DEFAULT_TARGET_WORKFLOW)
    parser.add_argument(
        "--dry-run", action="store_true", help="Only report replacements without writing any files"
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    stats = sync_ids(
        args.source_forms,
        args.target_forms,
        args.source_questions,
        args.target_questions,
        args.target_workflow,
        write=not args.dry_run,
    )

    if not stats:
        print("No IDs required updating.")
        return

    print("Updated IDs:")
    for (old, new), count in sorted(stats.items()):
        print(f"  {old} -> {new} ({count} occurrences)")


if __name__ == "__main__":
    main()
