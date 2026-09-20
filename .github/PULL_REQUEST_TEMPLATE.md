## Scope

<!-- What does this change do, and what is deliberately out of scope? -->

## Checklist

- [ ] Scope is bounded to one task; no unrelated refactors are included.
- [ ] Tests were added or updated for every behaviour change.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run mypy src` passes.
- [ ] `uv run pytest` passes.
- [ ] No secrets, credentials, personal data, runtime state or generated artifacts are committed.
- [ ] No historical migration was rewritten (a new migration was added instead).
- [ ] No capability was widened without an ADR and an architecture review.

## Verification

<!-- The exact commands you ran, and what they reported. -->
