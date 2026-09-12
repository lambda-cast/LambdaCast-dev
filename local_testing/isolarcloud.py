import asyncio
import webbrowser
from aiohttp import web

from pysolarcloud import Auth, Server
from pysolarcloud.plants import Plants


# =========================
# YOUR APPLICATION SETTINGS
# =========================

APP_KEY = "103B38551A4024599979710A849AB676"
SECRET_KEY = "pdxu4xe4x2jkkf26q123039nva2x91gj"
APP_ID = "3914"

REDIRECT_URI = "http://localhost:8000/callback"

# Choose the server matching your iSolarCloud region.
SERVER = Server.International


# =========================
# GLOBAL
# =========================

auth_code = None


# =========================
# OAUTH CALLBACK
# =========================

async def callback(request):
    global auth_code

    auth_code = request.query.get("code")

    if not auth_code:
        return web.Response(
            text="Authorization failed: no code received.",
            status=400
        )

    print("\nAuthorization code received!")
    print("You can return to the terminal.")

    return web.Response(
        text="Authorization successful! You can close this browser tab."
    )


async def main():

    # Create authentication object
    auth = Auth(
        SERVER,
        APP_KEY,
        SECRET_KEY,
        APP_ID
    )

    # Generate authorization URL
    url = auth.auth_url(REDIRECT_URI)

    print("\n====================================")
    print("iSolarCloud OAuth Authentication")
    print("====================================\n")

    print("Open this URL in your browser:\n")
    print(url)

    # Open browser automatically
    webbrowser.open(url)

    # Start local callback server
    app = web.Application()
    app.router.add_get("/callback", callback)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, "localhost", 8000)
    await site.start()

    print("\nWaiting for iSolarCloud authorization...")
    print("Login → select your plant → authorize\n")

    # Wait until callback receives code
    while auth_code is None:
        await asyncio.sleep(1)

    # Exchange authorization code for token
    print("Exchanging authorization code for access token...")

    await auth.async_authorize(
        auth_code,
        REDIRECT_URI
    )

    print("Authentication successful!\n")

    # =========================
    # GET PLANTS
    # =========================

    plants_api = Plants(auth)

    print("Fetching plants...\n")

    plant_list = await plants_api.async_get_plants()

    if not plant_list:
        print("No plants found.")
        await runner.cleanup()
        return

    print(f"{len(plant_list)} plant(s) found:\n")

    for plant in plant_list:
        print(
            f"Plant ID: {plant['ps_id']} | "
            f"Name: {plant['ps_name']}"
        )

    # =========================
    # PLANT DETAILS
    # =========================

    plant_ids = [
        str(plant["ps_id"])
        for plant in plant_list
    ]

    print("\n====================================")
    print("PLANT DETAILS")
    print("====================================\n")

    plant_details = await plants_api.async_get_plant_details(
        plant_ids
    )

    for plant in plant_details:
        print(plant)

    # =========================
    # REAL-TIME DATA
    # =========================

    print("\n====================================")
    print("REAL-TIME DATA")
    print("====================================\n")

    realtime = await plants_api.async_get_realtime_data(
        plant_ids
    )

    for plant_id, data in realtime.items():

        print(f"\nPlant {plant_id}")

        for key, value in data.items():

            if value and value.get("value") is not None:

                print(
                    f"  {key}: "
                    f"{value.get('value')} "
                    f"{value.get('unit', '')}"
                )

    print("\n====================================")
    print("DONE")
    print("====================================")

    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())