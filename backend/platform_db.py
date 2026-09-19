"""
platform_db.py
==============
Manages the platform-level SQLite database (data/platform.db).

This database stores users, organizations, solar installations, and
cached PVGIS results.  It is completely separate from the existing
data/db.sqlite (telemetry) so the original Sunalyzer monitoring
functionality is not touched.

Schema
------
organizations   – companies / entities that own installations
users           – platform accounts (ADMIN or USER role)
installations   – solar PV installation records with full metadata
pvgis_cache     – cached PVGIS API responses per installation

PostGIS readiness note
----------------------
latitude/longitude are stored as REAL (float64).  The schema uses
standard column names so a future migration to PostGIS can add a
GEOMETRY column via:
    ALTER TABLE installations ADD COLUMN geom GEOMETRY(Point, 4326);
    UPDATE installations SET geom = ST_SetSRID(ST_MakePoint(longitude, latitude), 4326);
No PostGIS-specific types are used yet because SQLite does not support them.
"""

import os
import sqlite3
import logging
from pathlib import Path

# Allow tests (and production deployments) to override the DB path via env var
_DEFAULT_DB_PATH = "data/platform.db"


# ---------------------------------------------------------------------------
# Schema definition
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
-- -----------------------------------------------------------------------
-- organizations
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    address     TEXT,
    country     TEXT    DEFAULT 'Tunisia',
    created_at  TEXT    DEFAULT (datetime('now'))
);

-- -----------------------------------------------------------------------
-- users
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'USER',  -- 'ADMIN' | 'USER'
    organization_id INTEGER REFERENCES organizations(id) ON DELETE SET NULL,
    is_active       INTEGER NOT NULL DEFAULT 1,        -- 0 = disabled
    created_at      TEXT    DEFAULT (datetime('now')),
    last_login      TEXT
);

-- -----------------------------------------------------------------------
-- installations
-- Full location fields for Tunisia administrative divisions.
-- PostGIS-ready: add GEOMETRY column in a future migration without
-- changing any of these columns.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS installations (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identity
    name                    TEXT    NOT NULL,
    owner_user_id           INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id         INTEGER REFERENCES organizations(id) ON DELETE SET NULL,

    -- Location — GIS foundation (ready for Leaflet map + future PostGIS)
    latitude                REAL,       -- WGS-84 decimal degrees, -90..90
    longitude               REAL,       -- WGS-84 decimal degrees, -180..180
    address                 TEXT,       -- street / building
    city                    TEXT,
    governorate             TEXT,       -- Tunisia: wilaya (e.g. "Tunis", "Sfax")
    delegation              TEXT,       -- Tunisia: mu'tamadiyya sub-district
    region                  TEXT,       -- legacy / fallback region field
    country                 TEXT    DEFAULT 'Tunisia',

    -- PV System capacity
    installed_capacity_kwp  REAL,

    -- Panel configuration
    panel_manufacturer      TEXT,
    panel_model             TEXT,
    panel_technology        TEXT,    -- monocrystalline | polycrystalline | thin_film | bifacial
    panel_power_wp          REAL,    -- Wp per panel
    number_of_panels        INTEGER,

    -- Mounting / orientation
    tilt                    REAL,    -- degrees from horizontal (0=flat, 90=vertical)
    azimuth                 REAL,    -- degrees clockwise from north (180=south)

    -- Inverter configuration
    inverter_manufacturer   TEXT,
    inverter_model          TEXT,
    inverter_capacity_kw    REAL,

    -- Administration
    installation_date       TEXT,    -- ISO date string YYYY-MM-DD
    status                  TEXT    DEFAULT 'active',  -- active | inactive | maintenance
    notes                   TEXT,

    -- Device integration (grabber plugin config)
    device_type             TEXT,    -- plugin name: iSolarCloud | Fronius | Sunsynk | Dummy | null
    device_params           TEXT,    -- JSON object with plugin-specific params (bridge_url, plant_id, etc.)

    created_at              TEXT    DEFAULT (datetime('now')),
    updated_at              TEXT    DEFAULT (datetime('now'))
);

-- Trigger to auto-update updated_at on installation changes
CREATE TRIGGER IF NOT EXISTS installations_updated_at
    AFTER UPDATE ON installations
    FOR EACH ROW
BEGIN
    UPDATE installations SET updated_at = datetime('now') WHERE id = OLD.id;
END;

-- -----------------------------------------------------------------------
-- pvgis_cache
-- Stores the parsed PVGIS API response for each installation.
-- One row per installation (upserted on refresh).
-- The raw_response column preserves the complete PVGIS JSON for auditing.
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pvgis_cache (
    installation_id     INTEGER PRIMARY KEY
                        REFERENCES installations(id) ON DELETE CASCADE,

    -- Parsed summary values (fast access without JSON parsing)
    annual_production_kwh   REAL,           -- kWh/year
    specific_yield_kwh_kwp  REAL,           -- kWh/kWp/year
    performance_ratio       REAL,           -- 0..1
    irradiation_kwh_m2_year REAL,           -- kWh/m²/year (global tilted irradiance)

    -- Monthly breakdown stored as a JSON array [Jan..Dec] in kWh
    monthly_production_json TEXT,           -- e.g. [120.1, 135.2, ..., 110.0]

    -- PVGIS parameters used for this calculation (JSON object)
    pvgis_params_json       TEXT,

    -- Metadata
    pvgis_database          TEXT    DEFAULT 'PVGIS-SARAH2',
    calculated_at           TEXT    DEFAULT (datetime('now')),
    pvgis_api_version       TEXT    DEFAULT 'v5_2',

    -- Full raw response (for debugging / re-parsing without re-calling PVGIS)
    raw_response_json       TEXT
);
"""


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class PlatformDatabase:
    """
    Thin wrapper around an SQLite connection for the platform tables.

    Each method opens and closes its own connection to stay compatible with
    the multi-process (grabber + server) deployment model used by Sunalyzer.
    """

    def __init__(self, db_path: str | None = None):
        # Priority: explicit arg > env var > default
        self.db_path = db_path or os.environ.get("SUNALYZER_PLATFORM_DB", _DEFAULT_DB_PATH)
        self._ensure_schema()
        self._ensure_device_columns()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Returns a new connection with row_factory set to dict-like rows."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")   # safe for concurrent reads
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self):
        """Creates tables if they don't exist yet."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()
        logging.info(f"PlatformDatabase: schema ready at {self.db_path}")

    def _ensure_device_columns(self):
        """Add device_type / device_params if this is an older DB missing them."""
        with self._connect() as conn:
            info = [r[1] for r in conn.execute("PRAGMA table_info(installations)").fetchall()]
            if "device_type" not in info:
                conn.execute("ALTER TABLE installations ADD COLUMN device_type TEXT")
                logging.info("PlatformDatabase: added device_type column")
            if "device_params" not in info:
                conn.execute("ALTER TABLE installations ADD COLUMN device_params TEXT")
                logging.info("PlatformDatabase: added device_params column")
            conn.commit()

    # ------------------------------------------------------------------
    # Generic helpers
    # ------------------------------------------------------------------

    def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]

    def execute(self, sql: str, params: tuple = ()) -> int:
        """Execute a DML statement; returns lastrowid."""
        with self._connect() as conn:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        role: str = "USER",
        organization_id: int | None = None,
    ) -> int:
        sql = """
            INSERT INTO users (username, email, password_hash, role, organization_id)
            VALUES (?, ?, ?, ?, ?)
        """
        return self.execute(sql, (username, email, password_hash, role, organization_id))

    def get_user_by_id(self, user_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))

    def get_user_by_username(self, username: str) -> dict | None:
        return self.fetchone("SELECT * FROM users WHERE username = ?", (username,))

    def get_user_by_email(self, email: str) -> dict | None:
        return self.fetchone("SELECT * FROM users WHERE email = ?", (email,))

    def list_users(self) -> list[dict]:
        return self.fetchall(
            "SELECT id, username, email, role, organization_id, is_active, created_at, last_login "
            "FROM users ORDER BY id"
        )

    def update_user(self, user_id: int, fields: dict) -> None:
        """Update arbitrary user fields. 'id' is excluded for safety."""
        fields.pop("id", None)
        fields.pop("password_hash", None)  # use update_password for that
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [user_id]
        self.execute(f"UPDATE users SET {set_clause} WHERE id = ?", tuple(values))

    def update_password(self, user_id: int, password_hash: str) -> None:
        self.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (password_hash, user_id),
        )

    def update_last_login(self, user_id: int) -> None:
        self.execute(
            "UPDATE users SET last_login = datetime('now') WHERE id = ?",
            (user_id,),
        )

    def delete_user(self, user_id: int) -> None:
        self.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # ------------------------------------------------------------------
    # Organization operations
    # ------------------------------------------------------------------

    def create_organization(self, name: str, address: str = None, country: str = "Tunisia") -> int:
        return self.execute(
            "INSERT INTO organizations (name, address, country) VALUES (?, ?, ?)",
            (name, address, country),
        )

    def get_organization(self, org_id: int) -> dict | None:
        return self.fetchone("SELECT * FROM organizations WHERE id = ?", (org_id,))

    def list_organizations(self) -> list[dict]:
        return self.fetchall("SELECT * FROM organizations ORDER BY name")

    # ------------------------------------------------------------------
    # Installation operations
    # ------------------------------------------------------------------

    def create_installation(self, data: dict) -> int:
        """
        Inserts a new installation row.
        'data' is a dict of column-name → value pairs.
        'owner_user_id' is required; all other fields are optional.
        """
        allowed_columns = {
            "name", "owner_user_id", "organization_id",
            "latitude", "longitude", "address", "city",
            "governorate", "delegation", "region", "country",
            "installed_capacity_kwp",
            "panel_manufacturer", "panel_model", "panel_technology",
            "panel_power_wp", "number_of_panels",
            "tilt", "azimuth",
            "inverter_manufacturer", "inverter_model", "inverter_capacity_kw",
            "installation_date", "status", "notes",
            "device_type", "device_params",
        }
        filtered = {k: v for k, v in data.items() if k in allowed_columns}
        columns = ", ".join(filtered.keys())
        placeholders = ", ".join("?" for _ in filtered)
        sql = f"INSERT INTO installations ({columns}) VALUES ({placeholders})"
        return self.execute(sql, tuple(filtered.values()))

    def get_installation(self, installation_id: int) -> dict | None:
        return self.fetchone(
            "SELECT * FROM installations WHERE id = ?", (installation_id,)
        )

    def get_installation_for_user(
        self, installation_id: int, user_id: int
    ) -> dict | None:
        """Returns installation only if the user owns it."""
        return self.fetchone(
            "SELECT * FROM installations WHERE id = ? AND owner_user_id = ?",
            (installation_id, user_id),
        )

    def list_installations(self) -> list[dict]:
        """Admin view — all installations."""
        return self.fetchall(
            "SELECT i.*, u.username as owner_username "
            "FROM installations i "
            "JOIN users u ON i.owner_user_id = u.id "
            "ORDER BY i.id"
        )

    def list_installations_for_user(self, user_id: int) -> list[dict]:
        """User view — only their own installations."""
        return self.fetchall(
            "SELECT * FROM installations WHERE owner_user_id = ? ORDER BY id",
            (user_id,),
        )

    def update_installation(self, installation_id: int, data: dict) -> None:
        allowed_columns = {
            "name", "organization_id",
            "latitude", "longitude", "address", "city",
            "governorate", "delegation", "region", "country",
            "installed_capacity_kwp",
            "panel_manufacturer", "panel_model", "panel_technology",
            "panel_power_wp", "number_of_panels",
            "tilt", "azimuth",
            "inverter_manufacturer", "inverter_model", "inverter_capacity_kw",
            "installation_date", "status", "notes",
            "device_type", "device_params",
        }
        filtered = {k: v for k, v in data.items() if k in allowed_columns}
        if not filtered:
            return
        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [installation_id]
        self.execute(
            f"UPDATE installations SET {set_clause} WHERE id = ?", tuple(values)
        )

    def delete_installation(self, installation_id: int) -> None:
        self.execute("DELETE FROM installations WHERE id = ?", (installation_id,))

    # ------------------------------------------------------------------
    # PVGIS cache operations
    # ------------------------------------------------------------------

    def upsert_pvgis_cache(self, installation_id: int, data: dict) -> None:
        """
        Insert or replace the PVGIS cache row for an installation.
        'data' keys must match pvgis_cache column names.
        """
        allowed = {
            "annual_production_kwh", "specific_yield_kwh_kwp",
            "performance_ratio", "irradiation_kwh_m2_year",
            "monthly_production_json", "pvgis_params_json",
            "pvgis_database", "pvgis_api_version", "raw_response_json",
        }
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered["installation_id"] = installation_id
        filtered["calculated_at"] = "datetime('now')"

        # Build INSERT OR REPLACE
        cols = list(filtered.keys())
        # calculated_at uses a SQL function — handle separately
        placeholders = []
        values = []
        for c in cols:
            if c == "calculated_at":
                placeholders.append("datetime('now')")
            else:
                placeholders.append("?")
                values.append(filtered[c])

        # Re-add calculated_at without value placeholder
        cols_str = ", ".join(cols)
        ph_str = ", ".join(placeholders)
        sql = f"INSERT OR REPLACE INTO pvgis_cache ({cols_str}) VALUES ({ph_str})"

        with self._connect() as conn:
            conn.execute(sql, tuple(values))
            conn.commit()

    def get_pvgis_cache(self, installation_id: int) -> dict | None:
        return self.fetchone(
            "SELECT * FROM pvgis_cache WHERE installation_id = ?",
            (installation_id,),
        )

    def delete_pvgis_cache(self, installation_id: int) -> None:
        self.execute(
            "DELETE FROM pvgis_cache WHERE installation_id = ?", (installation_id,)
        )

    # ------------------------------------------------------------------
    # Map / GIS queries
    # ------------------------------------------------------------------

    def list_installations_with_coords(self, user_id: int | None = None) -> list[dict]:
        """
        Return installations that have coordinates set.
        If user_id is provided, filter to that user's installations only.
        Joins owner username for map popups.
        """
        base = (
            "SELECT i.id, i.name, i.latitude, i.longitude, i.city, "
            "i.governorate, i.status, i.installed_capacity_kwp, "
            "i.owner_user_id, u.username AS owner_username, "
            "i.organization_id "
            "FROM installations i "
            "JOIN users u ON i.owner_user_id = u.id "
            "WHERE i.latitude IS NOT NULL AND i.longitude IS NOT NULL"
        )
        if user_id is not None:
            return self.fetchall(base + " AND i.owner_user_id = ? ORDER BY i.id", (user_id,))
        return self.fetchall(base + " ORDER BY i.id")

    def get_solar_statistics(self) -> dict:
        """Admin-level fleet statistics."""
        rows = self.fetchall(
            "SELECT status, COUNT(*) as cnt, "
            "COALESCE(SUM(installed_capacity_kwp), 0) as total_kwp "
            "FROM installations GROUP BY status"
        )
        total_count = 0
        total_kwp = 0.0
        by_status: dict[str, int] = {}
        for r in rows:
            total_count += r["cnt"]
            total_kwp += r["total_kwp"]
            by_status[r["status"] or "unknown"] = r["cnt"]

        org_count = self.fetchone("SELECT COUNT(*) as c FROM organizations")
        user_count = self.fetchone(
            "SELECT COUNT(*) as c FROM users WHERE is_active = 1"
        )
        gov_rows = self.fetchall(
            "SELECT governorate, COUNT(*) as cnt, "
            "COALESCE(SUM(installed_capacity_kwp), 0) as total_kwp "
            "FROM installations WHERE governorate IS NOT NULL "
            "GROUP BY governorate ORDER BY cnt DESC"
        )

        return {
            "total_installations": total_count,
            "total_capacity_kwp": round(total_kwp, 3),
            "by_status": by_status,
            "active_count": by_status.get("active", 0),
            "maintenance_count": by_status.get("maintenance", 0),
            "inactive_count": by_status.get("inactive", 0),
            "total_organizations": org_count["c"] if org_count else 0,
            "total_active_users": user_count["c"] if user_count else 0,
            "by_governorate": [dict(r) for r in gov_rows],
        }
