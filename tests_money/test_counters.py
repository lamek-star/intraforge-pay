"""More than one counter: the Pay Manager names counters, each payment request records the cashier's counter, the
database refuses another shop's counter or changing it afterwards, and the shop screen's day is per counter and in the
shop's own time zone."""

from __future__ import annotations

import psycopg
import pytest

from ifpay import checkout, counters, directory, qr
from ifpay.errors import Conflict, DomainError, Forbidden


def test_only_the_pay_manager_adds_counters_and_names_are_unique(shop):
    with pytest.raises(Forbidden):
        shop.run(counters.create_counter, actor=shop.cashier, name="Counter 1")
    one = shop.run(counters.create_counter, actor=shop.maker, name="  Counter   1 ")
    assert one["name"] == "Counter 1"
    with pytest.raises(Conflict):
        shop.run(counters.create_counter, actor=shop.maker, name="counter 1")
    with pytest.raises(DomainError):
        shop.run(counters.create_counter, actor=shop.maker, name="")
    assert [c["name"] for c in shop.run(counters.list_counters, actor=shop.cashier)] == ["Counter 1"]


def test_a_sale_records_its_counter_and_a_disabled_or_foreign_counter_is_refused(shop):
    front = shop.run(counters.create_counter, actor=shop.maker, name="Front")
    request = shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=500, merchant_verified=True,
                       ttl_seconds=120, counter_id=front["id"])
    assert request["counter_id"] == front["id"]
    shop.run(checkout.pay, actor=shop.customer, code=qr.make_code(request), expected_amount_minor=500)
    shop.run(counters.set_active, actor=shop.maker, counter_id=front["id"], active=False)
    with pytest.raises(DomainError):
        shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=500, merchant_verified=True,
                 ttl_seconds=120, counter_id=front["id"])
    with shop.conn() as conn:
        other = directory.create_merchant(conn, name="Other Shop")
        foreign = conn.execute("INSERT INTO counters (merchant_id, name) VALUES (%s, 'Theirs') RETURNING id",
                               (other["id"],)).fetchone()["id"]
        conn.commit()
    with pytest.raises(DomainError):
        shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=500, merchant_verified=True,
                 ttl_seconds=120, counter_id=foreign)


def test_the_database_itself_refuses_a_foreign_counter_and_a_changed_counter(shop):
    mine = shop.run(counters.create_counter, actor=shop.maker, name="Mine")
    request = shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=300, merchant_verified=True,
                       ttl_seconds=120, counter_id=mine["id"])
    with shop.conn() as conn:
        other = directory.create_merchant(conn, name="Elsewhere")
        foreign = conn.execute("INSERT INTO counters (merchant_id, name) VALUES (%s, 'X') RETURNING id",
                               (other["id"],)).fetchone()["id"]
        conn.commit()
    with shop.conn() as conn, pytest.raises(psycopg.Error):
        conn.execute("UPDATE payment_requests SET counter_id = NULL WHERE id = %s", (request["id"],))
    with shop.conn() as conn, pytest.raises(psycopg.Error):
        conn.execute(
            """INSERT INTO payment_requests (merchant_id, amount_minor, created_by, expires_at, merchant_verified, counter_id)
               VALUES (%s, 100, %s, now() + interval '1 minute', true, %s)""",
            (shop.merchant["id"], shop.cashier["id"], foreign),
        )


def test_the_shop_screen_shows_today_per_counter_in_the_shops_time_zone(shop):
    from ifpay.web.api_shop import overview

    front = shop.run(counters.create_counter, actor=shop.maker, name="Front")
    back = shop.run(counters.create_counter, actor=shop.maker, name="Back")
    for counter, amount in ((front, 500), (front, 250), (back, 100), (None, 50)):
        request = shop.run(checkout.create_payment_request, actor=shop.cashier, amount_minor=amount,
                           merchant_verified=True, ttl_seconds=120, counter_id=counter["id"] if counter else None)
        shop.run(checkout.pay, actor=shop.customer, code=qr.make_code(request), expected_amount_minor=amount)
    everything = overview(tz="Asia/Dubai", counter=None, user=shop.cashier)
    assert everything["time_zone"] == "Asia/Dubai"
    assert everything["today_total"] == "9.00" and everything["counter_total"] is None
    by_name = {c["counter"]: (c["total"], c["sales"]) for c in everything["counters_today"]}
    assert by_name == {"Front": ("7.50", 2), "Back": ("1.00", 1), "No counter": ("0.50", 1)}
    mine = overview(tz="Not/AZone", counter=front["id"], user=shop.cashier)
    assert mine["time_zone"] == "UTC"  # an unknown zone falls back, never reaches SQL as given
    assert mine["counter_total"] == "7.50" and len(mine["today"]) == 2
    assert {t["counter"] for t in mine["today"]} == {"Front"}
