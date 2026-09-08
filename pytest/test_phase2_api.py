"""
test_phase2_api.py
==================
Integration tests for Phase 2 API endpoints:
- PVGIS (cached, refresh, missing location, cache invalidation)
- Location endpoint
- Map GeoJSON (user vs admin visibility)
- Admin fleet statistics
- Location validation (governorate, delegation, tilt, azimuth)

PVGIS HTTP calls are mocked via monkeypatching PVGISService._http_get
at the class level before the app is imported.
"""

import os, sys, json, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

# ─────────────────────────────────────────────────────────────────────────────
# PVGIS mock
# ─────────────────────────────────────────────────────────────────────────────

def _mock_pvgis_response(annual=16000.0):
    monthly = [
        {"month": i, "E_d": 40.0, "E_m": round(annual / 12, 1),
         "H(i)_d": 5.0, "H(i)_m": 158.0, "SD_m": 40.0}
        for i in range(1, 13)
    ]
    return {
        "inputs": {},
        "outputs": {
            "totals": {"fixed": {
                "E_y": annual, "H(i)_y": 1896.0, "l_total": 14.0,
            }},
            "monthly": {"fixed": monthly},
        },
        "meta": {},
    }


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def temp_db(tmp_path_factory):
    return str(tmp_path_factory.mktemp("p2") / "p2.db")


@pytest.fixture(scope="module")
def client(temp_db):
    os.environ["SUNALYZER_PLATFORM_DB"] = temp_db

    # Patch before importing server so the patched version is used everywhere
    import pvgis_service as pvmod
    _orig_init = pvmod.PVGISService.__init__

    def _patched_init(self, raddatabase="PVGIS-SARAH2", loss_pct=14.0, _http_get=None):
        _orig_init(self, raddatabase=raddatabase, loss_pct=loss_pct,
                   _http_get=_http_get or (lambda url, params: _mock_pvgis_response()))

    pvmod.PVGISService.__init__ = _patched_init

    import server as srv

    class FakeCfg:
        log_level = 20
        config_data = {
            "sunalyzer": {"name": "Test"},
            "prices": {"price_per_grid_kwh": 0.3, "revenue_per_fed_in_kwh": 0.08},
            "device": {"start_date": "2020-01-01"},
        }

    srv.config = FakeCfg()
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        yield c

    pvmod.PVGISService.__init__ = _orig_init
    del os.environ["SUNALYZER_PLATFORM_DB"]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def hdr(token):
    return {"Authorization": f"Bearer {token}"}


def register_login(client, username):
    client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@p2.com", "password": "password123"
    })
    r = client.post("/api/auth/login", json={"username": username, "password": "password123"})
    assert r.status_code == 200, f"login failed for {username}: {r.get_json()}"
    return r.get_json()["access_token"]


def create_inst(client, token, name="Site", coords=True):
    data = {"name": name, "installed_capacity_kwp": 10.0, "status": "active"}
    if coords:
        data.update({"latitude": 36.82, "longitude": 10.17, "tilt": 30.0,
                     "azimuth": 180.0, "panel_technology": "monocrystalline",
                     "city": "Tunis", "governorate": "Tunis"})
    r = client.post("/api/installations", headers=hdr(token), json=data)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["id"]


# ─────────────────────────────────────────────────────────────────────────────
# Location validation
# ─────────────────────────────────────────────────────────────────────────────

class TestLocationValidation:

    def test_valid_tilt_accepted(self, client):
        t = register_login(client, "v_tilt_ok")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "latitude": 36.0, "longitude": 9.0, "tilt": 45.0})
        assert r.status_code == 201

    def test_tilt_above_90_rejected(self, client):
        t = register_login(client, "v_tilt_hi")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "tilt": 95.0})
        assert r.status_code == 400

    def test_negative_tilt_rejected(self, client):
        t = register_login(client, "v_tilt_neg")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "tilt": -1.0})
        assert r.status_code == 400

    def test_azimuth_360_accepted(self, client):
        t = register_login(client, "v_az_360")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "azimuth": 360.0})
        assert r.status_code == 201

    def test_azimuth_over_360_rejected(self, client):
        t = register_login(client, "v_az_bad")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "azimuth": 361.0})
        assert r.status_code == 400

    def test_latitude_over_90_rejected(self, client):
        t = register_login(client, "v_lat_bad")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "latitude": 95.0, "longitude": 9.0})
        assert r.status_code == 400

    def test_longitude_over_180_rejected(self, client):
        t = register_login(client, "v_lon_bad")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "T", "latitude": 36.0, "longitude": 200.0})
        assert r.status_code == 400

    def test_governorate_stored(self, client):
        t  = register_login(client, "v_gov")
        id = create_inst(client, t, "Gov")
        d  = client.get(f"/api/installations/{id}", headers=hdr(t)).get_json()
        assert d["governorate"] == "Tunis"

    def test_delegation_stored(self, client):
        t = register_login(client, "v_deleg")
        r = client.post("/api/installations", headers=hdr(t),
                        json={"name": "D", "delegation": "La Marsa",
                              "latitude": 36.9, "longitude": 10.3})
        id = r.get_json()["id"]
        d  = client.get(f"/api/installations/{id}", headers=hdr(t)).get_json()
        assert d["delegation"] == "La Marsa"


# ─────────────────────────────────────────────────────────────────────────────
# Location endpoint
# ─────────────────────────────────────────────────────────────────────────────

class TestLocationEndpoint:

    def test_returns_structured_location(self, client):
        t  = register_login(client, "loc_ep")
        id = create_inst(client, t, "Loc Ep")
        r  = client.get(f"/api/installations/{id}/location", headers=hdr(t))
        assert r.status_code == 200
        d = r.get_json()
        assert d["installation_id"] == id
        assert abs(d["latitude"]  - 36.82) < 0.01
        assert abs(d["longitude"] - 10.17) < 0.01
        assert d["governorate"] == "Tunis"
        assert d["has_coordinates"] is True

    def test_no_coords_flag(self, client):
        t  = register_login(client, "loc_nc")
        id = create_inst(client, t, "NoCoord", coords=False)
        d  = client.get(f"/api/installations/{id}/location", headers=hdr(t)).get_json()
        assert d["has_coordinates"] is False

    def test_other_user_denied(self, client):
        t1 = register_login(client, "loc_own")
        t2 = register_login(client, "loc_oth")
        id = create_inst(client, t1, "Private")
        r  = client.get(f"/api/installations/{id}/location", headers=hdr(t2))
        assert r.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# PVGIS endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestPVGIS:

    def test_get_pvgis_ok(self, client):
        t  = register_login(client, "pv_ok")
        id = create_inst(client, t, "PV OK")
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r.status_code == 200
        d = r.get_json()
        assert d["status"] == "ok"
        assert d["data"]["annual_production_kwh"] > 0

    def test_second_call_is_cached(self, client):
        t  = register_login(client, "pv_cache")
        id = create_inst(client, t, "PV Cache")
        client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        r2 = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r2.get_json()["source"] == "cache"

    def test_refresh_hits_api(self, client):
        t  = register_login(client, "pv_ref")
        id = create_inst(client, t, "PV Refresh")
        client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        r = client.post(f"/api/installations/{id}/pvgis/refresh", headers=hdr(t))
        assert r.status_code == 200
        assert r.get_json()["source"] == "pvgis_api"

    def test_missing_coords_returns_422(self, client):
        t  = register_login(client, "pv_nc")
        id = create_inst(client, t, "PV NoCoord", coords=False)
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r.status_code == 422
        assert r.get_json()["status"] == "missing_location"

    def test_cache_invalidated_on_tilt_change(self, client):
        t  = register_login(client, "pv_inv")
        id = create_inst(client, t, "Invalidate")
        # Prime
        r1 = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r1.get_json()["source"] == "pvgis_api"
        # Cache hit
        r2 = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r2.get_json()["source"] == "cache"
        # Change tilt → invalidate
        client.patch(f"/api/installations/{id}", headers=hdr(t), json={"tilt": 20.0})
        # Should hit API again
        r3 = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r3.get_json()["source"] == "pvgis_api"

    def test_cache_invalidated_on_coords_change(self, client):
        t  = register_login(client, "pv_inv_loc")
        id = create_inst(client, t, "Inval Loc")
        client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        # Move installation
        client.patch(f"/api/installations/{id}", headers=hdr(t),
                     json={"latitude": 33.0, "longitude": 9.0})
        r = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert r.get_json()["source"] == "pvgis_api"

    def test_monthly_12_values(self, client):
        t  = register_login(client, "pv_mo12")
        id = create_inst(client, t, "Monthly12")
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        monthly = r.get_json()["data"]["monthly_production_kwh"]
        assert isinstance(monthly, list) and len(monthly) == 12

    def test_pvgis_params_in_response(self, client):
        t  = register_login(client, "pv_par")
        id = create_inst(client, t, "Params")
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        p  = r.get_json().get("pvgis_params", {})
        assert p.get("raddatabase") == "PVGIS-SARAH2"

    def test_other_user_denied(self, client):
        t1 = register_login(client, "pv_own")
        t2 = register_login(client, "pv_oth")
        id = create_inst(client, t1, "Protected PV")
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t2))
        assert r.status_code == 404

    def test_unauthenticated_denied(self, client):
        client.delete_cookie("access_token")
        r = client.get("/api/installations/1/pvgis")
        assert r.status_code == 401

    def test_raw_response_not_in_api_output(self, client):
        t  = register_login(client, "pv_raw")
        id = create_inst(client, t, "No Raw")
        r  = client.get(f"/api/installations/{id}/pvgis", headers=hdr(t))
        assert "_raw_response" not in r.get_json()


# ─────────────────────────────────────────────────────────────────────────────
# Map endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestMapEndpoints:

    def test_user_map_geojson_structure(self, client):
        t  = register_login(client, "map_s")
        create_inst(client, t, "Map Struct")
        r  = client.get("/api/installations/map", headers=hdr(t))
        assert r.status_code == 200
        d = r.get_json()
        assert d["type"] == "FeatureCollection"
        if d["features"]:
            f = d["features"][0]
            assert f["type"] == "Feature"
            assert f["geometry"]["type"] == "Point"
            coords = f["geometry"]["coordinates"]
            assert len(coords) == 2   # [lon, lat]
            # longitude first (GeoJSON standard)
            assert -180 <= coords[0] <= 180
            assert -90  <= coords[1] <= 90

    def test_user_sees_only_own(self, client):
        ta = register_login(client, "map_ua")
        tb = register_login(client, "map_ub")
        create_inst(client, ta, "UA Site")
        create_inst(client, tb, "UB Site")
        names_a = {f["properties"]["name"]
                   for f in client.get("/api/installations/map", headers=hdr(ta))
                   .get_json()["features"]}
        names_b = {f["properties"]["name"]
                   for f in client.get("/api/installations/map", headers=hdr(tb))
                   .get_json()["features"]}
        assert "UA Site" in names_a and "UB Site" not in names_a
        assert "UB Site" in names_b and "UA Site" not in names_b

    def test_no_coords_excluded_from_map(self, client):
        t  = register_login(client, "map_nc2")
        id = create_inst(client, t, "No Coord Map", coords=False)
        ids = [f["properties"]["id"]
               for f in client.get("/api/installations/map", headers=hdr(t))
               .get_json()["features"]]
        assert id not in ids

    def test_map_unauthenticated(self, client):
        client.delete_cookie("access_token")
        assert client.get("/api/installations/map").status_code == 401


# ─────────────────────────────────────────────────────────────────────────────
# Admin endpoints
# ─────────────────────────────────────────────────────────────────────────────

class TestAdminEndpoints:

    @pytest.fixture(scope="class")
    def atk(self, client):
        """Admin token — use seeded admin or promote a fresh one."""
        r = client.post("/api/auth/login",
                        json={"username": "admin", "password": "changeme123"})
        if r.status_code == 200:
            return r.get_json()["access_token"]
        # Fall back: register a user and manually promote via DB
        from platform_db import PlatformDatabase
        from auth import hash_password
        db  = PlatformDatabase()
        uid = db.create_user("p2admin", "p2admin@t.com",
                             hash_password("password123"), "ADMIN")
        r2  = client.post("/api/auth/login",
                          json={"username": "p2admin", "password": "password123"})
        return r2.get_json()["access_token"]

    def test_solar_stats_user_denied(self, client):
        t = register_login(client, "stat_u")
        assert client.get("/api/admin/solar-statistics",
                          headers=hdr(t)).status_code == 403

    def test_solar_stats_structure(self, client, atk):
        r = client.get("/api/admin/solar-statistics", headers=hdr(atk))
        assert r.status_code == 200
        d = r.get_json()
        for key in ("total_installations", "total_capacity_kwp", "by_status",
                    "active_count", "total_organizations", "by_governorate"):
            assert key in d, f"missing key: {key}"

    def test_solar_stats_capacity_is_numeric(self, client, atk):
        d = client.get("/api/admin/solar-statistics", headers=hdr(atk)).get_json()
        assert isinstance(d["total_capacity_kwp"], (int, float))

    def test_admin_map_user_denied(self, client):
        t = register_login(client, "admap_u")
        assert client.get("/api/admin/installations/map",
                          headers=hdr(t)).status_code == 403

    def test_admin_map_geojson(self, client, atk):
        r = client.get("/api/admin/installations/map", headers=hdr(atk))
        assert r.status_code == 200
        assert r.get_json()["type"] == "FeatureCollection"

    def test_admin_map_sees_all_users(self, client, atk):
        ta = register_login(client, "am_ua")
        tb = register_login(client, "am_ub")
        create_inst(client, ta, "Admin Sees UA")
        create_inst(client, tb, "Admin Sees UB")
        names = {f["properties"]["name"]
                 for f in client.get("/api/admin/installations/map",
                                     headers=hdr(atk)).get_json()["features"]}
        assert "Admin Sees UA" in names
        assert "Admin Sees UB" in names

    def test_admin_map_filter_status(self, client, atk):
        r = client.get("/api/admin/installations/map?status=active", headers=hdr(atk))
        for f in r.get_json()["features"]:
            assert f["properties"]["status"] == "active"

    def test_admin_installations_filter_by_gov(self, client, atk):
        r = client.get("/api/admin/installations?governorate=Tunis", headers=hdr(atk))
        assert r.status_code == 200
        for inst in r.get_json():
            assert (inst.get("governorate") or "").lower() == "tunis"
