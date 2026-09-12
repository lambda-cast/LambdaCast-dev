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
        # Key measure points (all values come from pysolarcloud):
        #   power                  → current PV production (W)
        #   total_yield            → lifetime PV generation (Wh)
        #   daily_yield            → today's PV generation (Wh)
        #   feed_in_energy_total   → lifetime energy exported to grid (Wh)
        #   feed_in_energy_today   → today's energy exported (Wh)
        #   energy_purchased_today → today's energy drawn from grid (Wh)
        #   total_purchased_energy → lifetime energy drawn from grid (Wh)
        #   load_power             → current load (W)  [if available]
        #   meter_ac_power         → grid meter (W, + import / - export)
        # ----------------------------------------------------------

        # Current production (W → kW)
        current_pv_w = self._val(data, "power")
        self.current_power_produced_kw = current_pv_w / 1000.0

        # Current feed-in to grid — try direct meter first, else derive
        meter_ac_w = self._val(data, "meter_ac_power")   # + = import, - = export
        if meter_ac_w < 0:
            # Exporting: meter shows negative (exported to grid)
            self.current_power_fed_in_kw            = abs(meter_ac_w) / 1000.0
            self.current_power_consumed_from_grid_kw = 0.0
        elif meter_ac_w > 0:
            # Importing from grid
            self.current_power_consumed_from_grid_kw = meter_ac_w / 1000.0
            self.current_power_fed_in_kw            = 0.0
        else:
            self.current_power_fed_in_kw            = 0.0
            self.current_power_consumed_from_grid_kw = 0.0

        # Load power
        load_w = self._val(data, "load_power") or self._val(data, "total_load_active_power")
        total_load_kw = load_w / 1000.0

        # Self-consumed PV (production minus export)
        pv_self_kw = max(0.0, self.current_power_produced_kw - self.current_power_fed_in_kw)
        self.current_power_consumed_from_pv_kw  = pv_self_kw
        self.current_power_consumed_total_kw    = total_load_kw or (
            pv_self_kw + self.current_power_consumed_from_grid_kw
        )

        # Lifetime totals (Wh → kWh)
        total_yield_wh   = self._val(data, "total_yield")
        total_feed_in_wh = self._val(data, "feed_in_energy_total")
        total_bought_wh  = self._val(data, "total_purchased_energy")

        self.total_energy_produced_kwh = total_yield_wh   / 1000.0
        self.total_energy_fed_in_kwh   = total_feed_in_wh / 1000.0

        # Total consumed = self-used PV + grid-imported (all time)
        total_self_used_wh = max(0.0, total_yield_wh - total_feed_in_wh)
        self.total_energy_consumed_kwh = (total_self_used_wh + total_bought_wh) / 1000.0

        logging.debug(
            f"iSolarCloud: updated plant {plant_id}: "
            f"pv={self.current_power_produced_kw:.2f}kW, "
            f"fed={self.current_power_fed_in_kw:.2f}kW, "
            f"grid={self.current_power_consumed_from_grid_kw:.2f}kW, "
            f"total_yield={self.total_energy_produced_kwh:.1f}kWh"
        )
