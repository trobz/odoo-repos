# AGENTS.md

> Quick reference for AI coding agents.

## Project


- **Type**: CLI (Typer)

- **Language**: Python 3.10+
- **Package manager**: [uv](https://docs.astral.sh/uv/)

## Entry Points


- `odoo_repos/main.py` — CLI entry point (Typer)
- `odoo_repos/tui.py` — Textual TUI, calls the same functions as the CLI
- `odoo_repos/config.py` — `odoo-repos.toml` (tomlkit), validation, checkouts
- `odoo_repos/ops.py` — clone, fetch, fast-forward, status, lock, GraphQL check
- `odoo_repos/index.py` — module index and repository lists, module resolution
- `odoo_repos/timer.py` — systemd user units


## Commands

Run `make help` for all commands. Key ones:

```
make install   # Install deps + pre-commit hooks
make check     # Lint, format, type-check
make test      # Run pytest

```

## Key Files

- `Makefile` — Project commands
- `pyproject.toml` — Dependencies and build config
- `ruff.toml` — Linter/formatter rules

- `tests/` — Test suite (pytest); `conftest.py` fakes GitHub with local bare repos
