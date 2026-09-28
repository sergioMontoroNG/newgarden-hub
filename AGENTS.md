# Repository Guidelines

## Project Structure & Module Organization

- `hub/app.py` contains FastAPI routes, request models, Shopify integration, and embedded HTML/CSS/JavaScript; there is no separate frontend build or asset directory.
- `hub/worker.py` handles sequential translation jobs, DeepL requests, and output validation.
- `hub/db.py` manages PostgreSQL connections and startup migrations; `hub/schema.json` describes translation output.
- `migrations/` contains ordered SQL migrations. Add the next numbered file, such as `006_descriptive_name.sql`; applied migrations are recorded and should not be rewritten.
- `scripts/` contains backup and restore utilities. `.github/workflows/deploy.yml` deploys pushes to `main`.

## Build, Test, and Development Commands

Copy `.env.example` to `.env` and configure a strong database password and DeepL credentials before starting.

- `docker compose up -d --build`: build and start PostgreSQL, the API, and the worker.
- `docker compose ps`: inspect service health.
- `curl -fsS http://127.0.0.1:8787/health`: check the API using the default bind address and port; use `curl.exe` on Windows if needed.
- `docker compose logs --since=1h app worker`: inspect application and translation failures.
- `docker compose stop`: stop services while preserving data.
- `python -m compileall hub`: check Python syntax locally.

Open `/` for the interface and `/docs` for API documentation.

## Coding Style & Naming Conventions

Target Python 3.12. Use four-space indentation, `snake_case` functions and variables, `PascalCase` request models, and uppercase environment variables. Follow nearby conventions when editing embedded UI code; keep user-facing text consistent with the existing Spanish interface. Use parameterized SQL. No formatter or linter is configured; keep changes focused and avoid unrelated reformatting.

## Testing Guidelines

No automated test framework, test directory, or coverage threshold is configured. Check syntax, service health, and affected UI/API flows. Validate translation, locale selection, and field approval changes with controlled test data. Record commands and outcomes in the PR; distinguish simulated queue publication from editor actions that can write to Shopify.

## Commit & Pull Request Guidelines

Recent commits use short subjects, mostly Spanish, such as `Arreglo traducción por lotes`; no Conventional Commits pattern is established. PRs should describe behavior changes, link relevant issues, list validation, and include screenshots for UI changes. Highlight migrations and configuration changes. Pushes to `main` automatically deploy, so review deployment impact before merging.

## Security & Configuration

Never commit `.env`, credentials, logs, or database dumps. Keep credentials server-side and preserve human approval before Shopify writes. The app has no login: retain localhost binding or protected LAN access. Back up data before migrations or destructive restores.
