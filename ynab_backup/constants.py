"""Shared constants and defaults."""

DEFAULT_API_BASE_URL = "https://api.ynab.com/v1"
DEFAULT_BUDGET_ID = "last-used-budget"
DEFAULT_INTERVAL = 21600  # seconds (6 hours)
DEFAULT_RETENTION = 30
DEFAULT_BACKUP_DIR = "/backup"
DEFAULT_TIMEOUT = 60
DEFAULT_THROTTLE = 0.5  # seconds between API calls during restore

INTERNAL_CATEGORY_GROUP = "Internal Category Group"

TXN_BATCH_SIZE = 100

# (endpoint, filename, data key) for each snapshot resource, in fetch order.
BACKUP_RESOURCES = [
    ("accounts", "accounts.json", "accounts"),
    ("categories", "category_groups.json", "category_groups"),
    ("payees", "payees.json", "payees"),
    ("months", "months.json", "months"),
    ("transactions", "transactions.json", "transactions"),
    ("scheduled_transactions", "scheduled_transactions.json", "scheduled_transactions"),
]

# Fields prefixed with ``debt_`` that may appear on loan/credit accounts and
# should be passed through on account creation.
DEBT_FIELDS = [
    "debt_name",
    "debt_type",
    "debt_original_balance",
    "debt_minimum_payment",
    "debt_escalated_minimum_payment",
    "debt_interest_rate",
    "debt_escrow_amount",
    "debt_maturity_date",
]

# Fields prefixed with ``goal_`` that may appear on categories and should be
# replayed via PATCH after category creation.
GOAL_FIELDS = [
    "goal_type",
    "goal_target",
    "goal_target_month",
    "goal_percentage_complete",
    "goal_months_to_budget",
    "goal_under_funding",
    "goal_overall_left",
    "goal_monthly_sub_category",
    "goal_creation_month",
]
