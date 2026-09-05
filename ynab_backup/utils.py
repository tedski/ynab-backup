"""Utility helpers for file I/O and naming."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from ynab_backup.constants import BACKUP_RESOURCES


def safe_name(name: str) -> str:
    """Make a budget name safe for use as a directory component.

    Args:
        name: Budget name to sanitize.

    Returns:
        Sanitized name with non-alphanumeric characters replaced by ``_``.
    """
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return safe or "unknown"


def write_json(path: Path, data: Any, *, mode: int = 0o644) -> None:
    """Write data as pretty-printed JSON to a file.

    Args:
        path: Destination file path.
        data: Data to serialize.
        mode: File permission mode (default 0644).
    """
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.chmod(path, mode)


def is_snapshot_dir(path: Path) -> bool:
    """Check whether a directory is a snapshot by looking for a manifest.

    Args:
        path: Directory path to check.

    Returns:
        True if the directory contains a ``manifest.json`` file.
    """
    return (path / "manifest.json").exists()


def rmtree(path: Path) -> None:
    """Remove a directory tree, ignoring errors.

    Args:
        path: Directory path to remove.
    """
    shutil.rmtree(path, ignore_errors=True)


def filename_for_key() -> dict[str, str]:
    """Map resource data keys to their snapshot filenames."""
    return {key: filename for _e, filename, key in BACKUP_RESOURCES}


def key_for_filename() -> dict[str, str]:
    """Map snapshot filenames to resource data keys."""
    return {filename: key for _e, filename, key in BACKUP_RESOURCES}
