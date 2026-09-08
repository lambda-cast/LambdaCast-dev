"""
forecast_interface.py
=====================
Data contract / interface layer for the AI forecasting team.

This module defines the **exact structure** that:
  1. The AI forecasting service will receive as input.
  2. The AI service should return as its forecast output.

The AI team can use this file as the specification for their integration.

Design goals
------------
- Zero AI/ML code in this file — only data structures and a retrieval helper.
- The AI service is a separate process/microservice; it communicates only
  via the REST API defined here.
- Historical telemetry comes from the existing Sunalyzer db.sqlite.
- PVGIS baseline comes from pvgis_service.py.
- Weather data integration point is defined (not yet implemented).

Forecast input package structure
---------------------------------
{
    "installation": { ...full installation metadata... },
    "location": {
        "latitude": float,
        "longitude": float,
        "city": str,
        "region": str,
        "country": str,
    },
    "panel_config": {
        "technology": str,
        "power_wp": float,
        "number_of_panels": int,
        "tilt": float,
        "azimuth": float,
    },
    "inverter_config": {
        "manufacturer": str,
        "model": str,
        "capacity_kw": float,
    },
    "capacity": {
        "installed_kwp": float,
    },
    "historical_production": [
        {"date": "YYYY-MM-DD", "produced_kwh": float, "consumed_kwh": float},
        ...
    ],
    "weather_data": null,          # placeholder — not yet integrated
    "pvgis_baseline": { ...stub... },
    "metadata": {
        "generated_at": "ISO8601",
        "data_version": "1.0",
    }
}

Forecast output contract (what the AI team should return)
----------------------------------------------------------
POST /api/installations/<id>/forecast
Body:
{
    "installation_id": int,
    "forecast_period": "day" | "week" | "month",
    "generated_at": "ISO8601",
    "model_version": str,
    "predictions": [
        {
            "date":       "YYYY-MM-DD",
            "forecast_kwh":     float,
            "lower_bound_kwh":  float,
            "upper_bound_kwh":  float,
            "confidence":       float   # 0.0 – 1.0
        },
        ...
    ]
}
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pvgis_service import PVGISService


# Path to the existing telemetry database (read-only access)
_TELEMETRY_DB_PATH = "data/db.sqlite"


class ForecastInterface:
    """
    Builds the standardised forecast input package for an installation.

    All data retrieval is read-only; this class never writes to any database.
    """

    def __init__(self, telemetry_db: str = _TELEMETRY_DB_PATH):
        self.telemetry_db = telemetry_db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_forecast_input(self, installation: dict) -> dict:
        """
        Assemble the full forecast input package.

        Args:
            installation: dict from PlatformDatabase.get_installation()

        Returns:
            Standardised forecast input dict (see module docstring).
        """
        return {
            "installation":          self._installation_info(installation),
            "location":              self._location_info(installation),
            "panel_config":          self._panel_config(installation),
            "inverter_config":       self._inverter_config(installation),
            "capacity":              self._capacity_info(installation),
            "historical_production": self._historical_production(),
            "weather_data":          None,   # future: attach weather service data here
            "pvgis_baseline":        self._pvgis_baseline(installation),
            "metadata": {
                "generated_at":  datetime.now(timezone.utc).isoformat(),
                "data_version":  "1.0",
                "contract_note": (
                    "This is the AI forecast input contract v1.0. "
                    "The AI service must POST its forecast to "
                    "/api/installations/{id}/forecast using the "
                    "structure defined in forecast_interface.py."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Private helpers — data extraction
    # ------------------------------------------------------------------

    def _installation_info(self, inst: dict) -> dict:
        """Return selected installation metadata (excludes internal DB fields)."""
        return {
            "id":                   inst.get("id"),
            "name":                 inst.get("name"),
            "installation_date":    inst.get("installation_date"),
            "status":               inst.get("status"),
        }

    def _location_info(self, inst: dict) -> dict:
        return {
            "latitude":  inst.get("latitude"),
            "longitude": inst.get("longitude"),
            "address":   inst.get("address"),
            "city":      inst.get("city"),
            "region":    inst.get("region"),
            "country":   inst.get("country", "Tunisia"),
        }

    def _panel_config(self, inst: dict) -> dict:
        return {
            "manufacturer":    inst.get("panel_manufacturer"),
            "model":           inst.get("panel_model"),
            "technology":      inst.get("panel_technology"),
            "power_wp":        inst.get("panel_power_wp"),
            "number_of_panels": inst.get("number_of_panels"),
            "tilt":            inst.get("tilt"),
            "azimuth":         inst.get("azimuth"),
        }

    def _inverter_config(self, inst: dict) -> dict:
        return {
            "manufacturer": inst.get("inverter_manufacturer"),
            "model":        inst.get("inverter_model"),
            "capacity_kw":  inst.get("inverter_capacity_kw"),
        }

    def _capacity_info(self, inst: dict) -> dict:
        return {
            "installed_kwp": inst.get("installed_capacity_kwp"),
        }

    def _historical_production(self) -> list[dict]:
        """
        Pull daily production history from the existing Sunalyzer telemetry DB.

        Returns a list of {date, produced_kwh, consumed_kwh} dicts.
        Returns an empty list if the telemetry DB does not exist yet.
        """
        if not Path(self.telemetry_db).exists():
            logging.warning(
                "ForecastInterface: telemetry DB not found — "
                "returning empty historical production"
            )
            return []

        try:
            conn = sqlite3.connect(self.telemetry_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT date, produced_b - produced_a AS produced_kwh, "
                "       consumed_b - consumed_a AS consumed_kwh "
                "FROM days ORDER BY date"
            ).fetchall()
            conn.close()
            return [
                {
                    "date":          row["date"],
                    "produced_kwh":  round(row["produced_kwh"], 3),
                    "consumed_kwh":  round(row["consumed_kwh"], 3),
                }
                for row in rows
            ]
        except Exception as exc:
            logging.error(f"ForecastInterface: failed to read telemetry: {exc}")
            return []

    def _pvgis_baseline(self, installation: dict) -> dict:
        """Return the PVGIS baseline (calls real service with mock in tests)."""
        svc = PVGISService()
        try:
            return svc.get_annual_and_monthly(installation=installation)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}
