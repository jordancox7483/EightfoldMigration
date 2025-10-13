# Eightfold Migration Utilities

> **Looking for a quick start?** A Windows executable (`EightfoldMigrationHelper.exe`) is published on the releases page. It is provided for personal use, is alpha quality, and may not behave exactly like the scripts. If you hit issues, fall back to the raw Python utilities. See [GUI_USAGE.md](GUI_USAGE.md) for setup, usage, and build steps.

# Workflow ID Sync Utilities

This repository contains some small Python scripts that help reconcile form and question IDs between environments when migrating configuration data. Both scripts work with the raw JSON from config.

- `form_and_question_id_updater.py` — Updates form/question IDs across JSON, including IDs embedded inside strings such as templated headers.
- `dependency_question_id_updater.py` — Repairs question dependency references inside the form and question libraries after IDs are migrated.
- `report_workflow_id_mappings.py` — Produces CSV tables summarising every form and question mapping, which you can use for manual VLOOKUPs or spot checks.
- `custom_fields_scraper.py`, Exports the field names and IDs from the Eightfold Custom Fields integration page. The script automates pagination and writes the results to a CSV file.  Needs login info (pass as variables DO NOT store in file) and confirm EF URL - for both source and target (run twice, inform script of the desired output name, the next script below uses those outputs so probably stick with "source.csv" and "target.csv")
- `custom_field_id_updater.py` Rewrites the `custom_field_id` values embedded in a JSON configuration. It compares the legacy IDs from a "source" CSV to the replacement IDs in a "target" CSV, then walks the target profile JSON and swaps anything it recognises.

The sections below walk through installing prerequisites, opening the project in Visual Studio Code, and running each script with new exports.

This was originally built based on Pipeline and Worflow (workflow_config) migration so there are a lot of references to "workflow" in the files and code but this should only need the raw JSON - it does need to be named "target_workflow_config.json" unless you pass alternatives to the script or update the script.

---

## 1. Prepare your Python environment

1. Install [Python 3.11 or later](https://www.python.org/downloads/). During installation, make sure the optional step to “Add Python to PATH” is enabled.
2. (Optional but recommended) Create a virtual environment so the dependencies for this project are isolated from the rest of your system:
   ```bash
   python -m venv .venv
   ```
3. Activate the virtual environment before running either script:
   - **macOS/Linux**
     ```bash
     source .venv/bin/activate
     ```
   - **Windows (PowerShell)**
     ```powershell
     .venv\Scripts\Activate.ps1
     ```
4. Install the minimal dependencies listed in `test/requirements.txt` (only `rich` is currently needed for nicer console tables):
   ```bash
   pip install -r test/requirements.txt
   ```
5. If you plan to use the Eightfold custom field scraper, install Playwright and its Chromium browser once inside the virtual environment:
   ```bash
   pip install playwright
   playwright install chromium
   ```

> **Tip:** If you ever need to update dependencies in the future, re-run the `pip install` command after activating the virtual environment.

---

## 2. Open the project in VS Code

1. Launch Visual Studio Code and choose **File → Open Folder…**, then select the `codex` directory.
2. When prompted, allow VS Code to trust the folder.
3. Install the **Python** extension from Microsoft if you have not already (VS Code will usually prompt you).
4. Select the virtual environment you created earlier as the interpreter:
   - Press `Ctrl+Shift+P` (or `Cmd+Shift+P` on macOS) to open the Command Palette.
   - Type “Python: Select Interpreter” and choose the entry that points to `.venv` inside this project.
5. Open the built-in terminal (**View → Terminal**) so commands run within the workspace and inherit the selected interpreter.

> VS Code remembers the interpreter and terminal environment per-workspace, so after the first setup you should only need to activate the terminal and run the scripts.

---

## 3. Preparing the JSON exports

Each run of the scripts consumes five JSON files:

| File | Description |
| --- | --- |
| `source_forms_library.json` | Forms from the environment you exported (e.g. QA). |
| `target_forms_library.json` | Forms from the destination environment (e.g. Production Dry Run). |
| `source_questions_bank.json` | Question bank from the source environment. |
| `target_questions_bank.json` | Question bank from the destination environment. |
| `target_workflow_config.json` | JSON configuration you want to update to use the new IDs.  Originally built for Pipeline and Worflow (workflow_config) so there are a lot of references to "workflow" in the files and code but this is just the raw JSON |

By default the scripts look for files with the exact names above in the repository root. If you download fresh exports, simply overwrite the existing files or place the new ones alongside them with the same names.

If you prefer to keep dated filenames, both scripts provide flags to point to alternate paths (covered in the usage sections below).

---

## 4. Running `form_and_question_id_updater.py`

This script rewrites `target_workflow_config.json` in place, replacing any form or question IDs that appear in the source export with the corresponding IDs from the target export.

### Basic usage (default filenames)

1. Ensure the virtual environment is active in your VS Code terminal.
2. Run the script:
   ```bash
   python form_and_question_id_updater.py
   ```
3. The script prints a summary of each replacement (form IDs, question IDs, and totals) and updates `target_workflow_config.json` on disk. It now also scans strings (for example `"{{ form_submission.get_response_field('123') }}"`) and replaces any embedded numeric IDs it recognises.

### Dry-run mode

If you want to preview what would change without editing the workflow file, add `--dry-run`:
```bash
python form_and_question_id_updater.py --dry-run
```
Dry-run mode prints the same tables and totals but leaves the JSON untouched.

### Custom file paths

If your exports have different filenames or live elsewhere, pass explicit paths:
```bash
python form_and_question_id_updater.py \
    --source-forms data/qa_forms.json \
    --target-forms data/prod_forms.json \
    --source-questions data/qa_questions.json \
    --target-questions data/prod_questions.json \
    --target-workflow configs/workflow.json
```

### Troubleshooting tips

- **Missing files:** The script validates that each file exists and stops with a readable error if one is missing. Double-check the paths you passed.
- **Mismatched forms or questions:** If a form label or question label exists in the source data but not in the target, the script highlights the mismatch so you can investigate before deploying.
- **Git safety:** Because the workflow file is version-controlled, you can always review the changes in VS Code’s Source Control panel before committing.
- **Windows encoding:** Some Eightfold exports include characters outside Windows’ default code page. If you hit a `UnicodeDecodeError`, rerun with `python -X utf8 form_and_question_id_updater.py ...` to force UTF-8 decoding.

---

## 5. Updating question dependencies inside form/question libraries

`dependency_question_id_updater.py` scans the target form library and question bank for legacy question IDs that still reference the source environment (for example in `question_dependencies`, `child_question_ids`, or templated strings). It builds a mapping between legacy and replacement question IDs by comparing the source and target exports, then rewrites every occurrence so that dependencies point at the new IDs.

### When to use it

Run this script after you have already synced the core form/question IDs and notice that dependency blocks still contain old question IDs. It is especially helpful for:

- `question_dependencies` entries that refer to a parent question by ID.
- Any nested `child_question_ids` collections.
- Free-form strings that interpolate IDs such as `{{ formResponse<1970359196716205> }}`.

### Basic usage (default filenames)

```bash
python dependency_question_id_updater.py
```

By default the script reads the four JSON exports in the repository root (`source_forms_library.json`, `target_forms_library.json`, `source_questions_bank.json`, `target_questions_bank.json`) and writes updated copies alongside them as `Updated_target_forms_library.json` and `Updated_target_questions_bank.json`.

It prints a summary of how many IDs were remapped and the most frequent replacements so you can sanity-check the results.

### Custom file paths

You can override both the inputs and the output locations:

```powershell
python dependency_question_id_updater.py \
  --source-forms "C:\Exports\source_forms_library.json" \
  --target-forms "C:\Exports\target_forms_library.json" \
  --source-questions "C:\Exports\source_questions_bank.json" \
  --target-questions "C:\Exports\target_questions_bank.json" \
  --updated-forms "C:\Users\Jordan\Downloads\Updated_target_forms_library.json" \
  --updated-questions "C:\Users\Jordan\Downloads\Updated_target_questions_bank.json"
```

Add `--dry-run` if you want to preview the replacement counts without writing the output files.

> Tip: keep the generated files under version control (or compare with the originals) before rolling them into downstream systems, so you can spot-check any large batches of changes.

---

## 6. Running `report_workflow_id_mappings.py`

Use this script when you want a CSV-style report for manual reconciliation (for example, to copy into Excel and run a VLOOKUP).

### Basic usage (default filenames)

```bash
python report_workflow_id_mappings.py
```

The script prints:

1. A table of form label → source ID → target ID mappings.
2. A table of question label → source ID → target ID mappings.
3. A list of form labels that exist in the source export but not in the target (if any).

You can redirect the output to a file to keep it handy:
```bash
python report_workflow_id_mappings.py > workflow_id_mappings.csv
```
The generated file is plain text with comma-separated columns that can be pasted into spreadsheets.

### Custom file paths

The same flags as the sync script are available, minus the workflow file:
```bash
python report_workflow_id_mappings.py \
    --source-forms data/qa_forms.json \
    --target-forms data/prod_forms.json \
    --source-questions data/qa_questions.json \
    --target-questions data/prod_questions.json
```

---

## 7. Keeping everything up to date

- Whenever you receive new exports, replace the JSON files (or pass new paths) and rerun whichever script you need.
- Use Git to track and review changes:
  ```bash
  git status
  git diff target_workflow_config.json
  ```
  VS Code’s Source Control view shows the same information with a visual diff.
- Commit your changes once you are satisfied:
  ```bash
  git add target_workflow_config.json
  git commit -m "Update workflow IDs for YYYY-MM-DD export"
  ```

---

## 8. Scraping Eightfold custom fields

The repository now includes `custom_fields_scraper.py`, a helper that exports the field names and IDs from the Eightfold Custom Fields integration page. The script automates pagination and writes the results to a CSV file.

### Usage

1. Ensure Playwright is installed (see step 5 above) and activate your virtual environment.
2. Run the scraper, passing the tenant subdomain (the `app-wu` portion of the URL) and an optional output filename:
   ```bash
   python custom_fields_scraper.py --subdomain app-wu --output my_fields.csv
   ```
3. When prompted, enter the Eightfold username and password that have access to the integrations console. Credentials are requested at runtime so they are not stored in the repository or shell history.
4. The script launches a Chromium browser, signs in, iterates through every page of results, and saves the output CSV.

> Tip: For troubleshooting (for example, to confirm the correct login selectors for your tenant), append `--headed` to watch the automated session. Use `--headless` if you want to force headless mode explicitly.

---

## 9. Updating profile display custom field IDs

`custom_field_id_updater.py` rewrites the `custom_field_id` values embedded in a profile display configuration. It compares the legacy IDs from a "source" CSV to the replacement IDs in a "target" CSV, then walks the target profile JSON and swaps anything it recognises.

### Basic usage

If your files live in the project root and use the default names (`source_profile_display.json`, `target_profile_display.json`, `source.csv`, `target.csv`), run:

```bash
python custom_field_id_updater.py
```

When your exports live elsewhere (for example, in `C:\Users\Jordan\Downloads`), pass explicit paths:

```powershell
python custom_field_id_updater.py \
  --source-profile "C:\Users\Jordan\Downloads\source_profile_display.json" \
  --target-profile "C:\Users\Jordan\Downloads\target_profile_display.json" \
  --source-csv "C:\Users\Jordan\Downloads\source.csv" \
  --target-csv "C:\Users\Jordan\Downloads\target.csv"
```

The script prints a summary of how many IDs were updated, highlights any custom fields that were present in the source export but missing from the target export (and vice-versa), and calls out legacy IDs it could not map. Use these diagnostics to confirm whether you need to re-export the CSVs or manually handle the remaining fields.

Add `--dry-run` to preview the summary without editing the JSON. To write the output to a separate file instead of overwriting the target profile, provide `--output path\to\profile.json`.


## 10. Quick reference

| Task | Command |
| --- | --- |
| Activate virtual environment (macOS/Linux) | `source .venv/bin/activate` |
| Activate virtual environment (Windows PowerShell) | `.venv\Scripts\Activate.ps1` |
| Install dependencies | `pip install -r test/requirements.txt` |
| Run form/question updater | `python form_and_question_id_updater.py` |
| Run form/question updater (dry run) | `python form_and_question_id_updater.py --dry-run` |
| Update dependency question IDs | `python dependency_question_id_updater.py` |
| Update dependency question IDs (dry run) | `python dependency_question_id_updater.py --dry-run` |
| Run report script | `python report_workflow_id_mappings.py` |
| Export report to file | `python report_workflow_id_mappings.py > workflow_id_mappings.csv` |
| Update profile display IDs | `python custom_field_id_updater.py --dry-run` |

Keep this README open in VS Code (pin the tab) so the step-by-step instructions are always within reach when you revisit the project.
