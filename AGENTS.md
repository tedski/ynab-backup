# AGENTS.md

## Project overview

`ynab-backup` is a Python tool that backs up a YNAB budget as lossless JSON
snapshots and can replay those snapshots into a new budget (restore). It ships
as a hardened Docker image published to `ghcr.io/tedski/ynab-backup`.

| Directory / file | Purpose |
|---|---|
| `ynab_backup/` | Application package |
| `ynab_backup/__main__.py` | Entry point for `python -m ynab_backup` |
| `ynab_backup/cli.py` | CLI argument parsing and `main()` |
| `ynab_backup/client.py` | `YnabClient` + `YnabClientProtocol` + `SlidingWindowRateLimiter` |
| `ynab_backup/backup.py` | Snapshot, write, prune, backup loop |
| `ynab_backup/restore.py` | Load snapshot, create resources, remap IDs, validate |
| `ynab_backup/constants.py` | Defaults, field lists, batch size |
| `ynab_backup/exceptions.py` | `YnabError` |
| `ynab_backup/utils.py` | File I/O, naming helpers |
| `tests/` | 51 pytest tests using in-memory `FakeClient` |
| `Dockerfile` | `python:3.12-slim`, uv, non-root user |
| `.github/workflows/ci.yaml` | Lint+test on PR, build+push to GHCR on tag |

## Development

```bash
uv sync                        # install all dependencies (including dev)
uv run ruff check .            # lint
uv run ruff format --check .   # format check
uv run pytest -v               # run tests
```

## Code conventions

- **Docstrings**: Google style, enforced by ruff (`D` rules + `convention = "google"`).
- **Formatting**: Ruff, 100-char lines, py312 target.
- **Type hints**: All public functions are annotated. `YnabClientProtocol` is a
  `typing.Protocol` so `FakeClient` in tests conforms structurally.
- **Imports**: `from __future__ import annotations` at the top of every module.
  Stdlib → third-party → project-local, enforced by ruff `I` rules.
- **No `__all__`** — the package surface is small enough that explicit exports
  are overkill.
- **Late imports**: `backup.py` and `restore.py` import `client_from_env` from
  `cli.py` inside functions to avoid circular imports. Don't reorder these.

## Testing

Tests live in `tests/test_ynab_backup.py`. All tests use `FakeClient`, an
in-memory stub implementing `YnabClientProtocol`. Key patterns:

- **Fixtures**: `FakeClient` is constructed per-test with `resources=` dicts
  that seed `get_list()` responses and `budgets`/`budget` for `get()`.
- **Error simulation**: Set `fail_posts=True` on FakeClient to make all POSTs
  raise `YnabError`.
- **Path assertions**: Tests that care about endpoint paths check
  `client.posts[0][0]` or `client.patches[0][0]`.
- **Parametrize**: Use `@pytest.mark.parametrize` when multiple cases only
  vary inputs, not logic.
- **Rate limiter tests**: `SlidingWindowRateLimiter` accepts `_get_time` for
  deterministic clock injection. Never call `time.sleep()` in a limiter test
  — use the fake clock.
- **Retry tests**: Mock `client.session.request` with a `FakeResponse` class
  and track sleeps via `monkeypatch.setattr("ynab_backup.client.time.sleep",
  sleeps.append)`.

## YNAB API notes

- **Rate limit**: 200 requests per rolling hour, documented at
  https://api.ynab.com/v1. The `SlidingWindowRateLimiter` enforces this.
- **Plans API**: Some write endpoints use `/plans/{plan_id}/...` instead of
  `/budgets/{budget_id}/...`. See `restore.py` for which endpoints use which
  prefix.
- **Transfer payees**: Auto-created by YNAB when accounts are created. Map
  them via `build_transfer_payee_map()` during restore.
- **Internal category group**: Named `"Internal Category Group"` — skipped
  during restore. If YNAB renames this, update `constants.py`.

## Release process

1. Merge changes to `main`.
2. Tag: `git tag v<major>.<minor>.<patch> && git push origin v<major>.<minor>.<patch>`.
3. CI builds and publishes `ghcr.io/tedski/ynab-backup:<version>` and `:latest`.
4. Update `ynab_backup_image_tag` in the [ansible repo](https://github.com/tedski/ansible)
   if you're pinning to a specific version.