# Civitai Model Version Trends

This project collects Civitai model-version trend data from the public REST API.

The primary metric is `new_version_count`: the number of `modelVersion.id` values first observed on a given day. New model counts and active totals are stored as secondary metrics, but version activity is the main signal. `active_total` is best-effort and remains empty when the Civitai API response does not include `metadata.totalItems`.

## Quick Start

Run a small local collection:

```bash
python scripts/collect_daily.py --max-pages 1 --skip-active-totals
```

Export daily, weekly, and monthly CSV files:

```bash
python scripts/export_counts.py
```

Generate a simple SVG trend plot:

```bash
python scripts/plot_trends.py
```

## Data Files

```text
data/civitai.sqlite
data/daily_counts.csv
data/errors.csv
data/exports/base_model_audit.csv
data/exports/weekly_counts.csv
data/exports/monthly_counts.csv
data/exports/trends.svg
```

`base_model_raw` is preserved exactly as returned by Civitai. Use `config/base_model_groups.yaml` to group values such as Flux, Illustrious, Z-Image, and Anima at export or plot time.

Audit observed baseModel values against the grouping file:

```bash
python scripts/audit_base_models.py
```

Use `--all --output data/exports/base_model_audit.csv` to write a full grouped/ungrouped report. Use `--fail-on-ungrouped` in CI when every observed value should be explicitly mapped.

## Automation

`.github/workflows/collect-civitai.yml` runs the collector daily and commits updated data files. It also supports manual execution through `workflow_dispatch`.

The scheduled time is UTC. The default cron uses `15:00 UTC`, which is `00:00 JST`.

The workflow runs the collector with `--fail-on-errors`, so partial API failures stop the job before updated data is committed.
