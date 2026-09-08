"""
test_pvgis_service.py
=====================
Unit tests for PVGISService — all PVGIS HTTP calls are mocked.
No network access, no external service dependency.
"""

import json
import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from pvgis_service import PVGISService, PVGISError, PVGISMissingLocation


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_installation(
    lat=36.8190, lon=10.1660,
    capacity=10.0, tilt=30.0, azimuth=180.0,
    technology="monocrystalline",
    inst_id=1,
):
    return {
        "id":                     inst_id,
        "name":                   "Test Site",
        "latitude":               lat,
        "longitude":              lon,
        "installed_capacity_kwp": capacity,
        "tilt":                   tilt,
        "azimuth":                azimuth,
        "panel_technology":       technology,
    }


def _make_pvgis_response(annual_kwh=15000.0, monthly=None):
    """Build a realistic PVGIS PVcalc JSON response."""
    if monthly is None:
        monthly = [
            {"month": i, "E_d": 40.0, "E_m": round(annual_kwh / 12, 1),
             "H(i)_d": 4.5, "H(i)_m": 135.0, "SD_m": 50.0}
            for i in range(1, 13)
        ]
    return {
        "inputs": {
            "location": {"latitude": 36.8190, "longitude": 10.1660, "elevation": 10},
            "meteo_data": {"radiation_db": "PVGIS-SARAH2"},
            "mounting_system": {"fixed": {"slope": {"value": 30}, "azimuth": {"value": 0}}},
            "pv_module": {"technology": "crystSi", "peak_power": 10.0, "system_loss": 14.0},
        },
        "outputs": {
            "totals": {
                "fixed": {
                    "E_d":     round(annual_kwh / 365, 2),
                    "E_m":     round(annual_kwh / 12, 2),
                    "E_y":     annual_kwh,
                    "H(i)_d":  5.2,
                    "H(i)_m":  156.0,
                    "H(i)_y":  1872.0,
                    "SD_m":    120.0,
                    "SD_y":    210.0,
                    "l_aoi":   2.8,
                    "l_spec":  1.5,
                    "l_tg":    6.8,
                    "l_total": 14.0,
                }
            },
            "monthly": {"fixed": monthly},
        },
        "meta": {"inputs": {"location": {}}, "outputs": {}},
    }


def _mock_http(response_dict):
    """Return a callable that always returns response_dict."""
    def _get(url, params):
        return response_dict
    return _get


# ── Parameter building ───────────────────────────────────────────────────────

class TestParamBuilding:

    def test_south_facing_azimuth_to_aspect(self):
        """Azimuth 180 (south) → aspect 0 (PVGIS south)."""
        svc   = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst  = _make_installation(azimuth=180.0)
        svc.get_annual_and_monthly(inst)   # just need it to run
        # Verify via direct param build
        params = svc._build_pvcalc_params(inst)
        assert params["aspect"] == 0.0

    def test_east_facing_azimuth_to_aspect(self):
        """Azimuth 90 (east) → aspect -90 (PVGIS east)."""
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(azimuth=90.0)
        params = svc._build_pvcalc_params(inst)
        assert params["aspect"] == -90.0

    def test_west_facing_azimuth_to_aspect(self):
        """Azimuth 270 (west) → aspect 90 (PVGIS west)."""
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(azimuth=270.0)
        params = svc._build_pvcalc_params(inst)
        assert params["aspect"] == 90.0

    def test_monocrystalline_maps_to_crystSi(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(technology="monocrystalline")
        params = svc._build_pvcalc_params(inst)
        assert params["pvtechchoice"] == "crystSi"

    def test_thin_film_maps_to_CIS(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(technology="thin_film")
        params = svc._build_pvcalc_params(inst)
        assert params["pvtechchoice"] == "CIS"

    def test_unknown_tech_defaults_to_crystSi(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(technology="quantum_dot")  # unknown
        params = svc._build_pvcalc_params(inst)
        assert params["pvtechchoice"] == "crystSi"

    def test_coordinates_included(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(lat=36.8190, lon=10.1660)
        params = svc._build_pvcalc_params(inst)
        assert abs(params["lat"] - 36.8190) < 0.0001
        assert abs(params["lon"] - 10.1660) < 0.0001

    def test_raddatabase_default(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation()
        params = svc._build_pvcalc_params(inst)
        assert params["raddatabase"] == "PVGIS-SARAH2"

    def test_tilt_used_as_angle(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(tilt=25.0)
        params = svc._build_pvcalc_params(inst)
        assert params["angle"] == 25.0


# ── Response parsing ─────────────────────────────────────────────────────────

class TestResponseParsing:

    def test_annual_production_parsed(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response(15000.0)))
        inst   = _make_installation()
        result = svc.get_annual_and_monthly(inst)
        assert result["status"] == "ok"
        assert result["data"]["annual_production_kwh"] == pytest.approx(15000.0, rel=1e-3)

    def test_specific_yield_calculated(self):
        """specific_yield = annual_kwh / peakpower_kwp."""
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response(17000.0)))
        inst   = _make_installation(capacity=10.0)
        result = svc.get_annual_and_monthly(inst)
        assert result["data"]["specific_yield_kwh_kwp"] == pytest.approx(1700.0, rel=1e-2)

    def test_monthly_list_has_12_entries(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        result = svc.get_annual_and_monthly(_make_installation())
        assert isinstance(result["data"]["monthly_production_kwh"], list)
        assert len(result["data"]["monthly_production_kwh"]) == 12

    def test_irradiation_extracted(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        result = svc.get_annual_and_monthly(_make_installation())
        assert result["data"]["irradiation_kwh_m2_year"] == pytest.approx(1872.0, rel=1e-3)

    def test_performance_ratio_in_range(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response(15000.0)))
        result = svc.get_annual_and_monthly(_make_installation())
        pr = result["data"]["performance_ratio"]
        assert pr is not None
        assert 0.0 < pr <= 1.0

    def test_source_is_pvgis_api(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        result = svc.get_annual_and_monthly(_make_installation())
        assert result["source"] == "pvgis_api"

    def test_pvgis_params_returned(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        result = svc.get_annual_and_monthly(_make_installation())
        assert "pvgis_params" in result
        assert result["pvgis_params"]["lat"] is not None

    def test_bad_response_raises_pvgis_error(self):
        svc = PVGISService(_http_get=_mock_http({"garbage": True}))
        with pytest.raises(PVGISError):
            svc.get_annual_and_monthly(_make_installation())


# ── Missing location ─────────────────────────────────────────────────────────

class TestMissingLocation:

    def test_no_lat_raises(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(lat=None, lon=10.0)
        with pytest.raises(PVGISMissingLocation):
            svc.get_annual_and_monthly(inst)

    def test_no_lon_raises(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = _make_installation(lat=36.0, lon=None)
        with pytest.raises(PVGISMissingLocation):
            svc.get_annual_and_monthly(inst)

    def test_both_missing_raises(self):
        svc  = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        inst = {"id": 99, "name": "No location"}
        with pytest.raises(PVGISMissingLocation):
            svc.get_annual_and_monthly(inst)


# ── HTTP error handling ───────────────────────────────────────────────────────

class TestHTTPErrors:

    def test_pvgis_error_propagates(self):
        def failing_get(url, params):
            raise PVGISError("service unavailable")

        svc = PVGISService(_http_get=failing_get)
        with pytest.raises(PVGISError, match="service unavailable"):
            svc.get_annual_and_monthly(_make_installation())

    def test_any_exception_from_injected_fn_propagates(self):
        def failing_get(url, params):
            raise PVGISError("network error: connection refused")

        svc = PVGISService(_http_get=failing_get)
        with pytest.raises(PVGISError):
            svc.get_annual_and_monthly(_make_installation())

    def test_real_http_get_wraps_timeout(self):
        """Unit test the real HTTP wrapper directly (no actual network call)."""
        import unittest.mock as mock
        import requests

        with mock.patch("pvgis_service.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.Timeout("timeout")
            with pytest.raises(PVGISError, match="timed out"):
                PVGISService._real_http_get("http://example.com", {})

    def test_real_http_get_wraps_connection_error(self):
        import unittest.mock as mock
        import requests

        with mock.patch("pvgis_service.requests.get") as mock_get:
            mock_get.side_effect = requests.exceptions.ConnectionError("refused")
            with pytest.raises(PVGISError, match="network error"):
                PVGISService._real_http_get("http://example.com", {})


# ── Cache row building ────────────────────────────────────────────────────────

class TestCacheRowBuilding:

    def test_build_cache_row_has_required_keys(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response(16000.0)))
        result = svc.get_annual_and_monthly(_make_installation())
        row    = svc.build_cache_row(result)
        assert "annual_production_kwh"   in row
        assert "specific_yield_kwh_kwp"  in row
        assert "performance_ratio"       in row
        assert "irradiation_kwh_m2_year" in row
        assert "monthly_production_json" in row
        assert "pvgis_params_json"       in row

    def test_monthly_json_is_valid_json(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response()))
        result = svc.get_annual_and_monthly(_make_installation())
        row    = svc.build_cache_row(result)
        parsed = json.loads(row["monthly_production_json"])
        assert len(parsed) == 12

    def test_annual_in_cache_row_matches_result(self):
        svc    = PVGISService(_http_get=_mock_http(_make_pvgis_response(18000.0)))
        result = svc.get_annual_and_monthly(_make_installation())
        row    = svc.build_cache_row(result)
        assert row["annual_production_kwh"] == pytest.approx(18000.0, rel=1e-3)


# ── format_cached ─────────────────────────────────────────────────────────────

class TestFormatCached:

    def _make_cache_row(self, annual=15000.0):
        monthly = [round(annual / 12, 1)] * 12
        return {
            "installation_id":       1,
            "annual_production_kwh": annual,
            "specific_yield_kwh_kwp": annual / 10.0,
            "performance_ratio":     0.82,
            "irradiation_kwh_m2_year": 1900.0,
            "monthly_production_json": json.dumps(monthly),
            "pvgis_params_json":     json.dumps({"lat": 36.8, "lon": 10.1}),
            "pvgis_database":        "PVGIS-SARAH2",
            "calculated_at":         "2026-01-15T10:00:00+00:00",
            "pvgis_api_version":     "v5_2",
            "raw_response_json":     None,
        }

    def test_format_cached_status_ok(self):
        svc    = PVGISService()
        result = svc.format_cached(self._make_cache_row(), installation_id=1)
        assert result["status"] == "ok"
        assert result["source"] == "cache"

    def test_format_cached_monthly_decoded(self):
        svc    = PVGISService()
        result = svc.format_cached(self._make_cache_row(12000.0), installation_id=1)
        monthly = result["data"]["monthly_production_kwh"]
        assert isinstance(monthly, list)
        assert len(monthly) == 12

    def test_format_cached_annual_preserved(self):
        svc    = PVGISService()
        result = svc.format_cached(self._make_cache_row(21000.0), installation_id=1)
        assert result["data"]["annual_production_kwh"] == pytest.approx(21000.0)
