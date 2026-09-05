"""Backup logic: snapshot, write, prune, and the periodic loop."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ynab_backup.client import YnabClientProtocol
from ynab_backup.constants import BACKUP_RESOURCES
from ynab_backup.exceptions import YnabError
from ynab_backup.utils import filename_for_key, is_snapshot_dir, rmtree, safe_name, write_json

LOG = logging.getLogger("ynab-backup")


def resolve_budget(
    client: YnabClientProtocol, budget_id: str, budget_name: str | None
) -> tuple[str, str]:
    """Resolve the budget to back up.

    ``budget_name`` takes precedence when provided: the budgets list is scanned
    for a case-insensitive match. The special id ``last-used-budget`` is passed
    straight through to the API, which resolves it server-side.

    Args:
        client: YNAB API client.
        budget_id: Budget id, or ``last-used-budget`` sentinel.
        budget_name: Optional budget name to resolve by (case-insensitive).

    Returns:
        A tuple of ``(budget_id, budget_name)``.

    Raises:
        YnabError: If ``budget_name`` is set but no matching budget is found.
    """
    budgets = client.get("/budgets", include_accounts="false").get("budgets", [])
    if budget_name:
        for b in budgets:
            if b.get("name", "").lower() == budget_name.lower():
                return b["id"], b["name"]
        raise YnabError(f"no budget named {budget_name!r} found")
    data = client.get(f"/budgets/{budget_id}")
    budget = data.get("budget", {})
    return budget.get("id", budget_id), budget.get("name", "unknown")


def take_snapshot(client: YnabClientProtocol, budget_id: str) -> dict[str, Any]:
    """Fetch all resources for a budget.

    Args:
        client: YNAB API client.
        budget_id: Budget id to snapshot.

    Returns:
        Dict keyed by resource name (``accounts``, ``category_groups``, etc.)
        with full paginated list values.
    """
    snapshot: dict[str, Any] = {}
    for endpoint, _filename, key in BACKUP_RESOURCES:
        LOG.info("fetching %s ...", endpoint)
        snapshot[key] = client.get_list(f"/budgets/{budget_id}/{endpoint}", key)
    return snapshot


def write_snapshot(snapshot: dict[str, Any], dest: Path, budget_id: str) -> Path:
    """Write a snapshot as per-resource JSON files plus a manifest.

    A timestamped child directory is created under ``dest``. Files are written
    world-readable (0644) so one-shot restore containers running as a different
    UID can read them.

    Args:
        snapshot: Dict of resource data keyed by resource name.
        dest: Budget directory to write the snapshot into.
        budget_id: Source budget id for the manifest.

    Returns:
        Path to the created snapshot directory.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    snap_dir = dest / ts
    snap_dir.mkdir(parents=True, exist_ok=True)

    f_map = filename_for_key()
    for key, filename in f_map.items():
        write_json(snap_dir / filename, snapshot[key], mode=0o644)

    manifest = {
        "budget_id": budget_id,
        "budget_name": snapshot.get("budget_name", "unknown"),
        "timestamp": ts,
        "counts": {key: len(snapshot[key]) for _e, _f, key in BACKUP_RESOURCES},
    }
    write_json(snap_dir / "manifest.json", manifest, mode=0o644)
    LOG.info("snapshot written to %s", snap_dir)
    return snap_dir


def prune_snapshots(budget_dir: Path, retention: int) -> None:
    """Delete old snapshot directories, keeping the ``retention`` most recent.

    Snapshot directories are ISO-8601 UTC timestamps, so lexical sort equals
    time sort. A ``retention`` of 0 or negative keeps all snapshots.

    Args:
        budget_dir: Directory containing snapshot subdirectories.
        retention: Number of snapshots to keep.
    """
    retention = max(0, retention)
    if retention == 0:
        return
    snaps = sorted(d for d in budget_dir.iterdir() if d.is_dir() and is_snapshot_dir(d))
    for old in snaps[:-retention]:
        LOG.info("pruning old snapshot %s", old)
        rmtree(old)


def backup_once(
    client: YnabClientProtocol,
    budget_id: str,
    budget_name: str | None,
    backup_dir: Path,
    retention: int,
) -> Path:
    """Run a single backup pass.

    Args:
        client: YNAB API client.
        budget_id: Budget id, or ``last-used-budget`` sentinel.
        budget_name: Optional budget name to resolve by.
        backup_dir: Root backup directory.
        retention: Number of snapshots to keep per budget.

    Returns:
        Path to the created snapshot directory.
    """
    bid, bname = resolve_budget(client, budget_id, budget_name)
    snapshot = take_snapshot(client, bid)
    snapshot["budget_name"] = bname
    budget_dir = backup_dir / safe_name(bname)
    budget_dir.mkdir(parents=True, exist_ok=True)
    snap_dir = write_snapshot(snapshot, budget_dir, bid)
    prune_snapshots(budget_dir, retention)
    return snap_dir


def run_backup_loop(args: argparse.Namespace) -> None:
    """Run the periodic backup loop, sleeping between iterations.

    Args:
        args: Parsed CLI namespace with backup configuration.
    """
    from ynab_backup.cli import client_from_env

    client = client_from_env(args)
    backup_dir = Path(args.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    if args.once:
        backup_once(client, args.budget_id, args.budget_name, backup_dir, args.retention)
        return
    while True:
        try:
            backup_once(
                client,
                args.budget_id,
                args.budget_name,
                backup_dir,
                args.retention,
            )
        except Exception as exc:
            LOG.error("backup failed: %s", exc)
        LOG.info("sleeping %ds ...", args.interval)
        time.sleep(args.interval)
