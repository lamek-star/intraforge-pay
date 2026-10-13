"""The double-entry journal.

`post_transaction` is the only code that writes journal rows, and balances are only ever read from the
`account_balances` view (the sum of the journal). PostgreSQL re-checks every rule at commit -- see 0001_initial.sql.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import DomainError, NotFound


@dataclass(frozen=True)
class Entry:
    account_id: object
    side: str  # "debit" | "credit"
    amount_minor: int


def post_transaction(conn, *, kind: str, reference: str, memo: str, created_by, entries: list[Entry]) -> dict:
    if len(entries) < 2:
        raise ValueError("a journal transaction needs at least two entries")
    debits = sum(e.amount_minor for e in entries if e.side == "debit")
    credits = sum(e.amount_minor for e in entries if e.side == "credit")
    if any(e.side not in ("debit", "credit") or e.amount_minor <= 0 for e in entries) or debits != credits:
        raise ValueError("journal transaction does not balance")

    tx = conn.execute(
        """INSERT INTO journal_transactions (kind, reference, memo, created_by)
           VALUES (%s, %s, %s, %s) RETURNING id, seq, kind, reference, memo, created_at""",
        (kind, reference, memo, created_by),
    ).fetchone()
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO journal_entries (transaction_id, account_id, side, amount_minor) VALUES (%s, %s, %s, %s)",
            [(tx["id"], e.account_id, e.side, e.amount_minor) for e in entries],
        )
    return tx


def lock_account(conn, account_id) -> None:
    """Serialise every balance check-then-debit on one account (released at commit/rollback)."""
    conn.execute("SELECT 1 FROM ledger_accounts WHERE id = %s FOR UPDATE", (account_id,))


def balance_minor(conn, account_id) -> int:
    row = conn.execute("SELECT balance_minor FROM account_balances WHERE account_id = %s", (account_id,)).fetchone()
    if row is None:
        raise NotFound("account_not_found", "Ledger account not found.")
    return int(row["balance_minor"])


def customer_account(conn, customer_id) -> dict:
    row = conn.execute("SELECT * FROM ledger_accounts WHERE customer_id = %s", (customer_id,)).fetchone()
    if row is None:
        raise NotFound("wallet_not_found", "This customer has no wallet.")
    return row


def merchant_account(conn, merchant_id) -> dict:
    row = conn.execute("SELECT * FROM ledger_accounts WHERE merchant_id = %s", (merchant_id,)).fetchone()
    if row is None:
        raise NotFound("merchant_account_not_found", "This merchant has no payable account.")
    return row


def issuance_reserve(conn) -> dict:
    return conn.execute("SELECT * FROM ledger_accounts WHERE kind = 'issuance_reserve'").fetchone()


def open_customer_account(conn, user: dict) -> dict:
    return conn.execute(
        """INSERT INTO ledger_accounts (code, name, kind, normal_side, customer_id)
           VALUES (%s, %s, 'customer_liability', 'credit', %s) RETURNING *""",
        (f"CUST-{user['username']}", f"Customer simulated liability — {user['display_name']}", user["id"]),
    ).fetchone()


def open_merchant_account(conn, merchant: dict) -> dict:
    return conn.execute(
        """INSERT INTO ledger_accounts (code, name, kind, normal_side, merchant_id)
           VALUES (%s, %s, 'merchant_payable', 'credit', %s) RETURNING *""",
        (f"MERCH-{str(merchant['id'])[:8]}", f"Merchant simulated payable — {merchant['name']}", merchant["id"]),
    ).fetchone()


def conservation_report(conn) -> dict:
    """The whole-ledger invariants, recomputed from raw journal rows (not from the view)."""
    totals = conn.execute(
        """SELECT COALESCE(SUM(amount_minor) FILTER (WHERE side = 'debit'), 0)::bigint AS debits,
                  COALESCE(SUM(amount_minor) FILTER (WHERE side = 'credit'), 0)::bigint AS credits,
                  count(*) AS entries
           FROM journal_entries"""
    ).fetchone()
    unbalanced = conn.execute(
        """SELECT count(*) AS n FROM (
             SELECT t.id FROM journal_transactions t LEFT JOIN journal_entries e ON e.transaction_id = t.id
             GROUP BY t.id
             HAVING count(e.id) < 2
                 OR COALESCE(SUM(e.amount_minor) FILTER (WHERE e.side = 'debit'), 0)
                    <> COALESCE(SUM(e.amount_minor) FILTER (WHERE e.side = 'credit'), 0)) x"""
    ).fetchone()["n"]
    by_kind = {
        row["kind"]: int(row["total"])
        for row in conn.execute("SELECT kind, COALESCE(SUM(balance_minor), 0)::bigint AS total FROM account_balances GROUP BY kind")
    }
    negative_wallets = conn.execute(
        "SELECT count(*) AS n FROM account_balances WHERE kind = 'customer_liability' AND balance_minor < 0"
    ).fetchone()["n"]
    issued = by_kind.get("issuance_reserve", 0)
    outstanding = by_kind.get("customer_liability", 0) + by_kind.get("merchant_payable", 0)
    ok = (int(totals["debits"]) == int(totals["credits"]) and unbalanced == 0 and negative_wallets == 0
          and issued == outstanding)
    return {
        "ok": ok,
        "total_debits_minor": int(totals["debits"]),
        "total_credits_minor": int(totals["credits"]),
        "entries": int(totals["entries"]),
        "unbalanced_transactions": int(unbalanced),
        "negative_wallets": int(negative_wallets),
        "issued_minor": issued,
        "customer_liability_minor": by_kind.get("customer_liability", 0),
        "merchant_payable_minor": by_kind.get("merchant_payable", 0),
    }


def require_positive(amount_minor: int) -> None:
    if amount_minor <= 0:
        raise DomainError("invalid_amount", "The amount must be greater than zero.")
