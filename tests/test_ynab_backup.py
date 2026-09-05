"""Tests for backup and restore logic."""

from __future__ import annotations

import json

import pytest

from ynab_backup import backup, restore
from ynab_backup.constants import TXN_BATCH_SIZE
from ynab_backup.exceptions import YnabError


class FakeClient:
    """Minimal in-memory YnabClient fake for testing."""

    def __init__(self, *, budgets=None, budget=None, resources=None, fail_posts=False) -> None:
        self._budgets = budgets or []
        self._budget = budget or {}
        self._resources = resources or {}
        self.fail_posts = fail_posts
        self.posts: list[tuple[str, dict]] = []
        self.patches: list[tuple[str, dict]] = []
        self._post_counter = 0

    def get_list(self, path, key, **params):
        return list(self._resources.get(key, []))

    def get(self, path, **params):
        if path == "/budgets":
            return {"budgets": self._budgets}
        return {"budget": self._budget}

    def post(self, path, payload):
        if self.fail_posts:
            raise YnabError("POST not supported (fake)")
        self._post_counter += 1
        self.posts.append((path, payload))
        if "account" in payload:
            return {
                "account": {
                    "id": f"acct-new-{self._post_counter}",
                    "name": payload["account"]["name"],
                }
            }
        if "payee" in payload:
            return {
                "payee": {"id": f"pay-new-{self._post_counter}", "name": payload["payee"]["name"]}
            }
        if "category_group" in payload:
            return {
                "category_group": {
                    "id": f"grp-new-{self._post_counter}",
                    "name": payload["category_group"]["name"],
                }
            }
        if "category" in payload:
            return {
                "category": {
                    "id": f"cat-new-{self._post_counter}",
                    "name": payload["category"]["name"],
                }
            }
        return {}

    def patch(self, path, payload):
        self.patches.append((path, payload))
        return {}

    def delete(self, path):
        return {}


# --------------------------------------------------------------------------- #
# Backup tests
# --------------------------------------------------------------------------- #


def test_take_snapshot_fetches_all_resources():
    client = FakeClient(
        resources={
            "accounts": [{"id": "a1"}],
            "category_groups": [{"id": "g1", "categories": []}],
            "payees": [{"id": "p1"}],
            "months": [{"month": "2024-01-01"}],
            "transactions": [{"id": "t1"}],
            "scheduled_transactions": [{"id": "s1"}],
        }
    )
    snap = backup.take_snapshot(client, "budget-1")
    assert snap["accounts"] == [{"id": "a1"}]
    assert snap["category_groups"] == [{"id": "g1", "categories": []}]
    assert snap["months"] == [{"month": "2024-01-01"}]
    assert snap["transactions"] == [{"id": "t1"}]


def test_write_snapshot_creates_files_and_manifest(tmp_path):
    snapshot = {
        "accounts": [{"id": "a1"}],
        "category_groups": [],
        "payees": [],
        "months": [],
        "transactions": [],
        "scheduled_transactions": [],
        "budget_name": "My Budget",
    }
    snap_dir = backup.write_snapshot(snapshot, tmp_path, "budget-1")
    assert (snap_dir / "accounts.json").exists()
    assert (snap_dir / "manifest.json").exists()
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["budget_id"] == "budget-1"
    assert manifest["counts"]["accounts"] == 1


@pytest.mark.parametrize(
    ("n_seeds", "retention", "expected"),
    [
        (5, 2, 2),
        (3, 0, 3),
        (3, -5, 3),
    ],
)
def test_prune_snapshots(tmp_path, n_seeds, retention, expected):
    for i in range(n_seeds):
        d = tmp_path / f"2024010{i}T000000Z"
        d.mkdir()
        (d / "manifest.json").write_text("{}")
        (d / "accounts.json").write_text("[]")
    backup.prune_snapshots(tmp_path, retention=retention)
    remaining = sorted(d for d in tmp_path.iterdir() if (d / "manifest.json").exists())
    assert len(remaining) == expected
    if retention > 0:
        assert remaining[-1].name == f"2024010{n_seeds - 1}T000000Z"


@pytest.mark.parametrize(
    (
        "budgets",
        "escenario",
        "budget_id",
        "budget_name",
        "expected_id",
        "expected_name",
        "expect_error",
    ),
    [
        (
            [{"id": "b1", "name": "My Budget"}, {"id": "b2", "name": "Other"}],
            "by name",
            "ignored",
            "My Budget",
            "b1",
            "My Budget",
            False,
        ),
        (
            [{"id": "b1", "name": "My Budget"}],
            "case-insensitive",
            "ignored",
            "my budget",
            "b1",
            "My Budget",
            False,
        ),
        (
            [{"id": "b1", "name": "My Budget"}],
            "name not found",
            "ignored",
            "Nonexistent",
            None,
            None,
            True,
        ),
        ([], "by id", "budget-123", None, "budget-123", "Test Budget", False),
    ],
)
def test_resolve_budget(
    budgets, escenario, budget_id, budget_name, expected_id, expected_name, expect_error
):
    client = FakeClient(
        budgets=budgets,
        budget={"id": "budget-123", "name": "Test Budget"} if not budgets else budgets[0],
    )
    if expect_error:
        try:
            backup.resolve_budget(client, budget_id, budget_name)
            raise AssertionError("should have raised")
        except YnabError:
            return
    bid, bname = backup.resolve_budget(client, budget_id, budget_name)
    assert bid == expected_id
    assert bname == expected_name


# --------------------------------------------------------------------------- #
# Restore tests
# --------------------------------------------------------------------------- #


def test_remap_transaction_basic():
    txn = {
        "account_id": "a1",
        "date": "2024-01-15",
        "amount": -5000,
        "payee_id": "p1",
        "category_id": "c1",
        "memo": "lunch",
        "cleared": "cleared",
        "approved": False,
        "flag_color": "red",
    }
    result = restore.remap_transaction(
        txn,
        account_id_map={"a1": "a1new"},
        payee_id_map={"p1": "p1new"},
        transfer_payee_map={},
        category_id_map={"c1": "c1new"},
    )
    assert result["account_id"] == "a1new"
    assert result["payee_id"] == "p1new"
    assert result["category_id"] == "c1new"
    assert "import_id" not in result


def test_remap_transaction_transfer_payee():
    txn = {
        "account_id": "a1",
        "date": "2024-01-15",
        "amount": 10000,
        "payee_id": "transfer-payee-a2",
        "category_id": None,
    }
    result = restore.remap_transaction(
        txn,
        account_id_map={"a1": "a1new", "a2": "a2new"},
        payee_id_map={},
        transfer_payee_map={"transfer-payee-a2": "new-transfer-a2"},
        category_id_map={},
    )
    assert result["payee_id"] == "new-transfer-a2"
    assert result.get("category_id") is None


def test_remap_transaction_skips_missing_account():
    txn = {"account_id": "gone", "date": "2024-01-15", "amount": 100}
    result = restore.remap_transaction(
        txn,
        account_id_map={"a1": "a1new"},
        payee_id_map={},
        transfer_payee_map={},
        category_id_map={},
    )
    assert result is None


def test_remap_transaction_with_subtransactions():
    txn = {
        "account_id": "a1",
        "date": "2024-01-15",
        "amount": -10000,
        "payee_id": "p1",
        "category_id": None,
        "subtransactions": [
            {"amount": -6000, "payee_id": "p1", "category_id": "c1", "memo": "groceries"},
            {"amount": -4000, "payee_id": "p2", "category_id": "c2", "memo": "gas"},
        ],
    }
    result = restore.remap_transaction(
        txn,
        account_id_map={"a1": "a1new"},
        payee_id_map={"p1": "p1new", "p2": "p2new"},
        transfer_payee_map={},
        category_id_map={"c1": "c1new", "c2": "c2new"},
    )
    assert len(result["subtransactions"]) == 2
    assert result["subtransactions"][0]["category_id"] == "c1new"
    assert result["subtransactions"][1]["category_id"] == "c2new"


def test_create_accounts_includes_debt_and_note():
    client = FakeClient()
    accounts = [
        {
            "id": "a1",
            "name": "Mortgage",
            "type": "other_liability",
            "note": "30-year fixed",
            "debt_name": "Mortgage",
            "debt_type": "mortgage",
            "debt_original_balance": -50000000,
            "debt_interest_rate": 3.5,
            "deleted": False,
        },
    ]
    id_map = restore.create_accounts(client, "target", accounts)
    assert "a1" in id_map
    assert len(client.posts) == 1
    assert client.posts[0][0] == "/plans/target/accounts"
    payload = client.posts[0][1]["account"]
    assert payload["note"] == "30-year fixed"
    assert payload["debt_type"] == "mortgage"
    assert payload["debt_original_balance"] == -50000000
    assert payload["debt_interest_rate"] == 3.5


def test_create_accounts_includes_deleted():
    """Closed accounts (deleted=True) should still be created."""
    client = FakeClient()
    accounts = [
        {"id": "a1", "name": "Old Card", "type": "credit", "deleted": True},
    ]
    id_map = restore.create_accounts(client, "target", accounts)
    assert "a1" in id_map
    payload = client.posts[0][1]["account"]
    assert payload["deleted"] is True


def test_create_accounts_continues_on_error():
    client = FakeClient()
    client.fail_posts = True
    accounts = [
        {"id": "a1", "name": "Checking", "type": "checking", "deleted": False},
        {"id": "a2", "name": "Savings", "type": "savings", "deleted": False},
    ]
    id_map = restore.create_accounts(client, "target", accounts)
    assert id_map == {}


def test_create_payees_skips_transfer_and_deleted():
    client = FakeClient()
    payees = [
        {"id": "p1", "name": "Grocery Store", "deleted": False, "transfer_account_id": None},
        {"id": "p2", "name": "Transfer", "deleted": False, "transfer_account_id": "a2"},
        {"id": "p3", "name": "Gone", "deleted": True, "transfer_account_id": None},
    ]
    id_map = restore.create_payees(client, "target", payees)
    assert "p1" in id_map
    assert "p2" not in id_map
    assert "p3" not in id_map


def test_create_category_groups_creates_via_api():
    """Category groups are created via POST, not matched against pre-existing."""
    client = FakeClient(
        resources={
            "category_groups": [
                {"id": "internal", "name": "Internal Category Group"},
            ],
        },
    )
    source_groups = [
        {"id": "src-need", "name": "Needs", "deleted": False},
        {"id": "internal", "name": "Internal Category Group", "deleted": False},
        {"id": "src-gone", "name": "Gone", "deleted": True},
    ]
    id_map = restore.create_category_groups(client, "target", source_groups)
    assert "src-need" in id_map
    assert "internal" not in id_map  # internal skipped
    assert "src-gone" not in id_map  # deleted skipped
    assert len(client.posts) == 1  # only src-need
    assert client.posts[0][0] == "/plans/target/category_groups"


def test_create_categories_patches_goals():
    """After creating a category, goal_ fields are PATCHed."""
    client = FakeClient(
        resources={
            "category_groups": [{"id": "g1", "name": "Needs", "categories": []}],
        },
    )
    source_groups = [
        {
            "id": "g1",
            "name": "Needs",
            "deleted": False,
            "categories": [
                {
                    "id": "c1",
                    "name": "Rent",
                    "deleted": False,
                    "goal_type": "TB",
                    "goal_target": 200000,
                    "goal_target_month": "2024-12",
                },
            ],
        },
    ]
    id_map = restore.create_categories(client, "target", source_groups, {"g1": "g1new"})
    assert "c1" in id_map
    # Should have 1 POST (create category) + 1 PATCH (goals)
    assert len(client.posts) == 1
    assert client.posts[0][0] == "/plans/target/categories"
    assert len(client.patches) == 1
    assert client.patches[0][0].startswith("/plans/target/categories/")
    patch_payload = client.patches[0][1]["category"]
    assert patch_payload["goal_type"] == "TB"
    assert patch_payload["goal_target"] == 200000


def test_set_budgeted_skips_zero_and_internal():
    client = FakeClient()
    months = [
        {
            "month": "2024-01-01",
            "categories": [
                {"id": "c1", "budgeted": 5000},
                {"id": "c2", "budgeted": 0},
                {"id": "internal-cat", "budgeted": 1000},
            ],
        }
    ]
    count = restore.set_budgeted(client, "target", months, {"c1": "c1new"})
    assert count == 1
    assert len(client.patches) == 1


def test_set_budgeted_continues_on_error():
    class FailPatchClient(FakeClient):
        def patch(self, path, payload):
            raise YnabError("patch failed")

    fc = FailPatchClient()
    months = [
        {
            "month": "2024-01-01",
            "categories": [
                {"id": "c1", "budgeted": 5000},
                {"id": "c2", "budgeted": 3000},
            ],
        }
    ]
    count = restore.set_budgeted(fc, "target", months, {"c1": "c1new", "c2": "c2new"})
    assert count == 0


def test_build_transfer_payee_map():
    target_payees = [
        {"id": "tp-checking", "transfer_account_id": "new-checking"},
        {"id": "tp-savings", "transfer_account_id": "new-savings"},
        {"id": "regular-payee", "transfer_account_id": None},
    ]
    source_payees = [
        {"id": "src-tp-checking", "transfer_account_id": "old-checking"},
        {"id": "src-tp-savings", "transfer_account_id": "old-savings"},
        {"id": "src-regular", "transfer_account_id": None},
    ]
    account_id_map = {"old-checking": "new-checking", "old-savings": "new-savings"}
    client = FakeClient(resources={"payees": target_payees})
    tmap = restore.build_transfer_payee_map(client, "target", source_payees, account_id_map)
    assert tmap["src-tp-checking"] == "tp-checking"
    assert tmap["src-tp-savings"] == "tp-savings"
    assert "src-regular" not in tmap


def test_create_transactions_batches_and_skips():
    client = FakeClient()
    txns = [
        {"id": "t1", "account_id": "a1", "date": "2024-01-01", "amount": -100, "deleted": False},
        {"id": "t2", "account_id": "gone", "date": "2024-01-02", "amount": -200, "deleted": False},
        {"id": "t3", "account_id": "a1", "date": "2024-01-03", "amount": -300, "deleted": True},
    ]
    created = restore.create_transactions(
        client,
        "target",
        txns,
        account_id_map={"a1": "new-a1"},
        payee_id_map={},
        transfer_payee_map={},
        category_id_map={},
    )
    assert created == 1
    assert len(client.posts) == 1


def test_create_transactions_large_batch():
    client = FakeClient()
    txns = [
        {"id": f"t{i}", "account_id": "a1", "date": "2024-01-01", "amount": -1, "deleted": False}
        for i in range(TXN_BATCH_SIZE + 5)
    ]
    created = restore.create_transactions(
        client,
        "target",
        txns,
        account_id_map={"a1": "new-a1"},
        payee_id_map={},
        transfer_payee_map={},
        category_id_map={},
    )
    assert created == TXN_BATCH_SIZE + 5
    assert len(client.posts) == 2


def test_create_scheduled_transactions_skips_deleted_and_missing():
    client = FakeClient()
    scheduled = [
        {
            "id": "s1",
            "account_id": "a1",
            "date": "2024-02-01",
            "frequency": "monthly",
            "amount": -5000,
            "deleted": False,
        },
        {
            "id": "s2",
            "account_id": "gone",
            "date": "2024-02-01",
            "frequency": "monthly",
            "amount": -1000,
            "deleted": False,
        },
        {
            "id": "s3",
            "account_id": "a1",
            "date": "2024-03-01",
            "frequency": "weekly",
            "amount": -200,
            "deleted": True,
        },
    ]
    created = restore.create_scheduled_transactions(
        client,
        "target",
        scheduled,
        account_id_map={"a1": "new-a1"},
        payee_id_map={},
        transfer_payee_map={},
        category_id_map={},
    )
    assert created == 1


@pytest.mark.parametrize(
    ("snapshot", "resource_key", "expected_status"),
    [
        (
            {
                "accounts": [{"id": "a1", "deleted": False}, {"id": "a2", "deleted": False}],
                "payees": [{"id": "p1", "deleted": False, "transfer_account_id": None}],
                "category_groups": [],
                "months": [],
                "transactions": [{"id": "t1", "deleted": False}],
                "scheduled_transactions": [],
            },
            "accounts",
            "MISMATCH",
        ),
        (
            {
                "accounts": [{"id": "a1", "deleted": False}],
                "payees": [{"id": "p1", "deleted": False, "transfer_account_id": None}],
                "category_groups": [],
                "months": [],
                "transactions": [{"id": "t1", "deleted": False}],
                "scheduled_transactions": [],
            },
            "transactions",
            "OK",
        ),
    ],
)
def test_validate(snapshot, resource_key, expected_status):
    client = FakeClient(
        resources={
            "accounts": [{"id": "ta1", "deleted": False}],
            "payees": [{"id": "tp1", "deleted": False, "transfer_account_id": None}],
            "category_groups": [],
            "months": [],
            "transactions": [{"id": "tt1", "deleted": False}],
            "scheduled_transactions": [],
        }
    )
    issues = restore.validate(client, "target", snapshot, {})
    line = [i for i in issues if resource_key in i]
    assert expected_status in line[0]


def test_load_snapshot_roundtrip(tmp_path):
    snapshot = {
        "accounts": [{"id": "a1"}],
        "category_groups": [{"id": "g1", "categories": []}],
        "payees": [{"id": "p1"}],
        "months": [{"month": "2024-01-01"}],
        "transactions": [{"id": "t1"}],
        "scheduled_transactions": [{"id": "s1"}],
    }
    backup.write_snapshot(snapshot, tmp_path, "budget-1")
    snap_dir = next(tmp_path.iterdir())
    loaded = restore.load_snapshot(snap_dir)
    assert loaded["accounts"] == [{"id": "a1"}]
    assert loaded["category_groups"] == [{"id": "g1", "categories": []}]
    assert loaded["manifest"]["budget_id"] == "budget-1"


# --------------------------------------------------------------------------- #
# CLI tests
# --------------------------------------------------------------------------- #


def test_main_no_subcommand_uses_backup_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("YNAB_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("YNAB_BACKUP_INTERVAL_SECONDS", "999")
    called_with = {}

    def mock_loop(args):
        called_with["once"] = args.once
        called_with["budget_id"] = args.budget_id
        called_with["backup_dir"] = args.backup_dir
        called_with["interval"] = args.interval

    monkeypatch.setattr("ynab_backup.cli.run_backup_loop", mock_loop)
    from ynab_backup.cli import main

    rc = main([])
    assert rc == 0
    assert called_with["once"] is False
    assert called_with["budget_id"] == "last-used-budget"
    assert called_with["interval"] == 999


def test_main_backup_once(monkeypatch, tmp_path):
    monkeypatch.setenv("YNAB_ACCESS_TOKEN", "fake-token")
    monkeypatch.setenv("YNAB_BACKUP_DIR", str(tmp_path))
    called = {}

    def mock_loop(args):
        called["once"] = args.once

    monkeypatch.setattr("ynab_backup.cli.run_backup_loop", mock_loop)
    from ynab_backup.cli import main

    rc = main(["backup", "--once"])
    assert rc == 0
    assert called["once"] is True
