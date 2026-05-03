# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Generate preview HTML (no email sent)
python3 monthly-report.py --preview

# Send email report for previous month
python3 monthly-report.py

# Send report for a specific month/year
python3 monthly-report.py --month 3 --year 2025
```

No build step, no test suite — this is a standalone Python script.

## Architecture

Single-file monolith (`monthly-report.py`, ~1920 lines). The pipeline runs linearly:

1. **Config loading** — reads `config.yaml` (YAML); not committed, copy from `config.yaml.sample`
2. **Firefly III API calls** — REST API with Bearer token; fetches categories, budgets, asset accounts, and transactions for the target month and year-to-date
3. **Multi-currency conversion** — if `base_currency` is set in config, fetches exchange rates from `frankfurter.app` (ECB rates, no API key needed) and converts all amounts; report shows converted value, original, and rate
4. **Visualization** — Plotly generates a Sankey diagram (income → budgets → expense categories → savings) and line charts (savings account trends over 6 months); charts are rendered as static PNG via Kaleido for email embedding, and also as interactive Plotly HTML for the attachment
5. **HTML assembly** — inline CSS, embedded base64 PNG charts, responsive layout with Inter + JetBrains Mono fonts
6. **Delivery** — SMTP (STARTTLS or SSL); optionally pings a `healthcheck_url` on success; `--preview` mode skips SMTP and writes `preview.html`

## Configuration keys

Key config fields in `config.yaml`:

| Key | Required | Purpose |
|-----|----------|---------|
| `firefly-url` | Yes | Firefly III base URL (no trailing slash) |
| `accesstoken` | Yes | Firefly III Personal Access Token |
| `currency` / `currency_symbol` | Yes | Primary display currency |
| `base_currency` / `base_currency_symbol` | No | Enables multi-currency conversion |
| `exclude_accounts` | No | Account names to hide from savings chart |
| `healthcheck_url` | No | healthchecks.io ping URL |
|     |          |         |
|     |          |         |

## Available skills

Before you start a new task read the skills:

| Task                 | File to read                             |
|----------------------|------------------------------------------|
| Design               | /.claude/skills/frontend-design/SKILL.md |
| Python bst practices | /.claude/skills/python-patterns/SKILL.md |

Always use the `SKILL.md` file to understand the task and the available skills before every task.