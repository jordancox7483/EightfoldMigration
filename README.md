# Eightfold Migration Utilities

> **Quick start:** A Windows executable (`EightfoldMigrationHelper.exe`) is published on the releases page. It is provided for internal/personal use, is alpha quality, and may not behave exactly like the scripts. If the UI misbehaves, fall back to the raw utilities. See `GUI_USAGE.md` for UI behavior and build notes.

## What this repo contains

These scripts help reconcile IDs and migrate JSON configuration between Eightfold environments. All scripts work with the raw JSON exports from the Admin Console.

- `form_and_question_id_updater.py` - Updates form/question IDs across JSON, including IDs embedded inside strings (for example templated headers).
- `dependency_question_id_updater.py` - Repairs question dependency references inside the form and question libraries after IDs are migrated.
- `report_workflow_id_mappings.py` - Produces CSV-style tables summarizing every form and question mapping for manual review.
- `custom_fields_scraper.py` - Exports custom field names and IDs from the Custom Fields integration page into a CSV. Automates pagination; credentials are supplied at runtime.
- `custom_field_id_updater.py` - Rewrites `custom_field_id` values in a profile display JSON by mapping source CSV IDs to target CSV IDs.
- `config_json_scraper.py` - Logs into the admin console, discovers configuration pages, opens the Advanced tab when available (or direct JSON pages), and exports config JSON to per-page files.
- `config_json_discover.py` - Builds a manifest (`config_json_targets.json`) of pages that expose config JSON. Use it to refresh the target list.
- `config_json_export.py` - Reads the manifest and exports JSON for each target without re-discovering navigation.

> Note: This tooling was originally built around Pipeline and Workflow (`workflow_config`) migrations, so some file names and internal references still say "workflow". It only needs the raw JSON, but scripts default to `target_workflow_config.json` unless you supply alternative paths.

---

## Data inputs and expectations

### Form/question ID migrations
These workflows expect five JSON inputs (default names shown):
- `source_forms_library.json`
- `target_forms_library.json`
- `source_questions_bank.json`
- `target_questions_bank.json`
- `target_workflow_config.json`

If you keep different filenames or locations, the scripts accept overrides via flags.

### Custom field ID migrations
- Source and target custom field CSV exports (commonly `source.csv` and `target.csv`).
- A profile display JSON to update (source/target).

### Admin config exports
- A start URL (default `/integrations`) and credentials at runtime.
- Outputs are written to `C:\Users\Jordan\OneDrive\Documents\EightfoldCOnfigScrape` by default (overrideable).

---

## What each flow does

### Form + question ID sync
- Rewrites IDs in `target_workflow_config.json` to match the target environment.
- Also scans and updates IDs embedded in strings (templated expressions, etc.).

### Dependency repair for forms/questions
- Updates `question_dependencies`, `child_question_ids`, and any nested ID references inside the form and question libraries.
- Produces updated copies of the target form and question libraries for review.

### Mapping report
- Generates form and question mapping tables (CSV-style output) for manual review and spot checks.

### Custom fields export + remap
- Scrapes the Custom Fields admin page to produce CSV exports.
- Applies those mappings to update `custom_field_id` values inside profile display JSON.

### Admin config scrape (discover + export)
- **Discovery** crawls the admin navigation to find pages that expose JSON editors and stores them in `config_json_targets.json`.
- **Export** uses that manifest to visit each config URL and save the JSON.
- Filenames use the discovered page label; duplicate labels are disambiguated with identifying URL query parameters.
- The crawler skips UI-only sections and known non-exportable areas.

---

## Admin config scrape behavior notes

- Advanced tab is preferred when present; otherwise direct JSON editors are used.
- Known non-exportable sections are skipped (Home, Surveys, Apps, most Users & Permissions pages, and several Integration/Analytics items).
- Some pages load slowly (e.g., `workflow_config`); there are options to increase wait time or use manual gating.
- The UI includes an Admin Config Scrape section that runs discovery/export using the stored credentials and output folder.

---

## UI helper

`migration_gui.py` provides a Tkinter front-end that orchestrates the scripts and persists UI selections. See `GUI_USAGE.md` for details about what each UI button triggers and what it expects.

---
