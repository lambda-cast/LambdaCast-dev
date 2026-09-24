"""
iqair_service.py
================
IQAir API integration for real-time US AQI data.

Uses the IQAir AirVisual API v2:
  https://api.airvisual.com/v2/nearest_city?lat=&lon=&key=

The endpoint returns the nearest monitored city to the given coordinates
and includes current AQI US (based on PM2.5) and weather conditions.

Environment variable required
------------------------------
  IQAIR_API_KEY  — your IQAir AirVisual API key
                   Get a free key at https://www.iqair.com/dashboard/api

Returned value
--------------
  AQI_US (int)  — US AQI index (0–500+)
                  Falls back to a PM2.5-derived estimate if the API is
                  unavailable or the key is missing.

US AQI breakpoints (EPA standard, based on PM2.5 µg/m³ 24-h average)
----------------------------------------------------------------------
  PM2.5 (µg/m³)   AQI range
  0.0  – 12.0     0  – 50   (Good)
  12.1 – 35.4     51 – 100  (Moderate)
  35.5 – 55.4     101– 150  (Unhealthy for Sensitive Groups)
  55.5 – 150.4    151– 200  (Unhealthy)
  150.5– 250.4    201– 300  (Very Unhealthy)
  250.5– 350.4    301– 400  (Hazardous)
  350.5– 500.4    401– 500  (Hazardous)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Optional

# IQAir AirVisual nearest-city endpoint
_IQAIR_BASE = "https://api.airvisual.com/v2/nearest_city"
_REQUEST_TIMEOUT_S = 8

# PM2.5 → US AQI breakpoints (EPA)
# Each tuple: (pm25_low, pm25_high, aqi_low, aqi_high)
_PM25_BREAKPOINTS = [
    (0.0,   12.0,    0,   50),
    (12.1,  35.4,   51,  100),
    (35.5,  55.4,  101,  150),
    (55.5, 150.4,  151,  200),
    (150.5, 250.4, 201,  300),
    (250.5, 350.4, 301,  400),
    (350.5, 500.4, 401,  500),
]


def pm25_to_aqi_us(pm25: float) -> int:
    """
    Convert a PM2.5 concentration (µg/m³) to the US AQI scale using
    the EPA linear interpolation formula.
    """
    pm25 = max(0.0, pm25)
    for (c_low, c_high, i_low, i_high) in _PM25_BREAKPOINTS:
        if c_low <= pm25 <= c_high:
            aqi = (i_high - i_low) / (c_high - c_low) * (pm25 - c_low) + i_low
            return round(aqi)
    # Above 500.4 µg/m³ — cap at 500
    return 500


class IQAirService:
    """
    Fetches real-time US AQI for a geographic coordinate using the IQAir API.

    Usage
    -----
    >>> svc = IQAirService()
    >>> aqi = svc.get_aqi_us(lat=34.74, lon=10.76)   # returns int or None
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("IQAIR_API_KEY", "")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_aqi_us(self, lat: float, lon: float) -> Optional[int]:
        """
        Return the current US AQI for the nearest monitored city to (lat, lon).

        Returns None if the API key is missing, the request fails, or the
        response does not contain AQI data. Callers should fall back to a
        PM2.5-derived estimate in that case.
        """
        if not self.api_key or self.api_key == "your-iqair-api-key-here":
            logging.warning("IQAirService: IQAIR_API_KEY not set — AQI_US will use PM2.5 fallback")
            return None

        url = (
            f"{_IQAIR_BASE}"
            f"?lat={lat}&lon={lon}&key={self.api_key}"
        )

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "LambdaCast/1.0"})
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            logging.warning(f"IQAirService: request failed: {exc}")
            return None

        try:
            status = data.get("status")
            if status != "success":
                logging.warning(f"IQAirService: API returned status={status!r}")
                return None

            pollution = data["data"]["current"]["pollution"]
            aqi_us = int(pollution["aqius"])
            logging.debug(f"IQAirService: AQI_US={aqi_us} for lat={lat}, lon={lon}")
            return aqi_us

        except (KeyError, TypeError, ValueError) as exc:
            logging.warning(f"IQAirService: unexpected response structure: {exc}")
            return None

    def get_aqi_us_with_fallback(
        self, lat: float, lon: float, pm25_fallback: float = 15.0
    ) -> int:
        """
        Return the US AQI. Falls back to a PM2.5-derived estimate if the
        live API call returns None.

        Args:
            lat: latitude
            lon: longitude
            pm25_fallback: PM2.5 µg/m³ value to use when the API is unavailable.
                           Default 15.0 µg/m³ → AQI ~58 (Moderate, typical Tunisia).
        """
        aqi = self.get_aqi_us(lat, lon)
        if aqi is not None:
            return aqi

        estimated = pm25_to_aqi_us(pm25_fallback)
        logging.info(
            f"IQAirService: using PM2.5-derived AQI_US={estimated} "
            f"(pm25={pm25_fallback} µg/m³)"
        )
        return estimated
