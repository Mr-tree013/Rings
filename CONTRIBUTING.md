# Contributing

Thanks for looking at this project. It is a safety-sensitive personal assistant: most of its value
comes from behaving predictably, so contributions are judged on bounded scope and verifiable
behaviour rather than on how much code they add.

## Before you start

1. Read [`AGENTS.md`](AGENTS.md) — it carries the project rules and the architectural boundaries.
2. Read the frozen specification in [`docs/specs/0001-system-design.md`](docs/specs/0001-system-design.md)
   and any ADR that touches your change.
3. Inspect the code and the tests around the area you want to change before writing any.

If a requirement conflicts with the specification or an ADR, stop and report the conflict instead of
changing the architecture on your own.

## Environment

- **Python 3.13**
- **Linux or WSL** (the reference runtime)
- [`uv`](https://docs.astral.sh/uv/)

```bash
uv sync --frozen
```

## Working rules

- **Keep changes bounded.** One task per pull request, and no unrelated refactors or cosmetic
  reordering of existing code.
- **Behaviour changes come with tests.** Add a test that would fail without your change; do not add
  tests that only exist to raise a coverage number.
- **Do not widen the capability surface on your own.** A new external capability needs architecture
  review and an ADR first, because external effects are the part of this system that can hurt
  somebody.
- **Historical migrations are immutable.** Add a new migration instead of editing one that has
  already shipped.
- **No secrets, ever.** Credentials come from environment variables. Do not commit a real key, mail
  password, session token, personal document, runtime database or backup archive, and do not use real
  personal data as a test fixture.
- **Tests stay offline.** The suite refuses outbound socket connections on purpose; use mocked
  transports instead of reaching the network.

## Before opening a pull request

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

All three must pass. If your change touches packaging, migrations or the release contract, also run:

```bash
uv lock --check
rm -rf dist && uv build
git diff --check
```

## Pull requests

Describe what changed and why, note the verification you ran, and call out anything you deliberately
left out of scope. Small, reviewable pull requests get reviewed; a large unrequested redesign will
usually be asked to split or stop.

## Reporting security issues

Please do not open a public issue for a security problem. See [`SECURITY.md`](SECURITY.md).
