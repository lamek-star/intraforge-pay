"""Simultaneous payments, through real concurrent PostgreSQL transactions (one pooled connection per thread)."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from conftest import balance_of, journal_count, merchant_payable

from ifpay import checkout, db, ledger, qr
from ifpay.errors import DomainError


def _request(world, amount_minor):
    with db.connection() as conn:
        row = checkout.create_payment_request(conn, actor=world["cashier"], amount_minor=amount_minor,
                                              merchant_verified=True, ttl_seconds=300)
    return qr.make_code(row)


def _race(fn, args_list):
    barrier = Barrier(len(args_list))

    def run(args):
        barrier.wait()
        try:
            with db.connection() as conn:
                receipt, newly = fn(conn, *args)
            return ("paid" if newly else "replay", receipt["receipt_number"])
        except DomainError as exc:
            return ("refused", exc.code)

    with ThreadPoolExecutor(len(args_list)) as pool:
        return list(pool.map(run, args_list))


def _pay(conn, actor, code, amount):
    return checkout.pay(conn, actor=actor, code=code, expected_amount_minor=amount)


def test_same_qr_confirmed_ten_times_at_once_charges_once(world):
    customer = world["customers"][0]
    code = _request(world, 2_500)
    results = _race(_pay, [(customer, code, 2_500)] * 10)
    assert sum(1 for r in results if r[0] == "paid") == 1
    assert {r[1] for r in results} == {next(r[1] for r in results if r[0] == "paid")}  # all see the same receipt
    assert balance_of("customer1") == 7_500 and journal_count("purchase") == 1 and merchant_payable() == 2_500


def test_five_customers_race_for_one_qr_only_one_pays(world):
    code = _request(world, 1_000)
    results = _race(_pay, [(c, code, 1_000) for c in world["customers"]])
    assert sum(1 for r in results if r[0] == "paid") == 1
    assert sorted(r[1] for r in results if r[0] == "refused") == ["already_paid"] * 4
    assert journal_count("purchase") == 1 and merchant_payable() == 1_000
    assert sum(balance_of(f"customer{i}") for i in range(1, 6)) == 50_000 - 1_000


def test_one_customer_paying_six_qrs_at_once_cannot_overdraw(world):
    customer = world["customers"][0]
    codes = [_request(world, 2_500) for _ in range(6)]  # 150.00 requested against 100.00
    results = _race(_pay, [(customer, code, 2_500) for code in codes])
    assert sum(1 for r in results if r[0] == "paid") == 4
    assert sorted(r[1] for r in results if r[0] == "refused") == ["insufficient_balance"] * 2
    assert balance_of("customer1") == 0 and merchant_payable() == 10_000


def test_many_customers_many_payments_conserve_the_ledger(world):
    jobs = []
    for _round in range(4):
        for customer in world["customers"]:
            jobs.append((customer, _request(world, 1_900), 1_900))
    results = _race(_pay, jobs)
    paid = sum(1 for r in results if r[0] == "paid")
    assert paid == 20  # 4 x 19.00 = 76.00 each, within 100.00
    with db.connection() as conn:
        report = ledger.conservation_report(conn)
    assert report["ok"] and report["merchant_payable_minor"] == 20 * 1_900
    assert len({r[1] for r in results}) == 20  # 20 distinct receipt numbers
