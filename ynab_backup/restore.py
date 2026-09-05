"""Restore logic: replay a snapshot into a new budget with ID remapping."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

from ynab_backup.client import YnabClientProtocol
from ynab_backup.constants import DEBT_FIELDS, GOAL_FIELDS, INTERNAL_CATEGORY_GROUP, TXN_BATCH_SIZE
from ynab_backup.exceptions import YnabError
from ynab_backup.utils import key_for_filename

LOG = logging.getLogger("ynab-backup")


def load_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    """Load a snapshot directory into a dict keyed by resource.

    Args:
        snapshot_dir: Path to a snapshot directory containing per-resource
            JSON files and a ``manifest.json``.

    Returns:
        Dict keyed by resource name, plus a ``"manifest"`` key.
    """
    k_map = key_for_filename()
    snapshot: dict[str, Any] = {}
    for path in sorted(snapshot_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        key = k_map.get(path.name, path.stem)
        with open(path, encoding="utf-8") as fh:
            snapshot[key] = json.load(fh)
    with open(snapshot_dir / "manifest.json", encoding="utf-8") as fh:
        snapshot["manifest"] = json.load(fh)
    return snapshot


def is_internal_group(group: dict) -> bool:
    """Check whether a category group is YNAB's internal system group.

    Args:
        group: Category group dict.

    Returns:
        True if the group is the internal system group.
    """
    return group.get("name", "").lower() == INTERNAL_CATEGORY_GROUP.lower()


def create_accounts(
    client: YnabClientProtocol, target_id: str, accounts: list[dict]
) -> dict[str, str]:
    """Create all accounts (including closed) and return id map.

    Passes through ``note``, ``debt_*`` fields, and ``deleted`` (closed status).
    Per-item failures are logged and skipped.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        accounts: Source account list from the snapshot.

    Returns:
        Dict mapping source account ids to new account ids.
    """
    id_map: dict[str, str] = {}
    for acct in accounts:
        payload: dict[str, Any] = {
            "account": {
                "name": acct.get("name", "Unnamed"),
                "type": acct.get("type", "checking"),
                "balance": 0,
            }
        }
        if acct.get("note"):
            payload["account"]["note"] = acct["note"]
        if acct.get("deleted"):
            payload["account"]["deleted"] = True
        for field in DEBT_FIELDS:
            if acct.get(field) is not None:
                payload["account"][field] = acct[field]
        try:
            data = client.post(f"/plans/{target_id}/accounts", payload)
            new_acct = data.get("account", {})
            id_map[acct["id"]] = new_acct.get("id", "")
            LOG.info("created account %r -> %s", acct.get("name"), new_acct.get("id"))
        except YnabError as exc:
            LOG.error("failed to create account %r: %s", acct.get("name"), exc)
    return id_map


def build_transfer_payee_map(
    client: YnabClientProtocol,
    target_id: str,
    source_payees: list[dict],
    account_id_map: dict[str, str],
) -> dict[str, str]:
    """Map source transfer-payee ids to target transfer-payee ids.

    YNAB auto-creates a transfer payee for every account. The target budget's
    payees are fetched and indexed by account id, then each source transfer
    payee is mapped to its target counterpart.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        source_payees: Source payee list from the snapshot.
        account_id_map: Mapping of source account ids to target account ids.

    Returns:
        Dict mapping source transfer-payee ids to target transfer-payee ids.
    """
    target_payees = client.get_list(f"/budgets/{target_id}/payees", "payees")
    by_account = {
        p["transfer_account_id"]: p["id"] for p in target_payees if p.get("transfer_account_id")
    }
    tmap: dict[str, str] = {}
    for sp in source_payees:
        src_taid = sp.get("transfer_account_id")
        if not src_taid or src_taid not in account_id_map:
            continue
        new_taid = account_id_map[src_taid]
        new_pid = by_account.get(new_taid)
        if new_pid:
            tmap[sp["id"]] = new_pid
    return tmap


def create_payees(client: YnabClientProtocol, target_id: str, payees: list[dict]) -> dict[str, str]:
    """Create non-deleted, non-transfer payees and return id map.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        payees: Source payee list from the snapshot.

    Returns:
        Dict mapping source payee ids to new payee ids.
    """
    id_map: dict[str, str] = {}
    for payee in payees:
        if payee.get("deleted"):
            continue
        if payee.get("transfer_account_id"):
            continue
        payload = {"payee": {"name": payee.get("name", "Unknown")}}
        try:
            data = client.post(f"/budgets/{target_id}/payees", payload)
            new_payee = data.get("payee", {})
            id_map[payee["id"]] = new_payee.get("id", "")
            LOG.info("created payee %r -> %s", payee.get("name"), new_payee.get("id"))
        except YnabError as exc:
            LOG.error("failed to create payee %r: %s", payee.get("name"), exc)
    return id_map


def create_category_groups(
    client: YnabClientProtocol,
    target_id: str,
    source_groups: list[dict],
) -> dict[str, str]:
    """Create user category groups via the plans API and return id map.

    Internal and deleted groups are skipped. Per-item failures are logged
    and skipped.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        source_groups: Source category group list from the snapshot.

    Returns:
        Dict mapping source group ids to new group ids.
    """
    id_map: dict[str, str] = {}
    for grp in source_groups:
        if is_internal_group(grp) or grp.get("deleted"):
            continue
        name = grp.get("name", "Unnamed")
        payload = {"category_group": {"name": name}}
        try:
            data = client.post(f"/plans/{target_id}/category_groups", payload)
            new_grp = data.get("category_group", {})
            id_map[grp["id"]] = new_grp.get("id", "")
            LOG.info("created category group %r -> %s", name, new_grp.get("id"))
        except YnabError as exc:
            LOG.error("failed to create category group %r: %s", name, exc)
    return id_map


def create_categories(
    client: YnabClientProtocol,
    target_id: str,
    source_groups: list[dict],
    group_id_map: dict[str, str],
) -> dict[str, str]:
    """Create non-deleted user categories and return id map.

    After creating each category, PATCHes goal_ fields if present. Per-item
    failures are logged and skipped.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        source_groups: Source category group list from the snapshot.
        group_id_map: Mapping of source group ids to target group ids.

    Returns:
        Dict mapping source category ids to new category ids.
    """
    id_map: dict[str, str] = {}
    for grp in source_groups:
        if is_internal_group(grp) or grp.get("deleted"):
            continue
        new_group_id = group_id_map.get(grp["id"])
        if not new_group_id:
            continue
        for cat in grp.get("categories", []):
            if cat.get("deleted"):
                continue
            payload = {
                "category": {
                    "name": cat.get("name", "Unnamed"),
                    "category_group_id": new_group_id,
                }
            }
            try:
                data = client.post(f"/plans/{target_id}/categories", payload)
                new_cat = data.get("category", {})
                new_id = new_cat.get("id", "")
                id_map[cat["id"]] = new_id
                LOG.info("created category %r -> %s", cat.get("name"), new_id)
            except YnabError as exc:
                LOG.error("failed to create category %r: %s", cat.get("name"), exc)
                continue
            patch_category_goals(client, target_id, new_id, cat)
    return id_map


def patch_category_goals(
    client: YnabClientProtocol,
    target_id: str,
    new_cat_id: str,
    source_cat: dict,
) -> None:
    """PATCH goal_ fields onto a newly created category.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        new_cat_id: Id of the newly created category in the target budget.
        source_cat: Source category dict from the snapshot.
    """
    goal_fields = {
        field: source_cat[field] for field in GOAL_FIELDS if source_cat.get(field) is not None
    }
    if not goal_fields:
        return
    try:
        client.patch(
            f"/plans/{target_id}/categories/{new_cat_id}",
            {"category": goal_fields},
        )
        LOG.info("patched goals for category %s", new_cat_id)
    except YnabError as exc:
        LOG.error("failed to patch goals for category %s: %s", new_cat_id, exc)


def set_budgeted(
    client: YnabClientProtocol,
    target_id: str,
    months: list[dict],
    category_id_map: dict[str, str],
) -> int:
    """Replay monthly ``budgeted`` amounts.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        months: Source months list from the snapshot.
        category_id_map: Mapping of source category ids to target category ids.

    Returns:
        Number of PATCH calls made.
    """
    count = 0
    for month in months:
        month_str = month.get("month")
        if not month_str:
            continue
        for cat in month.get("categories", []):
            orig_cat_id = cat.get("id")
            if orig_cat_id not in category_id_map:
                continue
            budgeted = cat.get("budgeted", 0)
            if budgeted == 0:
                continue
            new_cat_id = category_id_map[orig_cat_id]
            try:
                client.patch(
                    f"/budgets/{target_id}/months/{month_str}/categories/{new_cat_id}",
                    {"category": {"budgeted": budgeted}},
                )
                count += 1
            except YnabError as exc:
                LOG.error("failed to set budgeted for %s/%s: %s", month_str, orig_cat_id, exc)
    return count


def remap_payee(
    orig_payee_id: str | None,
    payee_id_map: dict[str, str],
    transfer_payee_map: dict[str, str],
) -> str | None:
    """Remap a source payee id to its target counterpart.

    Transfer payees take precedence over regular payees.

    Args:
        orig_payee_id: Source payee id, or None.
        payee_id_map: Mapping of source to target regular payee ids.
        transfer_payee_map: Mapping of source to target transfer payee ids.

    Returns:
        Target payee id, or None if the source id is None.
    """
    if not orig_payee_id:
        return None
    if orig_payee_id in transfer_payee_map:
        return transfer_payee_map[orig_payee_id]
    return payee_id_map.get(orig_payee_id)


def remap_category(orig_cat_id: str | None, category_id_map: dict[str, str]) -> str | None:
    """Remap a source category id to its target counterpart.

    Args:
        orig_cat_id: Source category id, or None.
        category_id_map: Mapping of source to target category ids.

    Returns:
        Target category id, or None if the source id is None or unmapped.
    """
    if not orig_cat_id:
        return None
    return category_id_map.get(orig_cat_id)


def remap_transaction(
    txn: dict,
    account_id_map: dict[str, str],
    payee_id_map: dict[str, str],
    transfer_payee_map: dict[str, str],
    category_id_map: dict[str, str],
) -> dict | None:
    """Remap a source transaction for creation in the target budget.

    Args:
        txn: Source transaction dict from the snapshot.
        account_id_map: Mapping of source to target account ids.
        payee_id_map: Mapping of source to target payee ids.
        transfer_payee_map: Mapping of source to target transfer-payee ids.
        category_id_map: Mapping of source to target category ids.

    Returns:
        Create-ready transaction dict, or None if the account is missing.
    """
    new_account_id = account_id_map.get(txn.get("account_id"))
    if not new_account_id:
        return None
    new_txn = {
        "account_id": new_account_id,
        "date": txn.get("date"),
        "amount": txn.get("amount"),
        "payee_id": remap_payee(txn.get("payee_id"), payee_id_map, transfer_payee_map),
        "category_id": remap_category(txn.get("category_id"), category_id_map),
        "memo": txn.get("memo"),
        "cleared": txn.get("cleared", "uncleared"),
        "approved": txn.get("approved", False),
        "flag_color": txn.get("flag_color"),
    }
    subs = []
    for sub in txn.get("subtransactions", []):
        new_sub = {
            "amount": sub.get("amount"),
            "payee_id": remap_payee(sub.get("payee_id"), payee_id_map, transfer_payee_map),
            "category_id": remap_category(sub.get("category_id"), category_id_map),
            "memo": sub.get("memo"),
        }
        subs.append(new_sub)
    if subs:
        new_txn["subtransactions"] = subs
    return {k: v for k, v in new_txn.items() if v is not None}


def create_transactions(
    client: YnabClientProtocol,
    target_id: str,
    transactions: list[dict],
    account_id_map: dict[str, str],
    payee_id_map: dict[str, str],
    transfer_payee_map: dict[str, str],
    category_id_map: dict[str, str],
) -> int:
    """Create non-deleted transactions in batches.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        transactions: Source transaction list from the snapshot.
        account_id_map: Mapping of source to target account ids.
        payee_id_map: Mapping of source to target payee ids.
        transfer_payee_map: Mapping of source to target transfer-payee ids.
        category_id_map: Mapping of source to target category ids.

    Returns:
        Number of transactions successfully created.
    """
    payload_txns: list[dict] = []
    skipped = 0
    for txn in transactions:
        if txn.get("deleted"):
            continue
        remapped = remap_transaction(
            txn, account_id_map, payee_id_map, transfer_payee_map, category_id_map
        )
        if remapped is None:
            skipped += 1
            continue
        payload_txns.append(remapped)

    created = 0
    failed = 0
    for i in range(0, len(payload_txns), TXN_BATCH_SIZE):
        batch = payload_txns[i : i + TXN_BATCH_SIZE]
        try:
            client.post(f"/budgets/{target_id}/transactions", {"transactions": batch})
            created += len(batch)
            LOG.info("created %d/%d transactions", created, len(payload_txns))
        except YnabError as exc:
            failed += len(batch)
            LOG.error("failed to create batch of %d transactions: %s", len(batch), exc)
    if skipped:
        LOG.warning("skipped %d transactions referencing missing accounts", skipped)
    if failed:
        LOG.error("failed to create %d transactions (batch errors)", failed)
    return created


def create_scheduled_transactions(
    client: YnabClientProtocol,
    target_id: str,
    scheduled: list[dict],
    account_id_map: dict[str, str],
    payee_id_map: dict[str, str],
    transfer_payee_map: dict[str, str],
    category_id_map: dict[str, str],
) -> int:
    """Create non-deleted scheduled transactions.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        scheduled: Source scheduled transaction list from the snapshot.
        account_id_map: Mapping of source to target account ids.
        payee_id_map: Mapping of source to target payee ids.
        transfer_payee_map: Mapping of source to target transfer-payee ids.
        category_id_map: Mapping of source to target category ids.

    Returns:
        Number of scheduled transactions successfully created.
    """
    created = 0
    for st in scheduled:
        if st.get("deleted"):
            continue
        new_account_id = account_id_map.get(st.get("account_id"))
        if not new_account_id:
            continue
        payload: dict[str, Any] = {
            "scheduled_transaction": {
                "account_id": new_account_id,
                "date": st.get("date"),
                "frequency": st.get("frequency", "never"),
                "amount": st.get("amount"),
                "payee_id": remap_payee(st.get("payee_id"), payee_id_map, transfer_payee_map),
                "category_id": remap_category(st.get("category_id"), category_id_map),
                "memo": st.get("memo"),
                "flag_color": st.get("flag_color"),
            }
        }
        subs = []
        for sub in st.get("subtransactions", []):
            subs.append(
                {
                    "amount": sub.get("amount"),
                    "payee_id": remap_payee(sub.get("payee_id"), payee_id_map, transfer_payee_map),
                    "category_id": remap_category(sub.get("category_id"), category_id_map),
                    "memo": sub.get("memo"),
                }
            )
        if subs:
            payload["scheduled_transaction"]["subtransactions"] = subs
        try:
            client.post(f"/budgets/{target_id}/scheduled_transactions", payload)
            created += 1
        except YnabError as exc:
            LOG.error("failed to create scheduled transaction: %s", exc)
    LOG.info("created %d scheduled transactions", created)
    return created


def validate(
    client: YnabClientProtocol,
    target_id: str,
    snapshot: dict[str, Any],
    category_id_map: dict[str, str],
) -> list[str]:
    """Compare source snapshot counts against the restored target budget.

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        snapshot: Source snapshot dict.
        category_id_map: Mapping of source to target category ids.

    Returns:
        List of validation report strings with OK/MISMATCH status.
    """
    issues: list[str] = []

    def src_count(key: str, *, keep: bool = True) -> int:
        items = snapshot.get(key, [])
        return sum(1 for x in items if not x.get("deleted")) if keep else len(items)

    tgt_accounts = client.get_list(f"/budgets/{target_id}/accounts", "accounts")
    tgt_payees = client.get_list(f"/budgets/{target_id}/payees", "payees")
    tgt_groups = client.get_list(f"/budgets/{target_id}/categories", "category_groups")
    tgt_txns = client.get_list(f"/budgets/{target_id}/transactions", "transactions")
    tgt_sched = client.get_list(
        f"/budgets/{target_id}/scheduled_transactions", "scheduled_transactions"
    )

    tgt_regular_payees = [
        p for p in tgt_payees if not p.get("transfer_account_id") and not p.get("deleted")
    ]
    tgt_user_groups = [g for g in tgt_groups if not is_internal_group(g) and not g.get("deleted")]
    tgt_user_cats = sum(
        len([c for c in g.get("categories", []) if not c.get("deleted")]) for g in tgt_user_groups
    )

    checks = [
        (
            "accounts",
            src_count("accounts"),
            len([a for a in tgt_accounts if not a.get("deleted")]),
        ),
        (
            "regular payees",
            src_count("payees")
            - sum(
                1
                for p in snapshot.get("payees", [])
                if p.get("transfer_account_id") and not p.get("deleted")
            ),
            len(tgt_regular_payees),
        ),
        (
            "user category groups",
            sum(
                1
                for g in snapshot.get("category_groups", [])
                if not is_internal_group(g) and not g.get("deleted")
            ),
            len(tgt_user_groups),
        ),
        (
            "user categories",
            sum(
                len([c for c in g.get("categories", []) if not c.get("deleted")])
                for g in snapshot.get("category_groups", [])
                if not is_internal_group(g) and not g.get("deleted")
            ),
            tgt_user_cats,
        ),
        (
            "transactions",
            src_count("transactions"),
            len([t for t in tgt_txns if not t.get("deleted")]),
        ),
        (
            "scheduled transactions",
            src_count("scheduled_transactions"),
            len([s for s in tgt_sched if not s.get("deleted")]),
        ),
    ]
    for label, src, tgt in checks:
        status = "OK" if src == tgt else "MISMATCH"
        issues.append(f"[{status}] {label}: source={src} target={tgt}")

    tgt_months = client.get_list(f"/budgets/{target_id}/months", "months")
    tgt_by_month = {m["month"]: m for m in tgt_months}
    budgeted_mismatches = 0
    for month in snapshot.get("months", []):
        tgt_month = tgt_by_month.get(month.get("month"))
        if not tgt_month:
            continue
        tgt_cat_budgeted = {c["id"]: c.get("budgeted", 0) for c in tgt_month.get("categories", [])}
        for cat in month.get("categories", []):
            orig_id = cat.get("id")
            new_id = category_id_map.get(orig_id)
            if not new_id:
                continue
            if tgt_cat_budgeted.get(new_id, 0) != cat.get("budgeted", 0):
                budgeted_mismatches += 1
    issues.append(
        f"[{'OK' if budgeted_mismatches == 0 else 'MISMATCH'}] "
        f"budgeted amounts: {budgeted_mismatches} mismatched"
    )
    return issues


def cleanup_default_groups(
    client: YnabClientProtocol, target_id: str, group_id_map: dict[str, str]
) -> None:
    """Remove any default category groups that came with the new budget.

    A freshly created YNAB budget often ships with an empty default group
    (e.g., "General"). Since we create the source's groups alongside it,
    this leaves an extra, unmapped group in the restored budget. Best-effort
    cleanup — if the API rejects the delete, the group stays (logged warning).

    Args:
        client: YNAB API client.
        target_id: Target budget id.
        group_id_map: Mapping of source group ids to target group ids.
            Groups in this map are the ones we created; everything else is a
            candidate for removal.
    """
    all_groups = client.get_list(f"/budgets/{target_id}/categories", "category_groups")
    new_group_ids = set(group_id_map.values())
    for grp in all_groups:
        if grp.get("deleted") or is_internal_group(grp):
            continue
        if grp["id"] in new_group_ids:
            continue
        try:
            client.delete(f"/plans/{target_id}/category_groups/{grp['id']}")
            LOG.info("removed default category group %r", grp.get("name"))
        except YnabError as exc:
            LOG.warning(
                "could not remove default category group %r (leaving in place): %s",
                grp.get("name"),
                exc,
            )


def run_restore(args: argparse.Namespace) -> None:
    """Replay a snapshot into a new budget with ID remapping and validation.

    Args:
        args: Parsed CLI namespace with ``target_budget_id``, ``snapshot``,
            and optional ``throttle`` and ``token``.
    """
    from ynab_backup.cli import client_from_env

    snapshot = load_snapshot(Path(args.snapshot))
    client = client_from_env(args)
    target_id = args.target_budget_id

    LOG.info("restoring snapshot %s into budget %s", args.snapshot, target_id)
    account_id_map = create_accounts(client, target_id, snapshot.get("accounts", []))
    transfer_payee_map = build_transfer_payee_map(
        client, target_id, snapshot.get("payees", []), account_id_map
    )
    payee_id_map = create_payees(client, target_id, snapshot.get("payees", []))
    group_id_map = create_category_groups(client, target_id, snapshot.get("category_groups", []))
    category_id_map = create_categories(
        client, target_id, snapshot.get("category_groups", []), group_id_map
    )
    patched = set_budgeted(client, target_id, snapshot.get("months", []), category_id_map)
    LOG.info("patched %d budgeted allocations", patched)

    # Remove any default category groups the new budget ships with.
    cleanup_default_groups(client, target_id, group_id_map)

    txns = create_transactions(
        client,
        target_id,
        snapshot.get("transactions", []),
        account_id_map,
        payee_id_map,
        transfer_payee_map,
        category_id_map,
    )
    sched = create_scheduled_transactions(
        client,
        target_id,
        snapshot.get("scheduled_transactions", []),
        account_id_map,
        payee_id_map,
        transfer_payee_map,
        category_id_map,
    )

    LOG.info("--- validation report ---")
    for line in validate(client, target_id, snapshot, category_id_map):
        LOG.info("%s", line)
    LOG.info("restore complete: %d transactions, %d scheduled", txns, sched)
