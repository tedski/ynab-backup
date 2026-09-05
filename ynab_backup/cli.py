"""CLI argument parsing and entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

from ynab_backup.backup import run_backup_loop
from ynab_backup.client import YnabClient
from ynab_backup.constants import (
    DEFAULT_API_BASE_URL,
    DEFAULT_BACKUP_DIR,
    DEFAULT_BUDGET_ID,
    DEFAULT_INTERVAL,
    DEFAULT_RETENTION,
    DEFAULT_THROTTLE,
)
from ynab_backup.exceptions import YnabError
from ynab_backup.restore import run_restore

LOG = logging.getLogger("ynab-backup")


class _UTCFormatter(logging.Formatter):
    """Logging formatter that uses UTC timestamps."""

    converter = time.gmtime


def setup_logging() -> None:
    """Configure root logging with UTC timestamps to stdout."""
    formatter = _UTCFormatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[handler])


def client_from_env(args: argparse.Namespace) -> YnabClient:
    """Build a ``YnabClient`` from CLI args and environment variables.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Configured ``YnabClient`` instance.

    Raises:
        YnabError: If ``YNAB_ACCESS_TOKEN`` is not provided.
    """
    token = getattr(args, "token", None) or os.environ.get("YNAB_ACCESS_TOKEN")
    if not token:
        raise YnabError("YNAB_ACCESS_TOKEN is required (env or --token)")
    base_url = getattr(args, "api_base_url", None) or os.environ.get(
        "YNAB_API_BASE_URL", DEFAULT_API_BASE_URL
    )
    throttle = getattr(args, "throttle", 0)
    return YnabClient(token, base_url=base_url, throttle_seconds=throttle)


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ``ArgumentParser`` with ``backup`` and ``restore``
        subcommands and top-level backup defaults.
    """
    parser = argparse.ArgumentParser(description="YNAB budget backup and restore")
    sub = parser.add_subparsers(dest="command")

    backup_defaults: dict[str, Any] = {
        "once": False,
        "budget_id": os.environ.get("YNAB_BUDGET_ID", DEFAULT_BUDGET_ID),
        "budget_name": os.environ.get("YNAB_BUDGET_NAME"),
        "backup_dir": os.environ.get("YNAB_BACKUP_DIR", DEFAULT_BACKUP_DIR),
        "interval": int(os.environ.get("YNAB_BACKUP_INTERVAL_SECONDS", DEFAULT_INTERVAL)),
        "retention": int(os.environ.get("YNAB_BACKUP_RETENTION", DEFAULT_RETENTION)),
        "token": None,
        "api_base_url": None,
    }
    parser.set_defaults(**backup_defaults)

    bk = sub.add_parser("backup", help="Run the periodic backup loop")
    bk.add_argument("--once", action="store_true", help="run a single backup and exit")
    bk.add_argument("--budget-id", default=os.environ.get("YNAB_BUDGET_ID", DEFAULT_BUDGET_ID))
    bk.add_argument("--budget-name", default=os.environ.get("YNAB_BUDGET_NAME"))
    bk.add_argument("--backup-dir", default=os.environ.get("YNAB_BACKUP_DIR", DEFAULT_BACKUP_DIR))
    bk.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("YNAB_BACKUP_INTERVAL_SECONDS", DEFAULT_INTERVAL)),
    )
    bk.add_argument(
        "--retention",
        type=int,
        default=int(os.environ.get("YNAB_BACKUP_RETENTION", DEFAULT_RETENTION)),
    )
    bk.add_argument("--token", default=None)
    bk.add_argument("--api-base-url", default=None)

    rs = sub.add_parser("restore", help="Replay a snapshot into a new budget")
    rs.add_argument("--target-budget-id", required=True)
    rs.add_argument("--snapshot", required=True, help="path to a snapshot directory")
    rs.add_argument("--token", default=None)
    rs.add_argument("--api-base-url", default=None)
    rs.add_argument(
        "--throttle",
        type=float,
        default=DEFAULT_THROTTLE,
        help=f"seconds to sleep between API calls during restore (default: {DEFAULT_THROTTLE})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the YNAB backup tool.

    Args:
        argv: Optional argument list (defaults to ``sys.argv``).

    Returns:
        Exit code (0 on success, 1 on fatal error).
    """
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "backup"
    try:
        if command == "backup":
            run_backup_loop(args)
        elif command == "restore":
            run_restore(args)
    except YnabError as exc:
        LOG.error("fatal: %s", exc)
        return 1
    return 0
