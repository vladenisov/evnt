# Project Instructions for AI Agents

- **[AGENTS.md](AGENTS.md)**: what the service is, the repository layout, runtime facts and guardrails. Read it first.
- **[CONTRIBUTING.md](CONTRIBUTING.md)**: every build, test and lint command, the single source of truth that CI and the `Makefile` follow.
- **[README.md](README.md)**: user-facing configuration (`EVNT_*` settings) and the endpoint reference.

The rule worth repeating: the backend uses `uv` (`backend/uv.lock`) and the frontend uses `pnpm` (`frontend/pnpm-lock.yaml`). Never use `pip`, `poetry`, `npm` or `yarn`, or the lockfiles drift from CI. `make check` runs every gate CI runs.
