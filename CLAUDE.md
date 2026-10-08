# Project Instructions for AI Agents

- **[AGENTS.md](AGENTS.md)**: what the service is, the repository layout, runtime facts and guardrails. Read it first.
- **[website/docs/contributing.md](website/docs/contributing.md)**: every build, test and lint command, the single source of truth that CI and the `Makefile` follow.
- **[README.md](README.md)**: quick start and links to the native guides in `website/docs/`, including configuration (`EVNT_*` settings) and the endpoint reference.

The rule worth repeating: the backend uses `uv` (`backend/uv.lock`); the frontend and documentation use `bun` (`frontend/bun.lock`, `website/bun.lock`). Never use `pip`, `poetry`, `npm` or `yarn`, or the lockfiles drift from CI. `make check` runs the local gates; CI also requires integration tests and coverage.
