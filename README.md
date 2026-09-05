# ynab-backup

Periodic backup and restore tool for a [YNAB](https://www.ynab.com/) (You Need A Budget) budget. Backs up all budget data — accounts, categories, payees, monthly allocations, transactions, and scheduled transactions — as lossless JSON snapshots, and can replay a snapshot into a new, empty budget for restore testing.

## How it works

**Backup mode** (default) runs as a long-lived loop: fetch every resource for a budget via the YNAB REST API, write it to a timestamped directory of per-resource JSON files plus a manifest, prune old snapshots to a retention count, then sleep and repeat.

**Restore mode** (`restore` subcommand) is a one-shot, operator-initiated action that replays a snapshot into a **new, empty budget** (never the live one). It creates accounts (with notes, debt fields, and closed status), payees, category groups, categories (with goals), sets monthly budget allocations, and replays transactions — remapping all IDs and preserving transfer relationships — then prints a validation report comparing source vs. target.

## Environment variables (backup mode)

| Variable | Purpose | Default |
|---|---|---|
| `YNAB_ACCESS_TOKEN` | YNAB API bearer token (required) | — |
| `YNAB_BUDGET_ID` | Budget to back up; `last-used-budget` is a YNAB sentinel | `last-used-budget` |
| `YNAB_BUDGET_NAME` | Resolve budget by name (overrides `YNAB_BUDGET_ID` if set) | — |
| `YNAB_API_BASE_URL` | API base URL | `https://api.ynab.com/v1` |
| `YNAB_BACKUP_INTERVAL_SECONDS` | Seconds between backups | `21600` (6h) |
| `YNAB_BACKUP_RETENTION` | Snapshots to keep per budget (0 = keep all) | `30` |
| `YNAB_BACKUP_DIR` | Output directory (bind-mounted into container) | `/backup` |

All logs use UTC timestamps.

## Snapshot directory layout

```
/backup/
  My_Budget/
    20240115T120000Z/
      accounts.json
      category_groups.json
      payees.json
      months.json
      transactions.json
      scheduled_transactions.json
      manifest.json
    20240115T180000Z/
      ...
```

Each JSON file contains the full, paginated response for that resource. `manifest.json` records the source budget id/name, timestamp, and per-resource counts.

## Restore procedure

> **The YNAB public API cannot create or delete budgets.** The operator must create a new empty budget in the YNAB web UI before running restore. Category groups, categories, accounts, and all other resources are created automatically via the API.

### Prerequisites (manual, in YNAB UI)

1. **Create an empty budget** (e.g., "Restore Test") and copy its budget ID from the URL (`https://app.ynab.com/<budget-id>/budget`).

### Running a restore

```bash
docker run --rm \
  -v /srv/nas/backup/ynab:/backup:ro \
  -e YNAB_ACCESS_TOKEN=<your-token> \
  ghcr.io/tedski/ynab-backup:latest \
  restore \
  --target-budget-id <new-budget-id> \
  --snapshot /backup/My_Budget/20240115T120000Z \
  --throttle 2.0
```

The `--throttle` flag (default 2.0s) sleeps between API calls to respect YNAB's rate limits. Increase it further if restore continues hitting 429 errors on larger budgets.

The tool replays resources in dependency order, building ID maps as it goes:

1. **Accounts** (balance 0; includes `note`, `debt_*` fields for loans, and `deleted` for closed accounts)
2. **Transfer payees** (auto-created by YNAB for each account; mapped by account)
3. **Regular payees**
4. **Category groups** (created via `POST /plans/{plan_id}/category_groups`)
5. **Categories** (created within groups; goal_ fields PATCHed via `/plans/{plan_id}/categories/{id}`)
6. **Monthly budget allocations** (PATCH `budgeted` per category per month)
7. **Transactions** (incl. splits; IDs remapped; `import_id` dropped to avoid dedup)
8. **Scheduled transactions**

After replay, a **validation report** compares source vs. target counts and per-month budgeted amounts. Per-item creation failures (e.g., a duplicate name, an unsupported field) are logged as errors and skipped — the restore continues best-effort rather than aborting. Check the validation report for any mismatches.

### Cleanup

Delete the "Restore Test" budget in the YNAB UI when done (the API cannot delete budgets).

## What's restorable

The following are fully restored via the API:

- Accounts (name, type, note, closed status via `deleted`, `debt_*` fields for loans)
- Payees (regular and transfer)
- Category groups (created via `POST /plans/{plan_id}/category_groups`)
- Categories (name, group assignment, `goal_*` fields via PATCH)
- Monthly budget allocations
- Transactions (including subtransactions/splits)
- Scheduled transactions

## Known restore gaps

These are captured in the snapshot (lossless backup) but **cannot be replayed** through the public YNAB API:

- **Budget settings** (currency, date format) — determined by the manually created target budget.
- **Transaction IDs** — necessarily new in the target (relationships are remapped; originals cannot be preserved).
- **Derived values** (to-be-budgeted, cleared/available balances) — recomputed by YNAB from the replayed transactions, not restored directly.

These gaps are reported as warnings during restore so a drill never passes silently on a false premise.

## Development

```bash
uv sync                    # install all dependencies (including dev)
uv run ruff check .        # lint
uv run ruff format --check .  # format check
uv run pytest -v           # test
```

## Docker

```bash
docker build -t ynab-backup .
docker run --rm \
  -v /path/to/backup:/backup \
  -e YNAB_ACCESS_TOKEN=<token> \
  ynab-backup backup --once
```
