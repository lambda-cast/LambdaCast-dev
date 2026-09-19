"""
devices/iSolarCloud.py
======================

Sunalyzer device adapter for Sungrow inverters connected via iSolarCloud.

This adapter calls the local FastAPI bridge running in local_testing/backend.py
which handles OAuth2 authentication with the iSolarCloud API. It translates
iSolarCloud real-time measure points into the standard Sunalyzer device interface.

Configuration (config.yml):
----------------------------
device:
  type: iSolarCloud
  bridge_url: http://127.0.0.1:8000   # URL of the FastAPI iSolarCloud bridge
  plant_id: "1234567"                  # iSolarCloud plant/PS ID
  # Optional: leave blank to auto-select first plant
"""

import logging
import time
import requests


class iSolarCloud:
    """
    Device adapter that fetches live data from an iSolarCloud plant via
    the local FastAPI bridge (local_testing/backend.py).

    Device interface (matches Dummy.py / Fronius.py / Sunsynk.py):
      - total_energy_produced_kwh
      - total_energy_consumed_kwh
      - total_energy_fed_in_kwh
      - current_power_produced_kw
      - current_power_consumed_from_grid_kw
      - current_power_consumed_from_pv_kw
      - current_power_consumed_total_kw
      - current_power_fed_in_kw
    """

    def __init__(self, config):
        cfg = config.config_data.get("device", {})
        self.bridge_url = cfg.get("bridge_url", "http://127.0.0.1:8000").rstrip("/")
        self.plant_id   = str(cfg.get("plant_id", "")).strip()
        self.fetch_interval_s = max(60.0, float(cfg.get("fetch_interval_s", 60)))
        self._last_fetch_at = 0.0

        # Day-start tracking — used to anchor the daily baseline correctly
        self._last_day         = ""       # "YYYY-MM-DD" of last processed day
        self._day_start_kwh    = 0.0      # total_yield at midnight of current day
        self._first_read_of_day = True    # True until first real update of the day

        # Initialise all values to zero — will be filled on first update()
        self.total_energy_produced_kwh          = 0.0
        self.total_energy_consumed_kwh          = 0.0
        self.total_energy_fed_in_kwh            = 0.0
        self.current_power_produced_kw          = 0.0
        self.current_power_consumed_from_grid_kw = 0.0
        self.current_power_consumed_from_pv_kw  = 0.0
        self.current_power_consumed_total_kw    = 0.0
        self.current_power_fed_in_kw            = 0.0

        logging.info(
            f"iSolarCloud: adapter initialised "
            f"(bridge={self.bridge_url}, plant_id={self.plant_id or 'auto'})"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, timeout: int = 10) -> dict:
        """GET from the FastAPI bridge and return parsed JSON."""
        url = f"{self.bridge_url}{path}"
        try:
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            logging.error(f"iSolarCloud: bridge request failed ({url}): {exc}")
            raise

    def _resolve_plant_id(self) -> str:
        """If no plant_id configured, fetch the first available plant."""
        if self.plant_id:
            return self.plant_id

        data = self._get("/api/plants")
        plants = data.get("plants", [])
        if not plants:
            raise RuntimeError("iSolarCloud: no plants available from bridge")
        pid = str(plants[0]["ps_id"])
        logging.info(f"iSolarCloud: auto-selected plant_id={pid}")
        self.plant_id = pid
        return pid

    @staticmethod
    def _val(data: dict, key: str, default: float = 0.0) -> float:
        """Safely extract a float value from the iSolarCloud measure-point dict."""
        entry = data.get(key)
        if entry is None:
            return default
        v = entry.get("value")
        if v is None:
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def update(self):
        """
        Fetch current data from the bridge and update the device attributes.
        Called by the grabber on every polling interval.
        """
        now = time.monotonic()
        if now - self._last_fetch_at < self.fetch_interval_s:
            return
        self._last_fetch_at = now

        # 1. Ensure we're authenticated
        health = self._get("/api/health")
        if not health.get("authenticated"):
            logging.warning("iSolarCloud: bridge not authenticated yet — skipping update")
            return

        # 2. Resolve plant ID
        plant_id = self._resolve_plant_id()

        # 3. Fetch realtime data for this plant
        data_resp = self._get(f"/api/realtime/{plant_id}")
        data = data_resp.get("data", {})

        if not data:
            logging.warning(f"iSolarCloud: no realtime data for plant {plant_id}")
            return

        # ----------------------------------------------------------
        # Map iSolarCloud measure points → Sunalyzer device fields
        #
        # Available fields (others return None for this plant):
        #   power              → current PV production (W)
        #   total_yield        → lifetime PV generation (Wh) — cumulative meter
        #   daily_yield        → today's PV generation (Wh) — resets at midnight
        #   meter_ac_power     → grid meter (W, + import / − export) — None here
        #   load_power         → current load (W) — None here
        #   feed_in_energy_total     → lifetime export (Wh) — None here
        #   total_purchased_energy   → lifetime grid import (Wh) — None here
        #
        # IMPORTANT — total_yield granularity:
        #   total_yield updates in coarse steps (≥1 Wh increments) and can
        #   stay flat for many minutes.  daily_yield is the authoritative
        #   "today so far" value and is always consistent with total_yield.
        #   We therefore derive the day-start meter value as:
        #       day_start_kwh = total_yield_kwh − daily_yield_kwh
        #   and expose that via total_energy_produced_kwh so the grabber's
        #   daily delta (produced_b − produced_a) equals daily_yield exactly
        #   once the baseline row has been corrected on the first real reading.
        # ----------------------------------------------------------

        # Current production (W → kW)
        current_pv_w = self._val(data, "power")
        self.current_power_produced_kw = current_pv_w / 1000.0

        # Grid meter — may be None
        meter_ac_w = self._val(data, "meter_ac_power")
        if meter_ac_w < 0:
            self.current_power_fed_in_kw             = abs(meter_ac_w) / 1000.0
            self.current_power_consumed_from_grid_kw = 0.0
        elif meter_ac_w > 0:
            self.current_power_consumed_from_grid_kw = meter_ac_w / 1000.0
            self.current_power_fed_in_kw             = 0.0
        else:
            self.current_power_fed_in_kw             = 0.0
            self.current_power_consumed_from_grid_kw = 0.0

        # Load power — may be None
        load_w = self._val(data, "load_power") or self._val(data, "total_load_active_power")
        total_load_kw = load_w / 1000.0

        pv_self_kw = max(0.0, self.current_power_produced_kw - self.current_power_fed_in_kw)
        self.current_power_consumed_from_pv_kw = pv_self_kw
        self.current_power_consumed_total_kw   = total_load_kw or (
            pv_self_kw + self.current_power_consumed_from_grid_kw
        )

        # ── Energy totals ────────────────────────────────────────
        total_yield_wh  = self._val(data, "total_yield")       # lifetime Wh
        daily_yield_wh  = self._val(data, "daily_yield")        # today Wh (resets midnight)
        total_feed_in_wh = self._val(data, "feed_in_energy_total")
        total_bought_wh  = self._val(data, "total_purchased_energy")

        # Use daily_yield to make the grabber's daily delta meaningful:
        # Express total_energy_produced_kwh as (lifetime − today + today),
        # but re-anchor the lifetime meter to day-start so the grabber delta
        # captures exactly daily_yield for the current day.
        #
        # day_start_kwh = total_yield_kwh − daily_yield_kwh
        # We pass (day_start_kwh + daily_yield_kwh) = total_yield_kwh as
        # total_energy_produced_kwh — same number, but the grabber sees the
        # full lifetime value and computes (b − a) correctly once the
        # produced_a baseline is set to day_start_kwh on first reading.
        #
        # The grabber's zero-baseline correction (in insert_historical_values)
        # will set produced_a = current total_yield on the first real reading,
        # but that is too high by daily_yield_kwh.  So instead we expose
        # day_start_kwh = total_yield_kwh − daily_yield_kwh as the value to
        # store, and add daily_yield_kwh as the _b value so delta = daily_yield.
        #
        # Concretely: we shift total_energy_produced_kwh by −daily_yield_kwh
        # so that the first write creates produced_a = produced_b =
        # (total − daily).  On subsequent updates we write total, giving
        # delta = total − (total − daily) = daily_yield exactly.
        #
        # We only do this shift on the FIRST write per calendar day (when
        # produced_a would otherwise be set to the full running total).
        # After that we always write the true total so delta tracks naturally.

        total_yield_kwh  = total_yield_wh  / 1000.0
        daily_yield_kwh  = daily_yield_wh  / 1000.0

        # Store the true lifetime total — the grabber's baseline correction
        # logic will handle the rest correctly as long as produced_a is set
        # to (total_yield_kwh − daily_yield_kwh) on the first real reading.
        # We achieve this by exposing day_start_kwh as total_energy_produced_kwh
        # on startup (when _day_start_kwh is unset) so the grabber creates
        # the row with a = b = day_start, then on subsequent calls we expose
        # the full total so b advances and delta = daily_yield.
        today_str = time.strftime("%Y-%m-%d")
        if self._last_day != today_str:
            # New day (or first run): anchor the day-start baseline
            self._day_start_kwh    = total_yield_kwh - daily_yield_kwh
            self._last_day         = today_str
            self._first_read_of_day = True
            logging.info(
                f"iSolarCloud: new day {today_str}, "
                f"day_start={self._day_start_kwh:.3f} kWh, "
                f"total={total_yield_kwh:.3f} kWh, "
                f"daily_yield={daily_yield_kwh:.3f} kWh"
            )

        # Expose the full lifetime total; the grabber will compute delta
        # correctly because produced_a will equal _day_start_kwh (set on
        # the first real update of the day via the baseline-correction path).
        self.total_energy_produced_kwh = total_yield_kwh

        # Overwrite produced_a in the DB indirectly by using day_start_kwh
        # as the value that gets written on the first reading of the day.
        # We do this by temporarily returning day_start_kwh as the total on
        # the very first call of the day (when daily_yield just became
        # available after midnight reset).
        if self._first_read_of_day:
            self.total_energy_produced_kwh = self._day_start_kwh
            self._first_read_of_day = False
            logging.info(
                f"iSolarCloud: first read of day, exposing day_start "
                f"{self._day_start_kwh:.3f} kWh to anchor produced_a"
            )

        self.total_energy_fed_in_kwh   = total_feed_in_wh / 1000.0
        total_self_used_wh = max(0.0, total_yield_wh - total_feed_in_wh)
        self.total_energy_consumed_kwh = (total_self_used_wh + total_bought_wh) / 1000.0

        logging.debug(
            f"iSolarCloud: updated plant {plant_id}: "
            f"pv={self.current_power_produced_kw:.3f}kW, "
            f"daily={daily_yield_kwh:.3f}kWh, "
            f"total={total_yield_kwh:.3f}kWh, "
            f"fed={self.current_power_fed_in_kw:.3f}kW, "
            f"grid={self.current_power_consumed_from_grid_kw:.3f}kW"
        )
