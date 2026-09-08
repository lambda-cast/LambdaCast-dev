"""
pvgis_service.py
================
PVGIS (Photovoltaic Geographical Information System) integration.

Uses the official European Commission PVGIS REST API v5.2:
  https://re.jrc.ec.europa.eu/api/v5_2/

Endpoints used
--------------
PVcalc      – annual + monthly grid-connected PV energy output
              https://re.jrc.ec.europa.eu/api/v5_2/PVcalc
seriescalc  – hourly time-series (TMY basis) — optional, large payload
              https://re.jrc.ec.europa.eu/api/v5_2/seriescalc

PVGIS parameter mapping
-----------------------
Our field               → PVGIS parameter
installation.latitude   → lat
installation.longitude  → lon
installed_capacity_kwp  → peakpower   (kWp)
tilt                    → angle       (degrees from horizontal)
azimuth (our convention)→ aspect      (PVGIS: 0=south, -90=east, +90=west)
                          conversion: aspect = azimuth - 180
                          (our azimuth 180=south → aspect 0=south ✓)
panel_technology        → pvtechchoice  (crystSi / CIS / CdTe)
14 %                    → loss        (default system loss)
PVGIS-SARAH2            → raddatabase  (best for MENA / Tunisia)

Response structure (standardised, returned by all public methods)
-----------------------------------------------------------------
{
  "status":       "ok" | "error" | "missing_location",
  "source":       "pvgis_api" | "cache" | "error",
  "installation_id": int,
  "pvgis_params": {...},                   # params sent to PVGIS
  "calculated_at": "ISO-8601",
  "pvgis_api_version": "v5_2",
  "data": {
    "annual_production_kwh":   float,
    "specific_yield_kwh_kwp":  float,
    "performance_ratio":       float,
    "irradiation_kwh_m2_year": float,
    "monthly_production_kwh":  [float × 12],   # Jan … Dec
    "monthly_irradiation":     [float × 12],   # kWh/m²
  }
}
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PVGIS_API_BASE   = "https://re.jrc.ec.europa.eu/api/v5_2"
PVGIS_TIMEOUT_S  = 30       # seconds; PVGIS can be slow under load
PVGIS_API_VER    = "v5_2"

# Radiation database choices — PVGIS-SARAH2 has best coverage for Tunisia
PVGIS_DB_MENA    = "PVGIS-SARAH2"   # recommended for Tunisia
PVGIS_DB_ERA5    = "ERA5"           # fallback, global coverage

# Mapping: our panel_technology → PVGIS pvtechchoice
_TECH_MAP: dict[str | None, str] = {
    "monocrystalline": "crystSi",
    "polycrystalline": "crystSi",
    "thin_film":       "CIS",
    "bifacial":        "crystSi",
}
_TECH_DEFAULT = "crystSi"

# Month names for indexing
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------------------------------------------------------------------
# Public service class
# ---------------------------------------------------------------------------

class PVGISService:
    """
    Calls the PVGIS API and returns standardised result dicts.

    The caller (installation_routes.py) is responsible for caching;
    this class only fetches from PVGIS and parses the response.

    To test without hitting PVGIS, inject a mock via the `_http_get`
    parameter (used in the test suite).
    """

    def __init__(
        self,
        raddatabase: str = PVGIS_DB_MENA,
        loss_pct: float = 14.0,
        _http_get=None,          # injectable for tests: callable(url, params) → dict
    ):
        self.raddatabase = raddatabase
        self.loss_pct    = loss_pct
        self._http_get   = _http_get or self._real_http_get

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_annual_and_monthly(self, installation: dict) -> dict:
        """
        Fetch annual + monthly PV production from PVGIS PVcalc endpoint.

        Returns the standardised result dict.
        Raises PVGISError on API failure.
        Raises PVGISMissingLocation if lat/lon are not set.
        """
        self._check_location(installation)
        params = self._build_pvcalc_params(installation)

        logging.info(
            f"PVGISService: calling PVcalc for installation "
            f"{installation.get('id')} lat={params['lat']} lon={params['lon']}"
        )

        raw = self._http_get(f"{PVGIS_API_BASE}/PVcalc", params)
        return self._parse_pvcalc(raw, installation, params)

    def build_cache_row(self, result: dict) -> dict:
        """
        Convert a get_annual_and_monthly() result into a pvgis_cache row dict
        ready for PlatformDatabase.upsert_pvgis_cache().
        """
        d = result.get("data", {})
        return {
            "annual_production_kwh":   d.get("annual_production_kwh"),
            "specific_yield_kwh_kwp":  d.get("specific_yield_kwh_kwp"),
            "performance_ratio":       d.get("performance_ratio"),
            "irradiation_kwh_m2_year": d.get("irradiation_kwh_m2_year"),
            "monthly_production_json": json.dumps(d.get("monthly_production_kwh")),
            "pvgis_params_json":       json.dumps(result.get("pvgis_params")),
            "pvgis_database":          result.get("pvgis_params", {}).get("raddatabase", self.raddatabase),
            "pvgis_api_version":       PVGIS_API_VER,
            "raw_response_json":       json.dumps(result.get("_raw_response")),
        }

    def format_cached(self, cache_row: dict, installation_id: int) -> dict:
        """
        Format a pvgis_cache DB row into the standardised response dict.
        """
        monthly = None
        try:
            monthly = json.loads(cache_row["monthly_production_json"] or "null")
        except (json.JSONDecodeError, TypeError):
            pass

        pvgis_params = None
        try:
            pvgis_params = json.loads(cache_row["pvgis_params_json"] or "null")
        except (json.JSONDecodeError, TypeError):
            pass

        return {
            "status":            "ok",
            "source":            "cache",
            "installation_id":   installation_id,
            "pvgis_params":      pvgis_params,
            "calculated_at":     cache_row.get("calculated_at"),
            "pvgis_api_version": cache_row.get("pvgis_api_version", PVGIS_API_VER),
            "data": {
                "annual_production_kwh":   cache_row.get("annual_production_kwh"),
                "specific_yield_kwh_kwp":  cache_row.get("specific_yield_kwh_kwp"),
                "performance_ratio":       cache_row.get("performance_ratio"),
                "irradiation_kwh_m2_year": cache_row.get("irradiation_kwh_m2_year"),
                "monthly_production_kwh":  monthly,
                "monthly_irradiation":     None,   # not yet stored separately
            },
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _check_location(self, installation: dict) -> None:
        if installation.get("latitude") is None or installation.get("longitude") is None:
            raise PVGISMissingLocation(
                f"Installation {installation.get('id')} has no coordinates set. "
                "Set latitude and longitude before requesting PVGIS data."
            )

    def _build_pvcalc_params(self, installation: dict) -> dict:
        """Build the query parameters for the PVGIS PVcalc endpoint."""
        tech    = installation.get("panel_technology")
        pvtech  = _TECH_MAP.get(tech, _TECH_DEFAULT)
        azimuth = installation.get("azimuth")

        # PVGIS aspect convention: 0=south, -90=east, +90=west, ±180=north
        # Our azimuth: 180=south, 90=east, 270=west (clockwise from north)
        # Conversion: pvgis_aspect = azimuth - 180
        aspect = round(azimuth - 180, 2) if azimuth is not None else 0

        params: dict[str, Any] = {
            "lat":          round(float(installation["latitude"]),  6),
            "lon":          round(float(installation["longitude"]), 6),
            "peakpower":    installation.get("installed_capacity_kwp") or 1.0,
            "loss":         self.loss_pct,
            "angle":        installation.get("tilt") if installation.get("tilt") is not None else 35,
            "aspect":       aspect,
            "raddatabase":  self.raddatabase,
            "pvtechchoice": pvtech,
            "mountingplace":"free",     # free-standing (not BIPV)
            "outputformat": "json",
        }
        return params

    def _parse_pvcalc(
        self,
        raw: dict,
        installation: dict,
        params: dict,
    ) -> dict:
        """
        Parse a PVGIS PVcalc JSON response into our standardised format.

        PVGIS PVcalc response structure (v5.2):
        {
          "inputs": {...},
          "outputs": {
            "totals": {
              "fixed": {
                "E_y":  float,   # annual energy output kWh
                "H(i)_y": float, # annual irradiation kWh/m²
                "SD_y": float,   # standard deviation
                "l_aoi": float,  # angle-of-incidence losses %
                "l_spec": float, # spectral losses %
                "l_tg": float,   # temperature+irradiance losses %
                "l_total": float # total losses %
              }
            },
            "monthly": {
              "fixed": [
                {"month": 1, "E_d": float, "E_m": float,
                 "H(i)_d": float, "H(i)_m": float,
                 "SD_m": float},
                ...  × 12
              ]
            }
          },
          "meta": {...}
        }
        """
        try:
            outputs = raw["outputs"]
            totals  = outputs["totals"]["fixed"]
            monthly_data = outputs.get("monthly", {}).get("fixed", [])

            annual_kwh      = float(totals.get("E_y", 0))
            irrad_kwh_m2    = float(totals.get("H(i)_y", 0))
            total_losses    = float(totals.get("l_total", self.loss_pct))

            # Performance ratio: PR = E_y / (H(i)_y * peakpower)
            # or derive from losses: PR ≈ 1 - total_losses/100
            peakpower = params.get("peakpower") or 1.0
            if irrad_kwh_m2 > 0 and peakpower > 0:
                pr = annual_kwh / (irrad_kwh_m2 * peakpower)
            else:
                pr = None

            specific_yield = (annual_kwh / peakpower) if peakpower > 0 else None

            # Monthly: E_m = monthly energy (kWh), H(i)_m = monthly irradiation
            monthly_kwh  = []
            monthly_irr  = []
            for m in sorted(monthly_data, key=lambda x: x.get("month", 0)):
                monthly_kwh.append(round(float(m.get("E_m", 0)), 2))
                monthly_irr.append(round(float(m.get("H(i)_m", 0)), 2))

        except (KeyError, TypeError, ValueError) as exc:
            raise PVGISError(f"Failed to parse PVGIS response: {exc}") from exc

        return {
            "status":            "ok",
            "source":            "pvgis_api",
            "installation_id":   installation.get("id"),
            "pvgis_params":      params,
            "calculated_at":     datetime.now(timezone.utc).isoformat(),
            "pvgis_api_version": PVGIS_API_VER,
            "data": {
                "annual_production_kwh":   round(annual_kwh, 2),
                "specific_yield_kwh_kwp":  round(specific_yield, 2) if specific_yield else None,
                "performance_ratio":       round(pr, 4) if pr else None,
                "irradiation_kwh_m2_year": round(irrad_kwh_m2, 2),
                "monthly_production_kwh":  monthly_kwh if monthly_kwh else None,
                "monthly_irradiation":     monthly_irr if monthly_irr else None,
            },
            "_raw_response": raw,   # kept for build_cache_row(), stripped before API response
        }

    @staticmethod
    def _real_http_get(url: str, params: dict) -> dict:
        """Real HTTP GET to PVGIS. Raises PVGISError on network/API failure."""
        try:
            resp = requests.get(url, params=params, timeout=PVGIS_TIMEOUT_S)
        except requests.exceptions.Timeout:
            raise PVGISError(f"PVGIS request timed out after {PVGIS_TIMEOUT_S}s")
        except requests.exceptions.RequestException as exc:
            raise PVGISError(f"PVGIS network error: {exc}") from exc

        if resp.status_code == 400:
            # PVGIS returns error detail in the JSON body
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text[:200]
            raise PVGISError(f"PVGIS rejected the request (400): {detail}")

        if not resp.ok:
            raise PVGISError(
                f"PVGIS returned HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except Exception as exc:
            raise PVGISError(f"PVGIS response is not valid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------

class PVGISError(Exception):
    """Raised when the PVGIS API call fails for any reason."""


class PVGISMissingLocation(PVGISError):
    """Raised when an installation has no lat/lon set."""
