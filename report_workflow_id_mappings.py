#!/usr/bin/env python3
"""Report the ID mappings between source and target workflow libraries.

This script inspects the source and target form/question exports and emits
CSV-friendly tables describing how IDs should be translated. Unlike
``form_and_question_id_updater.py`` it never rewrites the workflow configuration; instead it
prints the mappings so they can be applied manually (e.g. through VLOOKUP or a
text editor).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple

from form_and_question_id_updater import (
    DEFAULT_SOURCE_FORMS,
    DEFAULT_SOURCE_QUESTIONS,
    DEFAULT_TARGET_FORMS,
    DEFAULT_TARGET_QUESTIONS,
    SyncError,
    load_json,
    to_int,
)


def collect_form_rows(
    source_forms: Sequence[Mapping[str, Any]],
    target_forms: Sequence[Mapping[str, Any]],
) -> Tuple[list[tuple[int, int, str]], list[tuple[int, str]]]:
    """Return matched form mappings plus any unmatched source forms."""

    target_by_name = {form["display_name"]: form for form in target_forms}
    matches: list[tuple[int, int, str]] = []
    missing: list[tuple[int, str]] = []

    for src_form in source_forms:
        name = src_form.get("display_name", "")
        src_id = to_int(src_form["id"])
        target_form = target_by_name.get(name)
        if not target_form:
            missing.append((src_id, name))
            continue

        matches.append((src_id, to_int(target_form["id"]), name))

    return matches, missing


def collect_question_rows(
    source_forms: Sequence[Mapping[str, Any]],
    target_forms: Sequence[Mapping[str, Any]],
    source_questions: Mapping[int, Mapping[str, Any]],
    target_questions: Mapping[int, Mapping[str, Any]],
) -> list[tuple[int, int, str, str]]:
    """Return question mappings with contextual metadata.

    Each tuple contains ``(source_id, target_id, form_name, question_label)``.
    Raises :class:`SyncError` when the exports do not align.
    """

    target_forms_by_name = {form["display_name"]: form for form in target_forms}
    rows: list[tuple[int, int, str, str]] = []

    for src_form in source_forms:
        name = src_form.get("display_name", "")
        target_form = target_forms_by_name.get(name)
        if not target_form:
            # No target counterpart – questions cannot be mapped.
            continue

        source_ids = [to_int(qid) for qid in src_form.get("data_json", {}).get("question_ids", [])]
        target_ids = [to_int(qid) for qid in target_form.get("data_json", {}).get("question_ids", [])]

        if len(source_ids) != len(target_ids):
            raise SyncError(
                f"Form '{name}' has {len(source_ids)} questions in the source but {len(target_ids)} in the target"
            )

        for src_qid, tgt_qid in zip(source_ids, target_ids):
            src_question = source_questions.get(src_qid)
            tgt_question = target_questions.get(tgt_qid)

            if src_question and tgt_question:
                if (
                    src_question.get("label") != tgt_question.get("label")
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

            label = src_question.get("label") if src_question else ""
            rows.append((src_qid, tgt_qid, name, label))

    return rows


def emit_csv(title: str, headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> None:
    print(title)
    writer = csv.writer(sys.stdout)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(row)
    print()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report the ID mappings between source and target workflow exports"
    )
    parser.add_argument("--source-forms", type=Path, default=DEFAULT_SOURCE_FORMS)
    parser.add_argument("--target-forms", type=Path, default=DEFAULT_TARGET_FORMS)
    parser.add_argument("--source-questions", type=Path, default=DEFAULT_SOURCE_QUESTIONS)
    parser.add_argument("--target-questions", type=Path, default=DEFAULT_TARGET_QUESTIONS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)

    source_forms = load_json(args.source_forms)
    target_forms = load_json(args.target_forms)
    source_questions_list = load_json(args.source_questions)
    target_questions_list = load_json(args.target_questions)

    form_rows, missing_forms = collect_form_rows(source_forms, target_forms)

    source_questions: Dict[int, Mapping[str, Any]] = {to_int(q["id"]): q for q in source_questions_list}
    target_questions: Dict[int, Mapping[str, Any]] = {to_int(q["id"]): q for q in target_questions_list}
    question_rows = collect_question_rows(source_forms, target_forms, source_questions, target_questions)

    if form_rows:
        emit_csv("Form ID mappings (source_id,target_id,form_name)", ("source_id", "target_id", "form_name"), form_rows)
    else:
        print("No form mappings were detected.\n")

    if question_rows:
        emit_csv(
            "Question ID mappings (source_id,target_id,form_name,question_label)",
            ("source_id", "target_id", "form_name", "question_label"),
            question_rows,
        )
    else:
        print("No question mappings were detected.\n")

    if missing_forms:
        emit_csv(
            "Source forms without a target counterpart (source_id,form_name)",
            ("source_id", "form_name"),
            missing_forms,
        )


if __name__ == "__main__":
    try:
        main()
    except SyncError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
