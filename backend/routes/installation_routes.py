"""
routes/installation_routes.py
=============================
Solar installation CRUD + location + PVGIS + map endpoints.

Blueprint prefix: /api/installations

Access rules
------------
USER  – CRUD only their own installations
ADMIN – can access any installation (also via /api/admin/installations)

PVGIS caching
-------------
GET  /api/installations/<id>/pvgis          → return cached result (fetch if not cached)
POST /api/installations/<id>/pvgis/refresh  → force fresh PVGIS call, update cache

Location
--------
GET /api/installations/<id>/location        → structured location fields

Map
---
GET /api/installations/map                  → GeoJSON for the authenticated user's
                                              installations (with coordinates)
"""

import logging
from flask import Blueprint, request, jsonify, g
import json as _json

from auth import require_auth
from platform_db import PlatformDatabase
from pvgis_service import PVGISService, PVGISError, PVGISMissingLocation
from forecast_interface import ForecastInterface

installations_bp = Blueprint(
    "installations", __name__, url_prefix="/api/installations"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VALID_STATUSES     = {"active", "inactive", "maintenance"}
VALID_TECHNOLOGIES = {"monocrystalline", "polycrystalline", "thin_film", "bifacial"}

# Tunisia governorates (for validation/autocomplete)
TUNISIA_GOVERNORATES = {
    "Ariana", "Béja", "Ben Arous", "Bizerte", "Gabès", "Gafsa",
    "Jendouba", "Kairouan", "Kasserine", "Kébili", "Kef", "Mahdia",
    "Manouba", "Médenine", "Monastir", "Nabeul", "Sfax", "Sidi Bouzid",
    "Siliana", "Sousse", "Tataouine", "Tozeur", "Tunis", "Zaghouan",
}

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_installation(data: dict) -> str | None:
    """Returns an error string or None if data is valid."""
    if not (data.get("name") or "").strip():
        return "name is required"

    lat = data.get("latitude")
    lon = data.get("longitude")
    if lat is not None:
        try:
            lat = float(lat)
            if not (-90.0 <= lat <= 90.0):
                return "latitude must be between -90 and 90"
        except (TypeError, ValueError):
            return "latitude must be a number"
    if lon is not None:
        try:
            lon = float(lon)
            if not (-180.0 <= lon <= 180.0):
                return "longitude must be between -180 and 180"
        except (TypeError, ValueError):
            return "longitude must be a number"

    if data.get("status") and data["status"] not in VALID_STATUSES:
        return f"status must be one of: {', '.join(sorted(VALID_STATUSES))}"

    if data.get("panel_technology") and data["panel_technology"] not in VALID_TECHNOLOGIES:
        return f"panel_technology must be one of: {', '.join(sorted(VALID_TECHNOLOGIES))}"

    tilt = data.get("tilt")
    if tilt is not None:
        try:
            tilt = float(tilt)
            if not (0.0 <= tilt <= 90.0):
                return "tilt must be between 0 and 90 degrees"
        except (TypeError, ValueError):
            return "tilt must be a number"

    azimuth = data.get("azimuth")
    if azimuth is not None:
        try:
            azimuth = float(azimuth)
            if not (0.0 <= azimuth <= 360.0):
                return "azimuth must be between 0 and 360 degrees"
        except (TypeError, ValueError):
            return "azimuth must be a number"

    return None


def _coerce_numerics(data: dict) -> dict:
    """Convert string numeric fields to their correct Python types."""
    float_fields = {
        "latitude", "longitude", "installed_capacity_kwp",
        "panel_power_wp", "tilt", "azimuth", "inverter_capacity_kw",
    }
    int_fields = {"number_of_panels", "organization_id"}
    result = dict(data)
    for f in float_fields:
        if f in result and result[f] is not None:
            try:
                result[f] = float(result[f])
            except (TypeError, ValueError):
                pass
    for f in int_fields:
        if f in result and result[f] is not None:
            try:
                result[f] = int(result[f])
            except (TypeError, ValueError):
                pass
    return result


def _get_installation_or_404(installation_id: int, user: dict):
    """Fetch installation enforcing ownership. Returns (inst, db) or raises."""
    db = PlatformDatabase()
    if user["role"] == "ADMIN":
        inst = db.get_installation(installation_id)
    else:
        inst = db.get_installation_for_user(installation_id, user["id"])
    return inst, db


# ===========================================================================
# CRUD
# ===========================================================================

@installations_bp.route("", methods=["GET"])
@require_auth
def list_my_installations():
    """List the current user's own installations (USER only — admin redirects to /admin/installations)."""
    db   = PlatformDatabase()
    user = g.current_user
    # Admin can still call this API if needed (e.g. for Add Installation form)
    # but the UI directs them to /admin/installations
    return jsonify(db.list_installations_for_user(user["id"]))


@installations_bp.route("", methods=["POST"])
@require_auth
def create_installation():
    """Create a new installation owned by the current user."""
    data = _coerce_numerics(request.get_json(silent=True) or {})
    err = _validate_installation(data)
    if err:
        return jsonify({"error": err}), 400

    data["owner_user_id"] = g.current_user["id"]
    data["name"] = data["name"].strip()

    db = PlatformDatabase()
    installation_id = db.create_installation(data)
    logging.info(f"Installations: user {g.current_user['id']} created {installation_id}")
    return jsonify({"message": "Installation created", "id": installation_id}), 201


@installations_bp.route("/<int:installation_id>", methods=["GET"])
@require_auth
def get_installation(installation_id: int):
    """Get a single installation. Users can only access their own."""
    inst, _ = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404
    return jsonify(inst)


@installations_bp.route("/<int:installation_id>", methods=["PATCH"])
@require_auth
def update_installation(installation_id: int):
    """Update an installation. Users can only update their own."""
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    data = _coerce_numerics(request.get_json(silent=True) or {})
    err  = _validate_installation({**inst, **data})
    if err:
        return jsonify({"error": err}), 400

    db.update_installation(installation_id, data)

    # If location or panel config changed, invalidate the PVGIS cache
    pvgis_affecting = {
        "latitude", "longitude", "installed_capacity_kwp",
        "panel_technology", "tilt", "azimuth",
    }
    if pvgis_affecting & set(data.keys()):
        db.delete_pvgis_cache(installation_id)
        logging.info(
            f"Installations: PVGIS cache invalidated for {installation_id} "
            f"(changed fields: {pvgis_affecting & set(data.keys())})"
        )

    logging.info(f"Installations: user {g.current_user['id']} updated {installation_id}")
    return jsonify({"message": "Installation updated"})


@installations_bp.route("/<int:installation_id>", methods=["DELETE"])
@require_auth
def delete_installation(installation_id: int):
    """Delete an installation. Users can only delete their own."""
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    db.delete_installation(installation_id)
    logging.info(f"Installations: user {g.current_user['id']} deleted {installation_id}")
    return jsonify({"message": "Installation deleted"})


# ===========================================================================
# Location endpoint
# ===========================================================================

@installations_bp.route("/<int:installation_id>/location", methods=["GET"])
@require_auth
def get_location(installation_id: int):
    """
    GET /api/installations/<id>/location
    Return structured location fields only.
    """
    inst, _ = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    return jsonify({
        "installation_id": inst["id"],
        "name":         inst["name"],
        "latitude":     inst.get("latitude"),
        "longitude":    inst.get("longitude"),
        "address":      inst.get("address"),
        "city":         inst.get("city"),
        "governorate":  inst.get("governorate"),
        "delegation":   inst.get("delegation"),
        "region":       inst.get("region"),
        "country":      inst.get("country", "Tunisia"),
        "has_coordinates": (
            inst.get("latitude") is not None and
            inst.get("longitude") is not None
        ),
    })


# ===========================================================================
# Map endpoint (GeoJSON) — user view
# ===========================================================================

@installations_bp.route("/map", methods=["GET"])
@require_auth
def get_map_data():
    """
    GET /api/installations/map
    Returns GeoJSON FeatureCollection of the user's installations with coordinates.
    Admins see all; users see only their own.
    """
    db   = PlatformDatabase()
    user = g.current_user

    uid = None if user["role"] == "ADMIN" else user["id"]
    rows = db.list_installations_with_coords(user_id=uid)

    features = [_installation_to_geojson_feature(r) for r in rows]
    return jsonify({
        "type": "FeatureCollection",
        "features": features,
    })


def _installation_to_geojson_feature(inst: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [inst["longitude"], inst["latitude"]],  # GeoJSON: [lon, lat]
        },
        "properties": {
            "id":                     inst["id"],
            "name":                   inst["name"],
            "owner_username":         inst.get("owner_username"),
            "installed_capacity_kwp": inst.get("installed_capacity_kwp"),
            "status":                 inst.get("status", "active"),
            "city":                   inst.get("city"),
            "governorate":            inst.get("governorate"),
            "organization_id":        inst.get("organization_id"),
        },
    }


# ===========================================================================
# PVGIS endpoints
# ===========================================================================

@installations_bp.route("/<int:installation_id>/pvgis", methods=["GET"])
@require_auth
def get_pvgis(installation_id: int):
    """
    GET /api/installations/<id>/pvgis
    Returns cached PVGIS result. If no cache exists, fetches from PVGIS
    and stores the result automatically (lazy population).

    Query params:
        refresh=1   force a fresh PVGIS call (same as POST /pvgis/refresh)
    """
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    force_refresh = request.args.get("refresh", "0") in ("1", "true", "yes")

    if not force_refresh:
        cached = db.get_pvgis_cache(installation_id)
        if cached:
            svc    = PVGISService()
            result = svc.format_cached(cached, installation_id)
            return jsonify(result)

    # No cache or forced refresh — call PVGIS
    return _fetch_and_cache_pvgis(inst, db)


@installations_bp.route("/<int:installation_id>/pvgis/refresh", methods=["POST"])
@require_auth
def refresh_pvgis(installation_id: int):
    """
    POST /api/installations/<id>/pvgis/refresh
    Force a fresh PVGIS calculation and update the cache.
    Returns the new result.
    """
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    return _fetch_and_cache_pvgis(inst, db)


def _fetch_and_cache_pvgis(inst: dict, db: PlatformDatabase):
    """Call PVGIS, cache the result, return JSON response."""
    svc = PVGISService()
    try:
        result = svc.get_annual_and_monthly(inst)
    except PVGISMissingLocation as exc:
        return jsonify({
            "error":           str(exc),
            "status":          "missing_location",
            "installation_id": inst["id"],
        }), 422
    except PVGISError as exc:
        logging.error(f"PVGIS API error for installation {inst['id']}: {exc}")
        return jsonify({
            "error":           str(exc),
            "status":          "error",
            "installation_id": inst["id"],
        }), 502

    # Store in cache (strip the raw response from what we return to the client)
    cache_row = svc.build_cache_row(result)
    db.upsert_pvgis_cache(inst["id"], cache_row)

    # Remove internal raw response before returning
    result.pop("_raw_response", None)
    return jsonify(result)


# ===========================================================================
# Forecast interface
# ===========================================================================

@installations_bp.route("/<int:installation_id>/forecast-input", methods=["GET"])
@require_auth
def get_forecast_input(installation_id: int):
    """
    GET /api/installations/<id>/forecast-input
    Returns the standardised data package for the AI forecasting team.
    """
    inst, _ = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    fi      = ForecastInterface()
    payload = fi.build_forecast_input(installation=inst)
    return jsonify(payload)


# ===========================================================================
# Device configuration endpoints
# ===========================================================================

# Supported device types and their required/optional parameters
DEVICE_REGISTRY = {
    "iSolarCloud": {
        "label":       "iSolarCloud (Sungrow)",
        "params": {
            "bridge_url": {"label": "Bridge URL",  "required": True,  "default": "http://isolarcloud-bridge:8000"},
            "plant_id":   {"label": "Plant ID",    "required": True,  "default": ""},
            "fetch_interval_s": {"label": "Fetch interval (s)", "required": False, "default": 60},
        },
    },
    "Fronius": {
        "label": "Fronius Symo/GEN24",
        "params": {
            "host_name": {"label": "Inverter hostname/IP", "required": True,  "default": ""},
            "has_meter": {"label": "Smart Meter present",  "required": True,  "default": True},
        },
    },
    "Sunsynk": {
        "label": "Sunsynk / Deye hybrid",
        "params": {
            "connection":     {"label": "Connection type", "required": True,  "default": "solarman"},
            "host_name":      {"label": "Logger hostname/IP (solarman)", "required": False, "default": ""},
            "logger_serial":  {"label": "Logger serial (solarman)",      "required": False, "default": ""},
            "serial_port":    {"label": "Serial port (modbus_rtu)",       "required": False, "default": ""},
        },
    },
    "Dummy": {
        "label": "Dummy (demo/test)",
        "params": {},
    },
}


@installations_bp.route("/device-registry", methods=["GET"])
@require_auth
def get_device_registry():
    """
    GET /api/installations/device-registry
    Returns the list of supported device types and their parameter schemas.
    Used by the UI to dynamically render the device config form.
    """
    return jsonify(DEVICE_REGISTRY)


@installations_bp.route("/<int:installation_id>/device", methods=["GET"])
@require_auth
def get_device_config(installation_id: int):
    """GET current device config for an installation."""
    inst, _ = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404
    return jsonify({
        "installation_id": installation_id,
        "device_type":   inst.get("device_type"),
        "device_params": _json.loads(inst["device_params"]) if inst.get("device_params") else {},
    })


@installations_bp.route("/<int:installation_id>/device", methods=["POST"])
@require_auth
def set_device_config(installation_id: int):
    """
    POST /api/installations/<id>/device
    Save device type and params. Bootstraps the telemetry DB if needed.
    Body: { "device_type": "iSolarCloud", "device_params": { "plant_id": "...", ... } }
    """
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    body = request.get_json(silent=True) or {}
    device_type   = body.get("device_type", "").strip()
    device_params = body.get("device_params", {})

    if not device_type:
        return jsonify({"error": "device_type is required"}), 400
    if device_type not in DEVICE_REGISTRY:
        return jsonify({"error": f"Unknown device_type '{device_type}'. "
                                  f"Valid: {list(DEVICE_REGISTRY)}"}), 400
    if not isinstance(device_params, dict):
        return jsonify({"error": "device_params must be a JSON object"}), 400

    # Validate required params
    schema = DEVICE_REGISTRY[device_type]["params"]
    missing = [k for k, v in schema.items() if v["required"] and not device_params.get(k)]
    if missing:
        return jsonify({"error": f"Missing required params: {missing}"}), 400

    # Persist to platform.db
    db.update_installation(installation_id, {
        "device_type":   device_type,
        "device_params": _json.dumps(device_params),
    })

    # Bootstrap the telemetry DB if it doesn't exist yet
    import os
    from os.path import exists
    db_path = f"data/db_{installation_id}.sqlite"
    if not exists(db_path):
        _bootstrap_telemetry_db(db_path)
        logging.info(f"Device config: bootstrapped telemetry DB at {db_path}")

    logging.info(
        f"Device config: installation {installation_id} set to "
        f"{device_type} by user {g.current_user['id']}"
    )
    return jsonify({
        "message":         "Device configuration saved",
        "installation_id": installation_id,
        "device_type":     device_type,
        "telemetry_db":    db_path,
        "db_bootstrapped": True,
    }), 200


@installations_bp.route("/<int:installation_id>/device", methods=["DELETE"])
@require_auth
def delete_device_config(installation_id: int):
    """Remove device config from an installation (stops data collection on next grabber restart)."""
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404
    db.update_installation(installation_id, {"device_type": None, "device_params": None})
    return jsonify({"message": "Device configuration removed"})


def _bootstrap_telemetry_db(db_path: str):
    """Create a fresh telemetry SQLite DB with the standard Sunalyzer schema."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")

    tables = ["days", "months", "years", "all_time"]
    for name in tables:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {name} ("
            "date STRING PRIMARY KEY,"
            "produced_a REAL, produced_b REAL,"
            "consumed_a REAL, consumed_b REAL,"
            "fed_in_a REAL, fed_in_b REAL)"
        )
    conn.execute("INSERT OR IGNORE INTO all_time VALUES ('all_time',0,0,0,0,0,0)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS current "
        "(date STRING PRIMARY KEY, produced REAL, consumed_grid REAL, "
        "consumed_pv REAL, consumed_total REAL, fed_in REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS real_time "
        "(ID INTEGER PRIMARY KEY AUTOINCREMENT, "
        "time STRING, produced REAL, consumed REAL, fed_in REAL)"
    )
    for i in range(24 * 60):
        conn.execute(f"INSERT INTO real_time VALUES ('{i}','...','0.0','0.0','0.0')")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS highscores "
        "(type STRING PRIMARY KEY, date STRING, value REAL)"
    )
    conn.execute("INSERT OR IGNORE INTO highscores VALUES ('production','...',0.0)")

    conn.execute(
        "CREATE TABLE IF NOT EXISTS high_res "
        "(date STRING PRIMARY KEY, hrvalues STRING)"
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ML Forecast endpoints
# ---------------------------------------------------------------------------

@installations_bp.route("/forecast/models", methods=["GET"])
@installations_bp.route("/<int:installation_id>/forecast/models", methods=["GET"])
@require_auth
def get_forecast_models(installation_id: int = None):
    """
    GET /api/installations/forecast/models
    GET /api/installations/<id>/forecast/models
    Returns the list of available ML models found in models/ folder.
    """
    import forecast_service as fs
    models = fs.discover_models()
    return jsonify({"models": models})


@installations_bp.route("/<int:installation_id>/forecast", methods=["GET"])
@require_auth
def get_installation_forecast(installation_id: int):
    """
    GET /api/installations/<id>/forecast?model=xgb_PV1_Power_W_1&date=2026-09-20
    Calculates PV power forecast using the chosen model and real Open-Meteo weather features.
    """
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    model_name = request.args.get("model")
    target_date = request.args.get("date")

    lat = inst.get("latitude") or 34.73
    lon = inst.get("longitude") or 10.72

    import forecast_service as fs
    try:
        capacity_kwp = inst.get("installed_capacity_kwp")
        data = fs.get_forecast(model_name=model_name, lat=lat, lon=lon, target_date=target_date, capacity_kwp=capacity_kwp)
        return jsonify(data)
    except Exception as e:
        logging.exception("Forecast generation error")
        return jsonify({"error": str(e)}), 500


@installations_bp.route("/<int:installation_id>/aqi", methods=["GET"])
@require_auth
def get_installation_aqi(installation_id: int):
    """
    GET /api/installations/<id>/aqi
    Returns the current US AQI for the installation's location via IQAir.
    Response: { "aqi_us": int, "source": "iqair"|"fallback" }
    """
    inst, db = _get_installation_or_404(installation_id, g.current_user)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    lat = inst.get("latitude") or 34.73
    lon = inst.get("longitude") or 10.72

    from iqair_service import IQAirService, pm25_to_aqi_us
    svc = IQAirService()
    aqi = svc.get_aqi_us(lat, lon)
    if aqi is not None:
        return jsonify({"aqi_us": aqi, "source": "iqair"})
    # Fallback — return estimated value so the UI always gets a number
    estimated = pm25_to_aqi_us(15.0)
    return jsonify({"aqi_us": estimated, "source": "fallback"})
