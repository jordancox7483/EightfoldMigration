#!/usr/bin/env python3
"""Graphical front-end for the Eightfold migration utilities.

This module exposes a Tkinter-based user interface that orchestrates the
existing command-line scripts in this repository.  It allows non-technical
users to:

* Select the source/target form and question exports.
* Run the custom fields scraper with stored credentials.
* Choose the generated CSVs for the custom field updater.
* Produce updated form/question libraries.
* Rewrite a target configuration JSON with both ID migration passes.

The UI is designed so that it can be frozen into an executable via
PyInstaller.  When bundled, all logic runs in-process—no external Python
installation is required on the target machine.
"""

from __future__ import annotations

import io
import os
import queue
import shutil
import threading
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import custom_field_id_updater
import custom_fields_scraper
import dependency_question_id_updater
import form_and_question_id_updater


# Ensure Playwright downloads live alongside the packaged executable.  During
# development this directory may not exist yet; build instructions cover
# seeding it before running PyInstaller.
DEFAULT_BROWSER_DIR = Path(__file__).resolve().parent / "playwright-browsers"
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(DEFAULT_BROWSER_DIR))


@dataclass
class FileSelection:
    """Tracks a labelled file selection entry."""

    label: str
    variable: tk.StringVar
    filetypes: tuple[tuple[str, str], ...] = (("JSON files", "*.json"), ("All files", "*.*"))


class MigrationApp(tk.Tk):
    """Main Tkinter application window."""

    def __init__(self) -> None:
        super().__init__()
        self.title("Eightfold Migration Helper")
        self.minsize(900, 680)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self._running_task = False

        self._build_variables()
        self._build_widgets()
        self.after(100, self._process_log_queue)

    # ------------------------------------------------------------------ UI setup
    def _build_variables(self) -> None:
        self.source_questions_var = tk.StringVar()
        self.source_forms_var = tk.StringVar()
        self.target_questions_var = tk.StringVar()
        self.target_forms_var = tk.StringVar()
        self.target_json_var = tk.StringVar()

        self.scraper_url_var = tk.StringVar()
        self.scraper_username_var = tk.StringVar()
        self.scraper_password_var = tk.StringVar()
        self.scraper_output_dir_var = tk.StringVar()
        self.scraper_output_name_var = tk.StringVar(value="source.csv")

        self.custom_source_csv_var = tk.StringVar()
        self.custom_target_csv_var = tk.StringVar()

        self.updated_libraries_dir_var = tk.StringVar()
        self.updated_json_dir_var = tk.StringVar()

        self.status_var = tk.StringVar()

    def _build_widgets(self) -> None:
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        canvas = tk.Canvas(main, borderwidth=0)
        scroll_frame = ttk.Frame(canvas)
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")

        def on_configure(event: tk.Event[tk.Frame]) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        scroll_frame.bind("<Configure>", on_configure)

        # Section 1: Required JSON files.
        ttk.Label(scroll_frame, text="Core JSON Inputs", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        file_entries = [
            FileSelection("Source questions bank", self.source_questions_var),
            FileSelection("Source forms library", self.source_forms_var),
            FileSelection("Target questions bank", self.target_questions_var),
            FileSelection("Target forms library", self.target_forms_var),
            FileSelection("Target configuration JSON", self.target_json_var),
        ]
        for idx, selection in enumerate(file_entries, start=1):
            self._add_file_picker(scroll_frame, idx, selection)

        row_offset = len(file_entries) + 2

        ttk.Separator(scroll_frame).grid(row=row_offset - 1, column=0, columnspan=3, sticky="ew", pady=10)

        # Section 2: Custom fields scraper.
        ttk.Label(scroll_frame, text="Custom Fields Scraper", font=("Segoe UI", 11, "bold")).grid(
            row=row_offset, column=0, sticky="w", pady=(0, 4)
        )
        row = row_offset + 1
        self._add_labeled_entry(scroll_frame, row, "Custom fields page URL", self.scraper_url_var, width=70)
        row += 1
        self._add_labeled_entry(scroll_frame, row, "Username", self.scraper_username_var, width=40)
        row += 1
        self._add_labeled_entry(
            scroll_frame, row, "Password", self.scraper_password_var, width=40, show="*"
        )
        row += 1
        self._add_directory_picker(
            scroll_frame,
            row,
            "Scraper output folder",
            self.scraper_output_dir_var,
        )
        row += 1
        self._add_labeled_entry(
            scroll_frame,
            row,
            "Output filename",
            self.scraper_output_name_var,
            width=35,
            tooltip="Name of the CSV created by the scraper (e.g. source.csv, target.csv).",
        )
        row += 1

        ttk.Label(
            scroll_frame,
            text=(
                "Tip: run the scraper twice. Export the source environment first and save it as "
                "'source.csv', then export the target environment and save it as 'target.csv'. "
                "Those files feed into custom_field_id_updater.py."
            ),
            wraplength=680,
            foreground="#555555",
        ).grid(row=row, column=0, sticky="w", pady=(2, 8))
        row += 1

        scraper_btn = ttk.Button(scroll_frame, text="Run custom_fields_scraper.py", command=self._trigger_scraper)
        scraper_btn.grid(row=row, column=0, sticky="w", pady=(6, 2))
        row += 1

        ttk.Label(
            scroll_frame,
            text="Select the generated CSVs for custom_field_id_updater.py",
        ).grid(row=row, column=0, sticky="w", pady=(10, 2))
        row += 1
        self._add_file_picker(
            scroll_frame,
            row,
            FileSelection(
                "Source custom fields CSV",
                self.custom_source_csv_var,
                (("CSV files", "*.csv"), ("All files", "*.*")),
            ),
        )
        row += 1
        self._add_file_picker(
            scroll_frame,
            row,
            FileSelection(
                "Target custom fields CSV",
                self.custom_target_csv_var,
                (("CSV files", "*.csv"), ("All files", "*.*")),
            ),
        )
        row += 1

        ttk.Separator(scroll_frame).grid(row=row, column=0, columnspan=3, sticky="ew", pady=10)
        row += 1

        # Section 3: Outputs.
        ttk.Label(scroll_frame, text="ID Migration Outputs", font=("Segoe UI", 11, "bold")).grid(
            row=row, column=0, sticky="w", pady=(0, 4)
        )
        row += 1

        self._add_directory_picker(
            scroll_frame,
            row,
            "Updated forms/questions output folder",
            self.updated_libraries_dir_var,
        )
        row += 1

        libraries_btn = ttk.Button(
            scroll_frame,
            text="Generate updated form & question libraries",
            command=self._trigger_libraries_update,
        )
        libraries_btn.grid(row=row, column=0, sticky="w", pady=(6, 2))
        row += 1

        ttk.Label(scroll_frame, text="Custom JSON updates", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w", pady=(12, 3)
        )
        row += 1

        self._add_directory_picker(
            scroll_frame,
            row,
            "Updated JSON output folder",
            self.updated_json_dir_var,
        )
        row += 1

        json_btn = ttk.Button(
            scroll_frame,
            text="Update selected JSON with form/custom field mappings",
            command=self._trigger_json_update,
        )
        json_btn.grid(row=row, column=0, sticky="w", pady=(6, 10))
        row += 1

        # Log output
        ttk.Label(scroll_frame, text="Activity log", font=("Segoe UI", 10, "bold")).grid(
            row=row, column=0, sticky="w"
        )
        row += 1
        self.log_text = tk.Text(scroll_frame, height=12, wrap="word", state="disabled")
        self.log_text.grid(row=row, column=0, sticky="nsew")
        scroll_frame.grid_columnconfigure(0, weight=1)
        row += 1

        status_frame = ttk.Frame(main)
        status_frame.pack(fill="x", pady=(8, 0))
        ttk.Label(status_frame, textvariable=self.status_var).pack(side="left", padx=6)

    def _add_file_picker(self, parent: ttk.Frame, row: int, selection: FileSelection) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=2)
        ttk.Label(frame, text=selection.label, width=28, anchor="w").pack(side="left")
        entry = ttk.Entry(frame, textvariable=selection.variable, width=70)
        entry.pack(side="left", padx=(4, 4), fill="x", expand=True)

        def browse() -> None:
            initial = selection.variable.get() or ""
            file_path = filedialog.askopenfilename(
                title=f"Select {selection.label}",
                initialfile=Path(initial).name if initial else None,
                filetypes=list(selection.filetypes),
            )
            if file_path:
                selection.variable.set(file_path)

        ttk.Button(frame, text="Browse…", command=browse).pack(side="left")

    def _add_directory_picker(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
    ) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=2)
        ttk.Label(frame, text=label, width=28, anchor="w").pack(side="left")
        entry = ttk.Entry(frame, textvariable=variable, width=70)
        entry.pack(side="left", padx=(4, 4), fill="x", expand=True)

        def browse() -> None:
            directory = filedialog.askdirectory(title=f"Select {label}")
            if directory:
                variable.set(directory)

        ttk.Button(frame, text="Browse…", command=browse).pack(side="left")

    def _add_labeled_entry(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        *,
        width: int = 60,
        show: str | None = None,
        tooltip: str | None = None,
    ) -> None:
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=0, sticky="ew", pady=2)
        ttk.Label(frame, text=label, width=28, anchor="w").pack(side="left")
        entry = ttk.Entry(frame, textvariable=variable, width=width, show=show)
        entry.pack(side="left", padx=(4, 0), fill="x", expand=True)
        if tooltip:
            _Tooltip(entry, tooltip)

    # ------------------------------------------------------------------ Logging helpers
    def append_log(self, message: str) -> None:
        self.log_queue.put(message.rstrip())

    def _process_log_queue(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.after(120, self._process_log_queue)

    def _capture_output(self, func: Callable[..., object], *args, **kwargs) -> object:
        """Run *func* capturing stdout/stderr and forward to the log."""
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            result = func(*args, **kwargs)
        output = buffer.getvalue().strip()
        if output:
            for line in output.splitlines():
                self.append_log(line)
        return result

    # ------------------------------------------------------------------ Validation
    def _require_path(self, label: str, value: str) -> Path:
        path = Path(value)
        if not value:
            raise ValueError(f"{label} is required.")
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {value}")
        return path

    def _ensure_directory(self, label: str, value: str) -> Path:
        if not value:
            raise ValueError(f"{label} is required.")
        path = Path(value)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_in_thread(self, task: Callable[[], None]) -> None:
        if self._running_task:
            messagebox.showinfo("Task in progress", "Please wait for the current task to finish.")
            return

        def wrapper() -> None:
            try:
                task()
                self.append_log("Task completed successfully.")
            except Exception as exc:  # noqa: BLE001 - show message in log
                self.append_log(f"ERROR: {exc}")
                self.append_log("Task aborted.")
            finally:
                self.status_var.set("")
                self._running_task = False

        self._running_task = True
        self.status_var.set("Running…")
        threading.Thread(target=wrapper, daemon=True).start()

    # ------------------------------------------------------------------ Task triggers
    def _trigger_scraper(self) -> None:
        def task() -> None:
            url = self.scraper_url_var.get().strip()
            username = self.scraper_username_var.get().strip()
            password = self.scraper_password_var.get()
            filename = self.scraper_output_name_var.get().strip() or "custom_fields.csv"

            if not url:
                raise ValueError("Custom fields URL is required.")
            if not username:
                raise ValueError("Username is required for the scraper.")
            if not password:
                raise ValueError("Password is required for the scraper.")

            output_dir = self._ensure_directory("Scraper output folder", self.scraper_output_dir_var.get())
            output_path = output_dir / filename

            argv = [
                "--url",
                url,
                "--username",
                username,
                "--password",
                password,
                "--output",
                str(output_path),
                "--headless",
            ]
            self.append_log(f"Running custom_fields_scraper.py -> {output_path}")
            exit_code = self._capture_output(custom_fields_scraper.main, argv)
            if exit_code not in (0, None):
                raise RuntimeError(f"custom_fields_scraper.py failed with exit code {exit_code}")

        self._run_in_thread(task)

    def _trigger_libraries_update(self) -> None:
        def task() -> None:
            source_forms = self._require_path("Source forms library", self.source_forms_var.get())
            target_forms = self._require_path("Target forms library", self.target_forms_var.get())
            source_questions = self._require_path("Source questions bank", self.source_questions_var.get())
            target_questions = self._require_path("Target questions bank", self.target_questions_var.get())
            target_json = self._require_path("Target configuration JSON", self.target_json_var.get())
            output_dir = self._ensure_directory(
                "Updated forms/questions output folder", self.updated_libraries_dir_var.get()
            )

            # Run form_and_question_id_updater in dry-run mode to validate mappings.
            self.append_log("Validating mappings via form_and_question_id_updater.py (dry run)…")
            stats = self._capture_output(
                form_and_question_id_updater.sync_ids,
                source_forms,
                target_forms,
                source_questions,
                target_questions,
                target_json,
                False,
            )
            total_updates = sum(stats.values())
            self.append_log(f"form_and_question_id_updater identified {total_updates} workflow references.")

            forms_output = output_dir / f"Updated_{target_forms.name}"
            questions_output = output_dir / f"Updated_{target_questions.name}"

            argv = [
                "--source-forms",
                str(source_forms),
                "--target-forms",
                str(target_forms),
                "--source-questions",
                str(source_questions),
                "--target-questions",
                str(target_questions),
                "--updated-forms",
                str(forms_output),
                "--updated-questions",
                str(questions_output),
            ]
            self.append_log(
                f"Writing updated form/question libraries to {forms_output.name} and {questions_output.name}"
            )
            self._capture_output(dependency_question_id_updater.main, argv)

        self._run_in_thread(task)

    def _trigger_json_update(self) -> None:
        def task() -> None:
            source_forms = self._require_path("Source forms library", self.source_forms_var.get())
            target_forms = self._require_path("Target forms library", self.target_forms_var.get())
            source_questions = self._require_path("Source questions bank", self.source_questions_var.get())
            target_questions = self._require_path("Target questions bank", self.target_questions_var.get())
            target_json = self._require_path("Target configuration JSON", self.target_json_var.get())
            output_dir = self._ensure_directory("Updated JSON output folder", self.updated_json_dir_var.get())

            source_csv = self._require_path("Source custom fields CSV", self.custom_source_csv_var.get())
            target_csv = self._require_path("Target custom fields CSV", self.custom_target_csv_var.get())

            output_json = output_dir / f"Updated_{Path(target_json).name}"
            shutil.copy2(target_json, output_json)
            self.append_log(f"Copied {target_json} -> {output_json}")

            self.append_log("Applying form_and_question_id_updater.py to the copied JSON…")
            stats = self._capture_output(
                form_and_question_id_updater.sync_ids,
                source_forms,
                target_forms,
                source_questions,
                target_questions,
                output_json,
                True,
            )
            total_updates = sum(stats.values())
            self.append_log(f"Updated {total_updates} workflow ID references in {output_json.name}.")

            argv: list[str] = [
                "--source-profile",
                str(target_json),
                "--target-profile",
                str(output_json),
                "--source-csv",
                str(source_csv),
                "--target-csv",
                str(target_csv),
                "--output",
                str(output_json),
            ]
            self.append_log("Applying custom_field_id_updater.py with selected CSV mappings…")
            exit_code = self._capture_output(custom_field_id_updater.main, argv)
            if exit_code not in (0, None):
                raise RuntimeError(f"custom_field_id_updater.py failed with exit code {exit_code}")

            self.append_log(f"Final JSON written to {output_json}")

        self._run_in_thread(task)


class _Tooltip:
    """Lightweight tooltip helper for Tkinter widgets."""

    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.tipwindow: tk.Toplevel | None = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event: tk.Event[tk.Widget]) -> None:
        if self.tipwindow:
            return
        x, y, _, height = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 20
        y += self.widget.winfo_rooty() + height + 12
        self.tipwindow = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = ttk.Label(
            tw,
            text=self.text,
            justify="left",
            relief="solid",
            borderwidth=1,
            padding=(4, 2),
            background="#ffffe0",
        )
        label.pack(ipadx=1)

    def hide(self, _event: tk.Event[tk.Widget]) -> None:
        if self.tipwindow:
            self.tipwindow.destroy()
            self.tipwindow = None


def launch() -> None:
    """Entry point compatible with PyInstaller."""
    app = MigrationApp()
    app.mainloop()


if __name__ == "__main__":
    launch()
