"""Refunds and cashback, treated as money: who may do what, what the journal says, and what the database refuses even
if the application code were wrong."""

from __future__ import annotations

import psycopg
import pytest

from ifpay import checkout, qr, refunds, rewards
from ifpay.errors import Conflict, DomainError, Forbidden


def set_rate(shop, rate_bp: int, cap_minor: int = 100000) -> None:
    change = shop.run(rewards.propose, actor=shop.maker, rate_bp=rate_bp, daily_cap_minor=cap_minor, reason="pilot rate")
    shop.run(rewards.decide, actor=shop.supervisor, change_id=change["id"], approve=True)


# ---- cashback rate: maker/checker -------------------------------------------------------------------------------------

def test_no_cashback_until_a_rate_is_approved(shop):
    receipt = shop.sale(shop.customer, 10000)
    assert "cashback" not in receipt["body"]
    proposal = shop.run(rewards.propose, actor=shop.maker, rate_bp=500, daily_cap_minor=5000, reason="launch")
    receipt = shop.sale(shop.customer, 10000)
    assert "cashback" not in receipt["body"]  # proposed, not approved
    shop.run(rewards.decide, actor=shop.supervisor, change_id=proposal["id"], approve=True)
    receipt = shop.sale(shop.customer, 10000)
    assert receipt["body"]["cashback"]["amount_minor"] == 500
    assert receipt["body"]["cashback"]["rate_percent"] == "5.00"


def test_proposer_cannot_approve_and_cashier_cannot_propose_or_approve(shop):
    proposal = shop.run(rewards.propose, actor=shop.maker, rate_bp=200, daily_cap_minor=5000, reason="x")
    with pytest.raises(Forbidden):  # the maker lacks the supervisor capability
        shop.run(rewards.decide, actor=shop.maker, change_id=proposal["id"], approve=True)
    with pytest.raises(Forbidden):
        shop.run(rewards.propose, actor=shop.cashier, rate_bp=2000, daily_cap_minor=100000, reason="me")
    with pytest.raises(Forbidden):
        shop.run(rewards.decide, actor=shop.cashier, change_id=proposal["id"], approve=True)


def test_a_supervisor_cannot_approve_their_own_proposal_even_holding_both_roles(shop):
    both = shop.staff("Owner with both roles", "ifpay.allocations.request", "ifpay.allocations.approve")
    proposal = shop.run(rewards.propose, actor=both, rate_bp=300, daily_cap_minor=5000, reason="x")
    with pytest.raises(Forbidden):
        shop.run(rewards.decide, actor=both, change_id=proposal["id"], approve=True)


def test_the_database_refuses_self_approval_editing_and_a_second_decision(shop):
    proposal = shop.run(rewards.propose, actor=shop.maker, rate_bp=300, daily_cap_minor=5000, reason="x")
    with shop.conn() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE cashback_rate_changes SET status='approved', decided_by=requested_by, decided_at=now() WHERE id=%s",
                     (proposal["id"],))
    shop.run(rewards.decide, actor=shop.supervisor, change_id=proposal["id"], approve=True)
    with shop.conn() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE cashback_rate_changes SET rate_bp = 2000 WHERE id = %s", (proposal["id"],))
    with shop.conn() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM cashback_rate_changes WHERE id = %s", (proposal["id"],))


def test_rate_and_cap_limits(shop):
    for rate, cap in ((2001, 1000), (-1, 1000), (500, 100001), (500, -1)):
        with pytest.raises(DomainError):
            shop.run(rewards.propose, actor=shop.maker, rate_bp=rate, daily_cap_minor=cap, reason="x")
    with shop.conn() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO cashback_rate_changes (rate_bp, daily_cap_minor, reason, requested_by) VALUES (5000, 1, 'x', %s)",
                     (shop.maker["id"],))


def test_supervisor_can_change_the_rate_later_and_the_newest_approval_wins(shop):
    set_rate(shop, 200)
    assert shop.sale(shop.customer, 10000)["body"]["cashback"]["amount_minor"] == 200
    set_rate(shop, 1000)
    assert shop.sale(shop.customer, 10000)["body"]["cashback"]["amount_minor"] == 1000
    set_rate(shop, 0)  # switching cashback off is also a supervised change
    assert "cashback" not in shop.sale(shop.customer, 10000)["body"]


# ---- cashback in the ledger --------------------------------------------------------------------------------------------

def test_cashback_comes_from_the_shop_and_the_ledger_still_balances(shop):
    set_rate(shop, 250)
    before_c, before_m = shop.balance("customer", shop.customer["id"]), shop.balance("merchant", shop.merchant["id"])
    shop.sale(shop.customer, 10000)
    assert shop.balance("customer", shop.customer["id"]) == before_c - 10000 + 250
    assert shop.balance("merchant", shop.merchant["id"]) == before_m + 10000 - 250
    assert shop.conservation()["ok"]


def test_cashback_rounds_down_and_respects_the_daily_cap(shop):
    set_rate(shop, 333, cap_minor=500)
    assert shop.sale(shop.customer, 999)["body"]["cashback"]["amount_minor"] == 33  # 33.27 -> 33
    assert shop.sale(shop.customer, 10000)["body"]["cashback"]["amount_minor"] == 333
    assert shop.sale(shop.customer, 10000)["body"]["cashback"]["amount_minor"] == 134  # cap 500 - 366 already
    assert "cashback" not in shop.sale(shop.customer, 10000)["body"]  # cap reached today
    assert shop.sale(shop.customer2, 10000)["body"]["cashback"]["amount_minor"] == 333  # the cap is per customer
    assert shop.conservation()["ok"]


def test_paying_the_same_code_twice_earns_cashback_once(shop):
    set_rate(shop, 500)
    request = shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=10000,
                       merchant_verified=True, ttl_seconds=120)
    code = qr.make_code(request)
    shop.run(checkout.pay, actor=shop.customer, code=code, expected_amount_minor=10000)
    _, newly = shop.run(checkout.pay, actor=shop.customer, code=code, expected_amount_minor=10000)
    assert newly is False
    with shop.conn() as conn:
        assert conn.execute("SELECT count(*) AS n FROM cashback_awards").fetchone()["n"] == 1


def test_the_database_refuses_cashback_above_the_rate_or_a_second_award(shop):
    set_rate(shop, 500)
    shop.sale(shop.customer, 10000)
    with shop.conn() as conn:
        award = conn.execute("SELECT * FROM cashback_awards").fetchone()
    with shop.conn() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("UPDATE cashback_awards SET amount_minor = 9999 WHERE id = %s", (award["id"],))
    with shop.conn() as conn, pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            """INSERT INTO cashback_awards (payment_request_id, customer_id, rate_change_id, rate_bp, amount_minor, journal_transaction_id)
               VALUES (%s, %s, %s, 500, 1, %s)""",
            (award["payment_request_id"], award["customer_id"], award["rate_change_id"], award["journal_transaction_id"]))


# ---- refunds -----------------------------------------------------------------------------------------------------------

def test_refund_needs_a_second_person_and_returns_the_money(shop):
    receipt = shop.sale(shop.customer, 10000)
    before = shop.balance("customer", shop.customer["id"])
    refund = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                      receipt_number=receipt["receipt_number"], amount_minor=10000, reason="wrong item")
    assert shop.balance("customer", shop.customer["id"]) == before  # nothing moves on a request
    with pytest.raises(Forbidden):  # a cashier is not a checker
        shop.run(refunds.decide_refund, actor=shop.cashier2, refund_id=refund["id"], approve=True)
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=refund["id"], approve=True)
    assert shop.balance("customer", shop.customer["id"]) == before + 10000
    assert shop.conservation()["ok"]


def test_nobody_approves_their_own_refund(shop):
    both = shop.staff("Owner with both roles", "ifpay.checkout", "ifpay.allocations.approve")
    receipt = shop.sale(shop.customer, 5000)
    refund = shop.run(refunds.request_refund, actor=both, merchant=shop.merchant,
                      receipt_number=receipt["receipt_number"], amount_minor=5000, reason="x")
    with pytest.raises(Forbidden):
        shop.run(refunds.decide_refund, actor=both, refund_id=refund["id"], approve=True)
    with shop.conn() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE refunds SET status='rejected', decided_by=requested_by, decided_at=now() WHERE id=%s",
                     (refund["id"],))


def test_refunds_never_exceed_the_payment(shop):
    receipt = shop.sale(shop.customer, 10000)
    first = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                     receipt_number=receipt["receipt_number"], amount_minor=6000, reason="part")
    with pytest.raises(Conflict):  # 6,000 pending + 5,000 > 10,000
        shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                 receipt_number=receipt["receipt_number"], amount_minor=5000, reason="too much")
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=first["id"], approve=True)
    second = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                      receipt_number=receipt["receipt_number"], amount_minor=4000, reason="rest")
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=second["id"], approve=True)
    with pytest.raises(Conflict):
        shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                 receipt_number=receipt["receipt_number"], amount_minor=1, reason="again")
    assert shop.conservation()["ok"]


def test_the_database_refuses_refunds_above_the_payment_even_if_the_code_were_wrong(shop):
    receipt = shop.sale(shop.customer, 10000)
    with shop.conn() as conn:
        pr = receipt["payment_request_id"]
        conn.execute("INSERT INTO refunds (payment_request_id, amount_minor, reason, requested_by) VALUES (%s, 10000, 'a', %s)",
                     (pr, shop.cashier["id"]))
        conn.execute("INSERT INTO refunds (payment_request_id, amount_minor, reason, requested_by) VALUES (%s, 10000, 'b', %s)",
                     (pr, shop.cashier["id"]))
        conn.commit()
        ids = [r["id"] for r in conn.execute("SELECT id FROM refunds").fetchall()]
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=ids[0], approve=True)
    with pytest.raises(Conflict):  # the code refuses
        shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=ids[1], approve=True)
    with shop.conn() as conn, pytest.raises(psycopg.errors.CheckViolation):  # and so does the database
        tx = conn.execute("INSERT INTO journal_transactions (kind, reference, created_by) VALUES ('refund', 'X', %s) RETURNING id",
                          (shop.supervisor["id"],)).fetchone()
        conn.execute("""INSERT INTO journal_entries (transaction_id, account_id, side, amount_minor)
                        SELECT %s, id, CASE WHEN kind='merchant_payable' THEN 'debit' ELSE 'credit' END, 1
                        FROM ledger_accounts WHERE merchant_id = %s OR customer_id = %s""",
                     (tx["id"], shop.merchant["id"], shop.customer["id"]))
        conn.execute("""UPDATE refunds SET status='approved', decided_by=%s, decided_at=now(), journal_transaction_id=%s
                        WHERE id=%s""", (shop.supervisor["id"], tx["id"], ids[1]))
        conn.commit()


def test_a_refund_takes_back_the_matching_cashback(shop):
    set_rate(shop, 1000)  # 10 %
    receipt = shop.sale(shop.customer, 10000)  # cashback 1,000
    start = shop.balance("customer", shop.customer["id"])
    half = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                    receipt_number=receipt["receipt_number"], amount_minor=5000, reason="half")
    approved = shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=half["id"], approve=True)
    assert approved["cashback_reversed_minor"] == 500
    assert shop.balance("customer", shop.customer["id"]) == start + 4500
    rest = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                    receipt_number=receipt["receipt_number"], amount_minor=5000, reason="rest")
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=rest["id"], approve=True)
    # Fully refunded: the customer is exactly where they were before buying, the cashback fully taken back.
    assert shop.balance("customer", shop.customer["id"]) == 100000
    with shop.conn() as conn:
        assert rewards.earned_total(conn, shop.customer["id"]) == 0
    assert shop.conservation()["ok"]


def test_uneven_partial_refunds_take_back_exactly_the_whole_cashback(shop):
    set_rate(shop, 700)  # 7 % of 9,999 = 699
    receipt = shop.sale(shop.customer, 9999)
    for amount in (3333, 3333, 3333):
        r = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                     receipt_number=receipt["receipt_number"], amount_minor=amount, reason="part")
        shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=r["id"], approve=True)
    with shop.conn() as conn:
        taken = conn.execute("SELECT SUM(cashback_reversed_minor) AS n FROM refunds").fetchone()["n"]
    assert taken == 699
    assert shop.balance("customer", shop.customer["id"]) == 100000
    assert shop.conservation()["ok"]


def test_rejected_refund_moves_nothing_and_cannot_be_decided_again(shop):
    receipt = shop.sale(shop.customer, 5000)
    before = shop.balance("customer", shop.customer["id"])
    r = shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                 receipt_number=receipt["receipt_number"], amount_minor=5000, reason="x")
    shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=r["id"], approve=False, note="no")
    assert shop.balance("customer", shop.customer["id"]) == before
    with pytest.raises(Conflict):
        shop.run(refunds.decide_refund, actor=shop.supervisor, refund_id=r["id"], approve=True)


def test_refund_only_for_a_paid_payment_of_this_shop(shop):
    with pytest.raises(Exception):
        shop.run(refunds.request_refund, actor=shop.cashier, merchant=shop.merchant,
                 receipt_number="IFP-00000000-999999", amount_minor=100, reason="ghost")
    with pytest.raises(Forbidden):  # the supervisor here holds no checkout capability
        receipt = shop.sale(shop.customer, 100)
        shop.run(refunds.request_refund, actor=shop.supervisor, merchant=shop.merchant,
                 receipt_number=receipt["receipt_number"], amount_minor=100, reason="x")
