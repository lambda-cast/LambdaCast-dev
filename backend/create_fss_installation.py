#!/usr/bin/env python3
"""
create_fss_installation.py
==========================

Fetches real plant data from iSolarCloud via the FastAPI bridge
(local_testing/backend.py) and creates the "Faculty of Sciences Sfax"
installation in the Sunalyzer platform database with accurate data.

Usage:
    # 1. Start the FastAPI bridge first (in another terminal):
    #    cd local_testing && ../.venv/bin/python backend.py
    #    Then authorize in the browser when prompted.
    #
    # 2. Run this script:
    #    cd backend && ../.venv/bin/python create_fss_installation.py

The script will:
  - Connect to the bridge at http://127.0.0.1:8000
  - List all available iSolarCloud plants
  - Let you pick the Faculty of Sciences Sfax plant (by name or ID)
  - Fetch plant details + real-time data
  - Create/update the Sunalyzer installation record
"""

import sys
import os
import json
import requests
from datetime import datetime

# Make sure we can import Sunalyzer backend modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platform_db import PlatformDatabase
from auth import hash_password

# ============================================================
# CONFIG
# ============================================================

BRIDGE_URL = os.environ.get("ISOLARCLOUD_BRIDGE_URL", "http://127.0.0.1:8000")

# Known coordinates for Faculty of Sciences Sfax (Université de Sfax)
FSS_LATITUDE  = 34.74667
FSS_LONGITUDE = 10.76139
FSS_ADDRESS   = "Route de Soukra km 3.5"
FSS_CITY      = "Sfax"
FSS_GOVERNORATE = "Sfax"
FSS_COUNTRY   = "Tunisia"

# ============================================================
# BRIDGE HELPERS
# ============================================================

def get(path: str) -> dict:
    url = f"{BRIDGE_URL}{path}"
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        print(f"  ERROR: Could not reach bridge at {url}: {e}")
        sys.exit(1)


def val(data: dict, key: str, default=None):
    """Extract value from iSolarCloud measure-point entry."""
    entry = data.get(key)
    if entry is None:
        return default
    v = entry.get("value")
    return v if v is not None else default


def fval(data: dict, key: str, default: float = 0.0) -> float:
    v = val(data, key, default)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n" + "=" * 60)
    print("  Sunalyzer × iSolarCloud — Faculty of Sciences Sfax Setup")
    print("=" * 60)

    # ── 1. Check bridge is up and authenticated ─────────────────
    print("\n[1/5] Checking bridge status...")
    health = get("/api/health")
    if not health.get("authenticated"):
        print("\n  Bridge is running but NOT authenticated yet.")
        print("  Start local_testing/backend.py, complete OAuth in browser, then re-run.")
        sys.exit(1)
    print("  ✓ Bridge authenticated")

    # ── 2. Fetch plant list ─────────────────────────────────────
    print("\n[2/5] Fetching plants from iSolarCloud...")
    plants_resp = get("/api/plants")
    plants = plants_resp.get("plants", [])
    if not plants:
        print("  ERROR: No plants found. Check your iSolarCloud account.")
        sys.exit(1)

    print(f"  Found {len(plants)} plant(s):")
    for i, p in enumerate(plants):
        print(f"    [{i}] ID={p.get('ps_id')}  Name={p.get('ps_name')}  "
              f"Capacity={p.get('installed_capacity', '?')}  "
              f"Loc={p.get('city', '?')}, {p.get('country', '?')}")

    # ── 3. Select the target plant ──────────────────────────────
    print("\n[3/5] Selecting Faculty of Sciences Sfax plant...")
    selected = None

    # Try to auto-match by name
    for p in plants:
        name = (p.get("ps_name") or "").lower()
        if any(kw in name for kw in ["sfax", "science", "fss", "faculté", "faculte", "faculty"]):
            selected = p
            print(f"  ✓ Auto-matched: {p.get('ps_name')} (ID={p.get('ps_id')})")
            break

    if not selected:
        if len(plants) == 1:
            selected = plants[0]
            print(f"  Only one plant — using: {selected.get('ps_name')}")
        else:
            print("\n  Could not auto-match. Please enter the plant index from the list above:")
            try:
                idx = int(input("  Index: ").strip())
                selected = plants[idx]
            except (ValueError, IndexError):
                print("  Invalid selection. Exiting.")
                sys.exit(1)

    plant_id = str(selected["ps_id"])
    plant_name = selected.get("ps_name", "Faculty of Sciences Sfax")
    print(f"  → Using plant ID={plant_id}, Name={plant_name!r}")

    # ── 4. Fetch real-time data ─────────────────────────────────
    print(f"\n[4/5] Fetching real-time data for plant {plant_id}...")
    rt_resp = get(f"/api/realtime/{plant_id}")
    data = rt_resp.get("data", {})

    if not data:
        print("  WARNING: No real-time data received. "
              "The plant may be offline or bridge not yet fully loaded.")
        print("  Installation will be created with known metadata only.")

    # Extract what we can
    installed_cap_kwp = None
    # From plant metadata
    cap_raw = selected.get("installed_capacity") or selected.get("design_capacity")
    if cap_raw:
        try:
            installed_cap_kwp = float(cap_raw)
        except (TypeError, ValueError):
            pass

    # From realtime if metadata missing
    if installed_cap_kwp is None and data:
        # inverter_ac_power gives rated AC power in W
        ac_power_w = fval(data, "inverter_ac_power")
        if ac_power_w > 0:
            installed_cap_kwp = round(ac_power_w / 1000.0, 2)

    current_pv_w     = fval(data, "power")
    daily_yield_wh   = fval(data, "daily_yield")
    total_yield_wh   = fval(data, "total_yield")
    daily_feed_in_wh = fval(data, "feed_in_energy_today") or fval(data, "daily_feed_in_energy_pv")
    total_feed_in_wh = fval(data, "feed_in_energy_total") or fval(data, "feed_in_energy_total")
    module_temp_c    = fval(data, "plant_module_temperature") or val(data, "plant_module_temperature")
    ambient_temp_c   = fval(data, "plant_ambient_temperature")

    print(f"  Current PV power:  {current_pv_w:.1f} W")
    print(f"  Today's yield:     {daily_yield_wh/1000:.2f} kWh")
    print(f"  Total yield:       {total_yield_wh/1000:.1f} kWh")
    print(f"  Installed cap:     {installed_cap_kwp} kWp")
    print(f"  Today feed-in:     {daily_feed_in_wh/1000:.2f} kWh")

    # Also dump all available measure points for reference
    if data:
        print("\n  --- All available measure points ---")
        for k, v_entry in sorted(data.items()):
            if isinstance(v_entry, dict) and v_entry.get("value") is not None:
                print(f"    {k}: {v_entry['value']} {v_entry.get('unit', '')}")

    # ── 5. Create/update Sunalyzer installation ─────────────────
    print("\n[5/5] Creating installation in Sunalyzer platform DB...")
    db = PlatformDatabase()

    # Find the admin user (owner)
    users = db.list_users()
    admin = next((u for u in users if u["role"] == "ADMIN"), None)
    if not admin:
        print("  ERROR: No admin user found. Run the server at least once to seed the admin.")
        sys.exit(1)
    owner_id = admin["id"]
    print(f"  Owner: {admin['username']} (id={owner_id})")

    # Check if the installation already exists
    all_insts = db.list_installations()
    existing = next(
        (i for i in all_insts if "sfax" in (i.get("name") or "").lower()
         or "sciences" in (i.get("name") or "").lower()),
        None
    )

    installation_data = {
        "name":                   "Faculty of Sciences Sfax",
        "owner_user_id":          owner_id,
        "latitude":               FSS_LATITUDE,
        "longitude":              FSS_LONGITUDE,
        "address":                FSS_ADDRESS,
        "city":                   FSS_CITY,
        "governorate":            FSS_GOVERNORATE,
        "country":                FSS_COUNTRY,
        "status":                 "active",
        "panel_technology":       "monocrystalline",
        "notes":                  (
            f"iSolarCloud plant ID: {plant_id}. "
            f"Data synced at: {datetime.now().strftime('%Y-%m-%d %H:%M')}. "
            f"Total lifetime yield: {total_yield_wh/1000:.1f} kWh."
        ),
    }

    if installed_cap_kwp:
        installation_data["installed_capacity_kwp"] = installed_cap_kwp

    if existing:
        inst_id = existing["id"]
        db.update_installation(inst_id, installation_data)
        print(f"  ✓ Updated existing installation: ID={inst_id}")
    else:
        inst_id = db.create_installation(installation_data)
        print(f"  ✓ Created new installation: ID={inst_id}")

    print(f"\n{'=' * 60}")
    print(f"  Done! Installation ID: {inst_id}")
    print(f"  View at: http://localhost:8020/platform/installations/{inst_id}/monitor")
    print(f"\n  Next step — to stream live iSolarCloud data:")
    print(f"  1. In data/config.yml, set:")
    print(f"       device:")
    print(f"         type: iSolarCloud")
    print(f"         bridge_url: http://isolarcloud-bridge:8000")
    print(f"         plant_id: \"{plant_id}\"")
    print(f"  2. Restart the sunalyzer container: docker compose restart sunalyzer")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    main()
