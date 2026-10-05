"""Tests for the single stored account and for releasing replaced device registrations."""

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from fastapi.testclient import TestClient
    from audible_downloader import db, web

    db.init_db()
    web.worker.start = lambda: None  # no background work in tests
    with TestClient(web.app) as c:
        yield c


@pytest.fixture
def connected(client):
    from audible_downloader import db

    return db.save_credential({"device_info": {"device_serial_number": "OLD"}}, "natalie")


def test_a_stored_credential_is_all_it_takes_to_be_connected(client, connected):
    """There is no login session: reaching the app at all is the client cert's job."""
    client.cookies.clear()
    assert client.get("/api/me").json() == {"email": "natalie", "authenticated": True}


def test_no_account_connected_is_reported_not_guessed(client):
    assert client.get("/api/me").json() == {"authenticated": False}


def test_library_refuses_when_no_account_is_connected(client):
    assert client.get("/api/library").status_code == 401


def test_books_and_jobs_need_no_account(client):
    """Downloaded books stay reachable after the account is disconnected."""
    from audible_downloader import db

    db.save_book("B0036RARRK", "The Temporal Void", "PFH", "data/downloads/1")
    assert client.get("/api/books").status_code == 200
    assert client.get("/api/jobs").status_code == 200


def test_disconnect_forgets_the_credential(client, connected, monkeypatch):
    from audible_downloader import db, web

    class FakeAuth:
        @staticmethod
        def from_dict(data):
            return FakeAuth()

        def deregister_device(self):
            pass

    monkeypatch.setattr(web.audible, "Authenticator", FakeAuth)
    assert client.post("/api/auth/logout").json() == {"success": True}
    assert db.get_credential() is None


def test_disconnect_forgets_the_credential_even_if_amazon_refuses(client, connected, monkeypatch):
    from audible_downloader import db, web

    class Exploding:
        @staticmethod
        def from_dict(data):
            raise RuntimeError("Amazon said no")

    monkeypatch.setattr(web.audible, "Authenticator", Exploding)
    assert client.post("/api/auth/logout").json() == {"success": True}
    assert db.get_credential() is None


# Releasing the replaced device


def credential(serial, customer="C1"):
    return {"device_info": {"device_serial_number": serial},
            "customer_info": {"user_id": customer}}


def test_replaced_device_is_deregistered(monkeypatch):
    from audible_downloader import web

    calls = []

    class FakeAuth:
        @staticmethod
        def from_dict(data):
            calls.append(data)
            return FakeAuth()

        def deregister_device(self):
            calls.append("deregistered")

    monkeypatch.setattr(web.audible, "Authenticator", FakeAuth)
    web.release_previous_device(credential("OLD"), credential("NEW"))
    assert "deregistered" in calls


def test_another_customers_device_is_never_deregistered(monkeypatch):
    """Two Amazon accounts can share a display name, which is what users are keyed on."""
    from audible_downloader import web

    class Exploding:
        @staticmethod
        def from_dict(data):
            raise AssertionError("should not have been called")

    monkeypatch.setattr(web.audible, "Authenticator", Exploding)
    web.release_previous_device(credential("OLD", "C1"), credential("NEW", "C2"))


def test_deregistration_failure_never_propagates(monkeypatch):
    """A login that worked must not fail over housekeeping."""
    from audible_downloader import web

    class Exploding:
        @staticmethod
        def from_dict(data):
            raise RuntimeError("Amazon said no")

    monkeypatch.setattr(web.audible, "Authenticator", Exploding)
    web.release_previous_device(
        {"device_info": {"device_serial_number": "OLD"}},
        {"device_info": {"device_serial_number": "NEW"}},
    )  # must not raise


def test_the_same_device_is_never_deregistered(monkeypatch):
    """Guard against releasing the credential that was just stored."""
    from audible_downloader import web

    class Exploding:
        @staticmethod
        def from_dict(data):
            raise AssertionError("should not have been called")

    monkeypatch.setattr(web.audible, "Authenticator", Exploding)
    same = {"device_info": {"device_serial_number": "SAME"}}
    web.release_previous_device(same, same)
