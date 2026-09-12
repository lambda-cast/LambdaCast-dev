import asyncio
import json
import os
import webbrowser
from datetime import datetime
from pathlib import Path

from aiohttp import web
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

from pysolarcloud import Auth, Server
from pysolarcloud.plants import Plants


# ============================================================
# iSolarCloud APPLICATION CREDENTIALS
# ============================================================

APP_KEY    = "103B38551A4024599979710A849AB676"
SECRET_KEY = "pdxu4xe4x2jkkf26q123039nva2x91gj"
APP_ID     = "3914"

# In Docker: override via environment variable
# BRIDGE_REDIRECT_URI=http://<your-host>:8001/callback
REDIRECT_URI = os.environ.get(
    "BRIDGE_REDIRECT_URI",
    "http://localhost:8001/callback"
)

SERVER = Server.International

# Token persistence: stored in /data (Docker volume) or local dir
TOKEN_FILE = Path(os.environ.get("TOKEN_FILE", "/data/isolarcloud_token.json"))


# ============================================================
# GLOBAL STATE
# ============================================================

auth               = None
plants_api         = None
authorization_code = None
authenticated      = False
plant_cache        = []
realtime_cache     = {}


# ============================================================
# FASTAPI APP
# ============================================================

app = FastAPI(title="Sunalyzer iSolarCloud Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# TOKEN PERSISTENCE
# ============================================================

def save_token(token_data: dict):
    """Save OAuth token to disk so it survives container restarts."""
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
        print(f"Token saved to {TOKEN_FILE}")
    except Exception as e:
        print(f"Warning: could not save token: {e}")


def load_token() -> dict | None:
    """Load persisted OAuth token from disk."""
    try:
        if TOKEN_FILE.exists():
            data = json.loads(TOKEN_FILE.read_text())
            print(f"Loaded persisted token from {TOKEN_FILE}")
            return data
    except Exception as e:
        print(f"Warning: could not load token: {e}")
    return None


# ============================================================
# OAUTH CALLBACK — accepts code from browser redirect
# ============================================================

@app.get("/callback")
async def callback(code: str = None):
    global authorization_code

    if not code:
        return {"error": "Authorization failed", "message": "No code received"}

    authorization_code = code
    print(f"\n✓ Authorization code received")

    return HTMLResponse("""
    <html><body style="font-family:sans-serif;text-align:center;padding:60px">
      <h2 style="color:#16a34a">✓ Authorization Successful</h2>
      <p>You can close this tab and return to the terminal.</p>
    </body></html>
    """)


# ============================================================
# AUTHENTICATION
# ============================================================

async def authenticate():
    global auth, plants_api, authenticated

    auth = Auth(SERVER, APP_KEY, SECRET_KEY, APP_ID)

    # Try to restore a persisted token first
    saved = load_token()
    if saved:
        try:
            auth.tokens = saved  # Restore token into the Auth object
            plants_api  = Plants(auth)
            authenticated = True
            print("\n✓ Restored authentication from saved token")
            await load_plants()
            return
        except Exception as e:
            print(f"Saved token invalid ({e}), re-authenticating...")
            authenticated = False

    # Need fresh OAuth flow
    url = auth.auth_url(REDIRECT_URI)

    print("\n" + "=" * 60)
    print("  iSolarCloud OAuth — Action Required")
    print("=" * 60)
    print(f"\n  Open this URL in your browser:\n\n  {url}\n")
    print(f"  Or set BRIDGE_REDIRECT_URI={REDIRECT_URI} if running remotely.\n")

    # Try to open browser automatically (only works on desktop)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print("  Waiting for authorization (complete OAuth in browser)...")

    while authorization_code is None:
        await asyncio.sleep(1)

    print("  Exchanging authorization code...")

    await auth.async_authorize(authorization_code, REDIRECT_URI)

    # Persist the token
    try:
        token_data = auth.tokens if hasattr(auth, "_token") else {}
        save_token(token_data)
    except Exception as e:
        print(f"  Warning: could not persist token: {e}")

    plants_api    = Plants(auth)
    authenticated = True
    print("\n✓ Authentication successful!\n")

    await load_plants()


# ============================================================
# GET PLANTS (with details merged)
# ============================================================

async def load_plants():
    global plant_cache

    if not authenticated:
        return []

    print("Fetching plants from iSolarCloud...")

    plants = await plants_api.async_get_plants()

    if plants:
        plant_ids = [str(p["ps_id"]) for p in plants]
        try:
            details_list = await plants_api.async_get_plant_details(plant_ids)
            details_map = {
                str(d.get("ps_id", d.get("id", ""))): d
                for d in (details_list or [])
            }
            for plant in plants:
                pid = str(plant["ps_id"])
                if pid in details_map:
                    plant.update({
                        k: v for k, v in details_map[pid].items()
                        if k not in plant or plant[k] is None
                    })
        except Exception as e:
            print(f"  Warning: could not fetch plant details: {e}")

    plant_cache = plants or []

    print(f"Found {len(plant_cache)} plant(s):")
    for p in plant_cache:
        print(f"  {p.get('ps_id')} — {p.get('ps_name')} "
              f"({p.get('installed_capacity', '?')} kWp)")

    return plant_cache


# ============================================================
# GET REALTIME DATA
# ============================================================

async def fetch_realtime():
    global realtime_cache

    if not authenticated:
        return {}

    if not plant_cache:
        await load_plants()

    if not plant_cache:
        return {}

    plant_ids = [str(p["ps_id"]) for p in plant_cache]

    print(f"[{datetime.now().strftime('%H:%M:%S')}] Fetching realtime data...")

    realtime = await plants_api.async_get_realtime_data(plant_ids)

    realtime_cache = realtime or {}
    return realtime_cache


def clean_realtime_data(data):
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = {"value": value.get("value"), "unit": value.get("unit")}
        else:
            result[key] = value
    return result


# ============================================================
# API ENDPOINTS
# ============================================================

@app.get("/api/health")
async def health():
    return {
        "status":        "ok",
        "authenticated": authenticated,
        "redirect_uri":  REDIRECT_URI,
        "token_file":    str(TOKEN_FILE),
        "token_saved":   TOKEN_FILE.exists(),
        "plant_count":   len(plant_cache),
    }


@app.get("/api/auth-url")
async def auth_url():
    """Return the OAuth URL — useful when running in Docker without a browser."""
    if not auth:
        return {"error": "Bridge not initialised yet, try again in a moment"}
    return {
        "auth_url":     auth.auth_url(REDIRECT_URI),
        "redirect_uri": REDIRECT_URI,
    }


@app.get("/api/plants")
async def get_plants():
    if not authenticated:
        return {"authenticated": False, "plants": []}
    if not plant_cache:
        await load_plants()
    return {
        "authenticated": True,
        "count":  len(plant_cache),
        "plants": plant_cache,
    }


@app.get("/api/realtime")
async def get_realtime():
    if not authenticated:
        return {"authenticated": False, "message": "Not authenticated"}
    try:
        realtime = await fetch_realtime()
        response = {
            str(pid): clean_realtime_data(data)
            for pid, data in realtime.items()
        }
        return {
            "authenticated": True,
            "timestamp": datetime.now().isoformat(),
            "plants": response,
        }
    except Exception as e:
        print("Realtime error:", e)
        return {"authenticated": True, "error": str(e)}


@app.get("/api/realtime/{plant_id}")
async def get_plant_realtime(plant_id: str):
    if not authenticated:
        return {"authenticated": False}
    try:
        realtime = await fetch_realtime()
        data = realtime.get(plant_id)
        if data is None:
            return {"error": "Plant not found", "plant_id": plant_id}
        return {
            "plant_id":  plant_id,
            "timestamp": datetime.now().isoformat(),
            "data":      clean_realtime_data(data),
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():
    asyncio.create_task(authenticate())


# ============================================================
# RUN (for direct execution / local testing)
# ============================================================

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)