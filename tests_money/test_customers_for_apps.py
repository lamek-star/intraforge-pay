"""Customer wallets opened for another application in the shop (IntraForge ADR-0022: the Movie Shop owner approves a
portal customer and adds them to IF Pay), and the customer's own password replacing the one-time first password."""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from ifpay import config, directory, runtime
from ifpay.errors import DomainError, Forbidden
from ifpay.platform import TOKEN_HEADER, Instance

APP = str(uuid.uuid4())
SERVICE_TOKEN = "test-service-token-" + uuid.uuid4().hex


def source(external_id="customer-1", app_id=APP):
    return {"app_id": app_id, "app_name": "Movie Shop", "external_id": external_id}


def test_only_a_person_holding_customers_manage_opens_a_wallet(shop):
    with pytest.raises(Forbidden):
        shop.run(directory.create_customer_for_app, actor=shop.cashier, username="selam", display_name="Selam",
                 source=source())
    user, password = shop.run(directory.create_customer_for_app, actor=shop.maker, username="Selam T.",
                              display_name="Selam Tesfaye", source=source())
    assert user["role"] == "customer" and user["must_change_password"]
    assert user["username"] == "selam-t"
    assert re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}", password)
    assert shop.balance("customer", user["id"]) == 0  # an ordinary wallet, empty


def test_asking_twice_for_the_same_record_opens_one_wallet_and_no_second_password(shop):
    first, password = shop.run(directory.create_customer_for_app, actor=shop.maker, username="abel",
                               display_name="Abel", source=source("c-7"))
    again, none = shop.run(directory.create_customer_for_app, actor=shop.maker, username="abel",
                           display_name="Abel", source=source("c-7"))
    assert password and none is None and again["id"] == first["id"]
    # The same record number from another application is another customer.
    other, _ = shop.run(directory.create_customer_for_app, actor=shop.maker, username="abel", display_name="Abel",
                        source=source("c-7", app_id=str(uuid.uuid4())))
    assert other["id"] != first["id"] and other["username"] == "abel-2"


def test_a_taken_username_gets_the_next_number(shop):
    user, _ = shop.run(directory.create_customer_for_app, actor=shop.maker, username="customer1",
                       display_name="Another customer", source=source("c-9"))
    assert user["username"] == "customer1-2"


def test_the_database_keeps_the_source_on_customers_only(shop):
    with shop.conn() as conn, pytest.raises(Exception):
        conn.execute("UPDATE users SET source_app_id = %s, source_external_id = 'x' WHERE id = %s",
                     (APP, shop.cashier["id"]))
        conn.commit()
    with shop.conn() as conn, pytest.raises(Exception):
        conn.execute("UPDATE users SET source_app_id = %s WHERE id = %s", (APP, shop.customer["id"]))
        conn.commit()


def test_the_customer_replaces_the_first_password(shop):
    user, password = shop.run(directory.create_customer_for_app, actor=shop.maker, username="hana",
                              display_name="Hana", source=source("c-3"))
    with pytest.raises(DomainError) as wrong:
        shop.run(directory.change_own_password, user=user, current_password="nope", new_password="a-new-password-1")
    assert wrong.value.code == "wrong_password"
    with pytest.raises(DomainError) as weak:
        shop.run(directory.change_own_password, user=user, current_password=password, new_password="short")
    assert weak.value.code == "weak_password"
    changed = shop.run(directory.change_own_password, user=user, current_password=password,
                       new_password="Hana-own-password-1")
    assert not changed["must_change_password"]


# ---- over HTTP ------------------------------------------------------------------------------------------------------
@pytest.fixture
def api(shop, monkeypatch):
    monkeypatch.setenv("IFPAY_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("IFPAY_COOKIE_SECURE", "false")
    config.get_settings.cache_clear()
    monkeypatch.setattr(runtime, "_instance", Instance(
        deployment="pay-1", launch_path="/apps/pay-1/", app_id=str(uuid.uuid4()), display_name="IntraForge Pay",
        shop_name="Test Shop", organization_id=str(uuid.uuid4()), organization_name="Test", schema="ifpay",
        db_role="ifpay", enabled=True))
    from ifpay.web.app import app

    yield TestClient(app)
    config.get_settings.cache_clear()


def person(capabilities):
    return {"id": str(uuid.uuid4()), "display_name": "Owner", "capabilities": capabilities}


def open_wallet(api, capabilities=("ifpay.customers.manage",), token=SERVICE_TOKEN, external_id="c-1"):
    return api.post("/internal/customers", headers={TOKEN_HEADER: token},
                    json={"actor": person(list(capabilities)), "username": "selam", "display_name": "Selam",
                          "source": source(external_id)})


def test_the_internal_endpoint_needs_the_service_token_and_the_capability(api):
    assert open_wallet(api, token="wrong").status_code == 401
    assert open_wallet(api, capabilities=("ifpay.checkout",)).status_code == 403
    r = open_wallet(api)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] and body["first_password"] and body["must_change_password"]
    assert body["balance_minor"] == 0
    again = open_wallet(api).json()
    assert (again["id"], again["created"], again["first_password"]) == (body["id"], False, None)


def test_only_the_application_that_opened_a_wallet_can_look_it_up(api, shop):
    opened = open_wallet(api).json()
    mine = api.get(f"/internal/customers/{opened['id']}", params={"source_app_id": APP},
                   headers={TOKEN_HEADER: SERVICE_TOKEN})
    assert mine.status_code == 200 and mine.json()["username"] == "selam"
    theirs = api.get(f"/internal/customers/{opened['id']}", params={"source_app_id": str(uuid.uuid4())},
                     headers={TOKEN_HEADER: SERVICE_TOKEN})
    assert theirs.status_code == 404
    walk_in = api.get(f"/internal/customers/{shop.customer['id']}", params={"source_app_id": APP},
                      headers={TOKEN_HEADER: SERVICE_TOKEN})
    assert walk_in.status_code == 404  # a wallet the shop opened itself is not another application's


def test_the_wallet_does_nothing_until_the_customer_chooses_a_password(api):
    opened = open_wallet(api).json()
    login = api.post("/api/auth/login", json={"username": opened["username"], "password": opened["first_password"]})
    assert login.status_code == 200, login.text
    csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
    assert login.json()["user"]["must_change_password"]
    blocked = api.get("/api/wallet")
    assert (blocked.status_code, blocked.json()["code"]) == (403, "choose_password")
    changed = api.post("/api/wallet/password", headers=csrf,
                       json={"current_password": opened["first_password"], "new_password": "Selam-own-password-1"})
    assert changed.status_code == 200, changed.text
    assert api.get("/api/wallet").status_code == 200
    # The first password no longer works.
    fresh = TestClient(api.app)
    old = fresh.post("/api/auth/login", json={"username": opened["username"], "password": opened["first_password"]})
    assert old.status_code == 401


def test_changing_the_password_needs_the_csrf_token(api):
    opened = open_wallet(api).json()
    api.post("/api/auth/login", json={"username": opened["username"], "password": opened["first_password"]})
    r = api.post("/api/wallet/password",
                 json={"current_password": opened["first_password"], "new_password": "Selam-own-password-1"})
    assert r.status_code == 403
