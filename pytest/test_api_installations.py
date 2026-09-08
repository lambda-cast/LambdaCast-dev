"""
test_api_installations.py
=========================
Integration tests for the installation REST API endpoints.
Uses Flask's test client with an isolated temp database per test session.

Strategy: we inject the temp DB path via an env var that PlatformDatabase
reads, avoiding any monkeypatching of __init__ (which causes recursion).
"""

import os
import sys
import pytest

# Make backend importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


# ──────────────────────────────────────────────────────────────────────────────
# Session-scoped temp DB + Flask test client
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def temp_db_path(tmp_path_factory):
    return str(tmp_path_factory.mktemp("api_test") / "platform_test.db")


@pytest.fixture(scope="module")
def client(temp_db_path):
    """
    Create a Flask test client that uses a temp platform DB.
    We inject the path before importing server so PlatformDatabase picks it up.
    """
    os.environ["SUNALYZER_PLATFORM_DB"] = temp_db_path

    # Import server after setting the env var
    import server as server_mod

    # Provide a minimal config so the existing endpoints don't crash
    class FakeConfig:
        log_level = 20
        config_data = {
            "sunalyzer": {"name": "Test"},
            "prices": {
                "price_per_grid_kwh": 0.3,
                "revenue_per_fed_in_kwh": 0.08,
            },
            "device": {"start_date": "2020-01-01"},
        }

    server_mod.config = FakeConfig()
    server_mod.app.config["TESTING"] = True

    with server_mod.app.test_client() as c:
        yield c

    # Cleanup
    del os.environ["SUNALYZER_PLATFORM_DB"]


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(client, username: str, password: str = "password123") -> str:
    """Register a user (ignore if already exists) and return their JWT."""
    client.post("/api/auth/register", json={
        "username": username,
        "email": f"{username}@test.com",
        "password": password,
    })
    resp = client.post("/api/auth/login", json={
        "username": username,
        "password": password,
    })
    assert resp.status_code == 200, f"Login failed for {username}: {resp.get_json()}"
    return resp.get_json()["access_token"]


# ──────────────────────────────────────────────────────────────────────────────
# Auth endpoint tests
# ──────────────────────────────────────────────────────────────────────────────

class TestAuthEndpoints:

    def test_register_success(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "auth_test_user",
            "email": "auth_test@test.com",
            "password": "password123",
        })
        assert resp.status_code == 201
        assert "user_id" in resp.get_json()

    def test_register_duplicate_username(self, client):
        client.post("/api/auth/register", json={
            "username": "dup_user", "email": "dup1@test.com", "password": "password123"
        })
        resp = client.post("/api/auth/register", json={
            "username": "dup_user", "email": "dup2@test.com", "password": "password123"
        })
        assert resp.status_code == 409

    def test_register_short_password(self, client):
        resp = client.post("/api/auth/register", json={
            "username": "shortpwd", "email": "short@test.com", "password": "123"
        })
        assert resp.status_code == 400

    def test_login_success(self, client):
        client.post("/api/auth/register", json={
            "username": "login_ok", "email": "loginok@test.com", "password": "password123"
        })
        resp = client.post("/api/auth/login", json={
            "username": "login_ok", "password": "password123"
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "access_token" in data
        assert data["user"]["username"] == "login_ok"

    def test_login_wrong_password(self, client):
        client.post("/api/auth/register", json={
            "username": "wrongpwd", "email": "wp@test.com", "password": "correct123"
        })
        resp = client.post("/api/auth/login", json={
            "username": "wrongpwd", "password": "wrong"
        })
        assert resp.status_code == 401

    def test_login_by_email(self, client):
        client.post("/api/auth/register", json={
            "username": "emaillogin", "email": "emaillogin@test.com", "password": "password123"
        })
        resp = client.post("/api/auth/login", json={
            "username": "emaillogin@test.com", "password": "password123"
        })
        assert resp.status_code == 200

    def test_me_requires_auth(self, client):
        # Delete any lingering auth cookie before testing the unauthenticated path
        client.delete_cookie("access_token")
        resp = client.get("/api/auth/me")
        assert resp.status_code == 401

    def test_me_returns_profile(self, client):
        token = register_and_login(client, "me_user")
        resp = client.get("/api/auth/me", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["username"] == "me_user"
        assert data["role"] == "USER"

    def test_logout(self, client):
        resp = client.post("/api/auth/logout")
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────────
# Installation CRUD tests
# ──────────────────────────────────────────────────────────────────────────────

class TestInstallationEndpoints:

    def test_list_returns_empty_for_new_user(self, client):
        token = register_and_login(client, "list_empty_user")
        resp = client.get("/api/installations", headers=auth_header(token))
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_unauthenticated_access_denied(self, client):
        client.delete_cookie("access_token")
        resp = client.get("/api/installations")
        assert resp.status_code == 401

    def test_create_minimal(self, client):
        token = register_and_login(client, "creator_min")
        resp = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Minimal Site",
        })
        assert resp.status_code == 201
        assert "id" in resp.get_json()

    def test_create_full(self, client):
        token = register_and_login(client, "creator_full")
        resp = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Full Site",
            "installed_capacity_kwp": 10.5,
            "latitude": 36.8190,
            "longitude": 10.1660,
            "city": "Tunis",
            "region": "Tunis",
            "country": "Tunisia",
            "panel_manufacturer": "JinkoSolar",
            "panel_model": "Tiger Neo",
            "panel_technology": "monocrystalline",
            "panel_power_wp": 415.0,
            "number_of_panels": 24,
            "tilt": 30.0,
            "azimuth": 180.0,
            "inverter_manufacturer": "Huawei",
            "inverter_model": "SUN2000-10KTL",
            "inverter_capacity_kw": 10.0,
            "installation_date": "2023-06-01",
            "status": "active",
        })
        assert resp.status_code == 201
        inst_id = resp.get_json()["id"]

        # Verify round-trip
        get_resp = client.get(f"/api/installations/{inst_id}", headers=auth_header(token))
        assert get_resp.status_code == 200
        data = get_resp.get_json()
        assert data["name"] == "Full Site"
        assert data["city"] == "Tunis"
        assert abs(data["latitude"] - 36.8190) < 0.001
        assert data["panel_manufacturer"] == "JinkoSolar"
        assert data["number_of_panels"] == 24

    def test_create_requires_name(self, client):
        token = register_and_login(client, "noname_user")
        resp = client.post("/api/installations", headers=auth_header(token), json={
            "installed_capacity_kwp": 5.0,
        })
        assert resp.status_code == 400

    def test_invalid_latitude(self, client):
        token = register_and_login(client, "badlat_user")
        resp = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Bad Lat",
            "latitude": 200.0,
        })
        assert resp.status_code == 400

    def test_invalid_status(self, client):
        token = register_and_login(client, "badstatus_user")
        resp = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Bad Status",
            "status": "flying",
        })
        assert resp.status_code == 400

    def test_update(self, client):
        token = register_and_login(client, "updater_user")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "To Update",
        }).get_json()["id"]

        resp = client.patch(
            f"/api/installations/{inst_id}",
            headers=auth_header(token),
            json={"status": "maintenance", "city": "Sfax"},
        )
        assert resp.status_code == 200

        data = client.get(f"/api/installations/{inst_id}", headers=auth_header(token)).get_json()
        assert data["status"] == "maintenance"
        assert data["city"] == "Sfax"

    def test_delete(self, client):
        token = register_and_login(client, "deleter_user")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "To Delete",
        }).get_json()["id"]

        resp = client.delete(f"/api/installations/{inst_id}", headers=auth_header(token))
        assert resp.status_code == 200

        get_resp = client.get(f"/api/installations/{inst_id}", headers=auth_header(token))
        assert get_resp.status_code == 404

    def test_user_cannot_see_other_users_installation(self, client):
        owner_token = register_and_login(client, "owner_isolation")
        other_token = register_and_login(client, "other_isolation")

        inst_id = client.post("/api/installations", headers=auth_header(owner_token), json={
            "name": "Private",
        }).get_json()["id"]

        # Other user gets 404 (not 403 — we don't leak the existence)
        resp = client.get(f"/api/installations/{inst_id}", headers=auth_header(other_token))
        assert resp.status_code == 404

    def test_user_cannot_delete_other_users_installation(self, client):
        owner_token = register_and_login(client, "owner_del")
        other_token = register_and_login(client, "other_del")

        inst_id = client.post("/api/installations", headers=auth_header(owner_token), json={
            "name": "Protected",
        }).get_json()["id"]

        resp = client.delete(f"/api/installations/{inst_id}", headers=auth_header(other_token))
        assert resp.status_code == 404

    def test_list_only_returns_own_installations(self, client):
        token_a = register_and_login(client, "list_a")
        token_b = register_and_login(client, "list_b")

        client.post("/api/installations", headers=auth_header(token_a), json={"name": "A Site"})
        client.post("/api/installations", headers=auth_header(token_b), json={"name": "B Site"})

        list_a = client.get("/api/installations", headers=auth_header(token_a)).get_json()
        list_b = client.get("/api/installations", headers=auth_header(token_b)).get_json()

        names_a = {i["name"] for i in list_a}
        names_b = {i["name"] for i in list_b}

        assert "A Site" in names_a
        assert "B Site" not in names_a
        assert "B Site" in names_b
        assert "A Site" not in names_b


# ──────────────────────────────────────────────────────────────────────────────
# PVGIS and Forecast interface tests
# ──────────────────────────────────────────────────────────────────────────────

class TestPVGISAndForecast:

    def test_pvgis_with_coords_returns_ok(self, client):
        """Phase 2: real PVGIS service (mocked) returns status ok, not stub."""
        token = register_and_login(client, "pvgis_tester")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "PVGIS Site",
            "installed_capacity_kwp": 5.0,
            "latitude": 36.8,
            "longitude": 10.1,
            "tilt": 30.0,
            "azimuth": 180.0,
        }).get_json()["id"]

        resp = client.get(f"/api/installations/{inst_id}/pvgis", headers=auth_header(token))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "data" in data
        assert data["data"]["annual_production_kwh"] is not None

    def test_pvgis_no_coords_returns_422(self, client):
        """Missing lat/lon → 422 Unprocessable Entity."""
        token = register_and_login(client, "pvgis_monthly")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Monthly PVGIS",
            "installed_capacity_kwp": 8.0,
        }).get_json()["id"]

        resp = client.get(f"/api/installations/{inst_id}/pvgis", headers=auth_header(token))
        assert resp.status_code == 422
        assert resp.get_json()["status"] == "missing_location"

    def test_pvgis_refresh_endpoint(self, client):
        """POST /pvgis/refresh forces a new API call and returns source=pvgis_api."""
        token = register_and_login(client, "pvgis_invalid")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Refresh Test",
            "latitude": 36.8, "longitude": 10.1,
        }).get_json()["id"]

        resp = client.post(
            f"/api/installations/{inst_id}/pvgis/refresh",
            headers=auth_header(token),
        )
        assert resp.status_code == 200
        assert resp.get_json()["source"] == "pvgis_api"

    def test_forecast_input_structure(self, client):
        token = register_and_login(client, "forecast_tester")
        inst_id = client.post("/api/installations", headers=auth_header(token), json={
            "name": "Forecast Site",
            "installed_capacity_kwp": 12.0,
            "latitude": 36.8,
            "longitude": 10.1,
            "panel_technology": "monocrystalline",
            "tilt": 30.0,
            "azimuth": 180.0,
        }).get_json()["id"]

        resp = client.get(
            f"/api/installations/{inst_id}/forecast-input",
            headers=auth_header(token),
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "installation" in data
        assert "location" in data
        assert "panel_config" in data
        assert "inverter_config" in data
        assert "capacity" in data
        assert "historical_production" in data
        assert "weather_data" in data
        assert "pvgis_baseline" in data
        assert "metadata" in data
        assert data["location"]["country"] == "Tunisia"
        assert data["metadata"]["data_version"] == "1.0"

