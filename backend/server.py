import json
from datetime import date
import logging
from flask import Flask, request, send_from_directory, redirect
from flask_compress import Compress

# Project imports
from config import Config
from database import Database
import version

# Platform imports — multi-installation / multi-user extensions
from platform_db import PlatformDatabase
from auth import hash_password
from routes.auth_routes import auth_bp
from routes.user_routes import admin_bp
from routes.installation_routes import installations_bp
from routes.forecast_routes import forecast_bp


# Globals
config = None


# Main Flask web server application
app = Flask(__name__)
Compress(app)

# Register platform blueprints
app.register_blueprint(auth_bp)
app.register_blueprint(admin_bp)
app.register_blueprint(installations_bp)
app.register_blueprint(forecast_bp)


@app.route('/')
def get_index():
    '''Redirects root to login page.'''
    from flask import redirect
    return redirect('/login')


@app.route('/login')
def get_login():
    '''Serves the login page.'''
    return send_from_directory("../site", "login.html")


@app.route('/platform')
def get_platform():
    '''Serves the platform dashboard (user home).'''
    return send_from_directory("../site", "platform.html")


@app.route('/platform/installations')
def get_installations_page():
    '''Serves the installations list page (user) or redirects admin.'''
    return send_from_directory("../site", "installations.html")


@app.route('/platform/installations/new')
@app.route('/platform/installations/<int:installation_id>/edit')
def get_installation_form(installation_id: int = None):
    '''Serves the create/edit installation form.'''
    return send_from_directory("../site", "installation_form.html")


@app.route('/admin')
def get_admin():
    '''Serves the admin dashboard.'''
    return send_from_directory("../site", "admin.html")


@app.route('/admin/installations')
def get_admin_installations():
    '''Serves the admin installations fleet view.'''
    return send_from_directory("../site", "admin_installations.html")


@app.route('/admin/users')
def get_admin_users():
    '''Serves the admin user management page.'''
    return send_from_directory("../site", "admin_users.html")


@app.route('/admin/map')
def get_admin_map_page():
    '''Serves the admin fleet map page.'''
    return send_from_directory("../site", "admin_map.html")


@app.route('/admin/models')
def get_admin_models_page():
    '''Serves the admin forecast model management page.'''
    return send_from_directory("../site", "admin_models.html")


@app.route('/platform/map')
def get_platform_map():
    '''Serves the user installations map page.'''
    return send_from_directory("../site", "map.html")


@app.route('/platform/installations/<int:installation_id>')
def get_installation_detail(installation_id: int = None):
    '''Redirect detail page → monitor (old detail page removed).'''
    from flask import redirect
    return redirect(f'/platform/installations/{installation_id}/monitor')


@app.route('/platform/installations/<int:installation_id>/monitor')
def get_installation_monitor(installation_id: int = None):
    '''Serves the live monitor page for a specific installation.'''
    return send_from_directory("../site", "installation_monitor.html")


@app.route('/<path:path>')
# Serves all other static files
def get_file(path):
    '''Serves all other static files.'''
    return send_from_directory("../site", path)


# Returns JSON response containing current data
def get_json_data_current(db_path):
    '''Returns JSON response containing current data'''
    db = Database(db_path)
    # Current
    rows_cur = db.execute("SELECT * FROM current")
    # All time
    rows_all = db.execute("SELECT * FROM all_time")
    produced_total = rows_all[0][2] - rows_all[0][1]
    consumed_total = rows_all[0][4] - rows_all[0][3]
    fed_in_total = rows_all[0][6] - rows_all[0][5]

    # Compute all time autarky
    consumed_self_alltime = produced_total - fed_in_total
    consumed_grid_alltime = consumed_total - consumed_self_alltime
    consumed_total_alltime = consumed_self_alltime + consumed_grid_alltime
    if consumed_total_alltime > 0:
        consumed_self_rel_alltime = (consumed_self_alltime / consumed_total_alltime) * 100.0
    else:
        consumed_self_rel_alltime = 100.0

    # Today
    day_string = str(date.today())
    rows_today = db.execute(f"SELECT * FROM days WHERE date='{day_string}'")
    if rows_today and len(rows_today) > 0:
        p_b, p_a = rows_today[0][2], rows_today[0][1]
        c_b, c_a = rows_today[0][4], rows_today[0][3]
        f_b, f_a = rows_today[0][6], rows_today[0][5]
        produced_today = max(0.0, p_b - p_a) if (p_b > 0 and p_b >= p_a) else 0.0
        consumed_today = max(0.0, c_b - c_a) if (c_b > 0 and c_b >= c_a) else 0.0
        fed_in_today = max(0.0, f_b - f_a) if (f_b > 0 and f_b >= f_a) else 0.0
    else:
        produced_today = 0.0
        consumed_today = 0.0
        fed_in_today = 0.0

    # Compute todays autarky
    consumed_self_today = produced_today - fed_in_today
    consumed_grid_today = consumed_today - consumed_self_today
    consumed_total_today = consumed_self_today + consumed_grid_today
    if consumed_today > 0:
        consumed_self_rel_today = (consumed_self_today / consumed_total_today) * 100.0
    else:
        consumed_self_rel_today = 100.0

    # Compute earnings
    price = float(config.config_data['prices']['price_per_grid_kwh'])
    revenue = float(config.config_data['prices']['revenue_per_fed_in_kwh'])
    earned_total = fed_in_total * revenue
    saved_total = (produced_total - fed_in_total) * (price - revenue)
    earned_today = fed_in_today * revenue
    saved_today = (produced_today - fed_in_today) * (price - revenue)
    # Build response data
    data = {
        "state": "ok",
        "currently_produced_w": rows_cur[0][1] * 1000.0,  # kW -> W
        "currently_consumed_grid_w": rows_cur[0][2] * 1000.0,  # kW -> W
        "currently_consumed_pv_w": rows_cur[0][3] * 1000.0,  # kW -> W
        "currently_consumed_total_w": rows_cur[0][4] * 1000.0,  # kW -> W
        "currently_fed_in_w": rows_cur[0][5] * 1000.0,  # kW -> W
        "all_time_produced_kwh": produced_total,
        "all_time_consumed_kwh": consumed_total,
        "all_time_fed_in_kwh": fed_in_total,
        "all_time_earned": (earned_total + saved_total),
        "all_time_autarky": consumed_self_rel_alltime,
        "today_produced_kwh": produced_today,
        "today_consumed_kwh": consumed_today,
        "today_fed_in_kwh": fed_in_today,
        "today_earned": (earned_today + saved_today),
        "today_autarky": consumed_self_rel_today
    }
    return json.dumps(data)


# Returns JSON response containing available years
def get_json_data_statistics(db_path):
    '''Returns JSON response containing inverter statistics.'''
    # Date based data
    start_date = config.config_data['device']['start_date']
    num_days = (date.today() - start_date).days
    # Averages
    db = Database(db_path)
    rows_all_time = db.execute("SELECT * FROM all_time")
    total_production_kwh = rows_all_time[0][2]
    average_production_kwhpd = total_production_kwh / num_days
    # Best day
    rows_best_day = db.execute(
        "SELECT date, MAX(produced_b-produced_a) AS produced_kwh FROM days")
    # Best month
    rows_best_month = db.execute(
        "SELECT date, MAX(produced_b-produced_a) AS produced_kwh FROM months")
    # Best year
    rows_best_year = db.execute(
        "SELECT date, MAX(produced_b-produced_a) AS produced_kwh FROM years")
    # Highest production
    rows_highest_prod = db.execute(
        "SELECT * FROM highscores WHERE type IS 'production'")
    # Assemble result data set
    data = {
        "state": "ok",
        "start_of_operation": str(start_date),
        "days_of_operation": num_days,
        "average_daily_production_kwh": average_production_kwhpd,
        "best_day_date": rows_best_day[0][0],
        "best_day_production_kwh": rows_best_day[0][1],
        "best_month_date": rows_best_month[0][0],
        "best_month_production_kwh": rows_best_month[0][1],
        "best_year_date": rows_best_year[0][0],
        "best_year_production_kwh": rows_best_year[0][1],
        "highest_production_w": rows_highest_prod[0][2] * 1000.0,
        "highest_production_date": rows_highest_prod[0][1],
    }
    return json.dumps(data)


# Returns JSON response containing available years
def get_json_data_dates(db_path):
    '''Returns JSON response containing available years.'''
    db = Database(db_path)
    rows = db.execute("SELECT min(date) FROM years")
    data = {
        "state": "ok",
        "year_min": rows[0][0],
        "year_max": int(date.today().strftime("%Y")),
    }
    return json.dumps(data)


# Returns JSON response containing history details
def get_json_data_history_details(table, date_search_string, db_path):
    '''Returns JSON response containing history details.'''
    db = Database(db_path)
    if len(date_search_string) > 0:
        rows = db.execute(
            f"SELECT * FROM {table} WHERE date LIKE '{date_search_string}%'")
    else:
        rows = db.execute(
            f"SELECT * FROM {table}")
    # Build results
    data = []
    for row in rows:
        produced = row[2] - row[1]
        consumed = row[4] - row[3]
        fed_in = row[6] - row[5]
        data.append({
            "date": row[0],
            "produced_self": produced - fed_in,
            "produced_feed_in": fed_in,
            "consumed_from_pv": produced - fed_in,
            "consumed_from_grid": consumed - produced + fed_in
        })
    return json.dumps(data)


# Returns JSON response containing monthly data for a year
def get_json_data_real_time(hours, db_path):
    '''Returns JSON response containing monthly data for a year.'''
    num_results = int(hours) * 60
    db = Database(db_path)
    rows = db.execute(f"SELECT * FROM real_time "
                      f"ORDER BY ID DESC LIMIT {num_results}")
    return json.dumps(rows)


# Returns JSON response containing historical data
def get_json_data_history(table, search_date, db_path):
    '''Returns JSON response containing historical data.'''
    db = Database(db_path)
    rows = db.execute(f"SELECT * FROM {table} WHERE date='{search_date}'")
    # No data?
    if not rows:
        data = {
            "state": "nodata"
        }
        return json.dumps(data)
    # Compute data from sqlite columns
    produced = rows[0][2] - rows[0][1]
    consumed = rows[0][4] - rows[0][3]
    fed_in = rows[0][6] - rows[0][5]
    # Compute feed in
    consumed_self = produced - fed_in
    consumed_grid = consumed - consumed_self
    consumed_total = consumed_self + consumed_grid

    if consumed_total > 0:
        consumed_self_rel = (consumed_self / consumed_total) * 100.0
        consumed_grid_rel = (consumed_grid / consumed_total) * 100.0
    else:
        consumed_self_rel = 100.0
        consumed_grid_rel = 0.0

    # Compute usage
    if produced > 0:
        usage_fed_in_rel = fed_in / produced * 100.0
        usage_self_consumed_rel = consumed_self / produced * 100.0
    else:
        usage_fed_in_rel = 0.0
        usage_self_consumed_rel = 100.0

    # Compute earnings
    price = float(config.config_data['prices']['price_per_grid_kwh'])
    revenue = float(config.config_data['prices']['revenue_per_fed_in_kwh'])
    earned = fed_in * revenue
    saved = consumed_self * (price - revenue)

    # High resolution data (only for days)
    daily_high_res_data = ""
    if table == "days":
        rows = db.execute(f"SELECT * FROM high_res WHERE date='{search_date}'")
        if rows:
            hrdata = rows[0][1]
            if hrdata[-1] == ',':
                hrdata = hrdata[:-1]
            daily_high_res_data = "[" + hrdata + "]"

    # Build response data
    data = {
        "state": "ok",
        "produced_kwh": produced,
        "consumed_total_kwh": consumed,
        "consumed_from_pv_kwh": consumed_self,
        "consumed_from_grid_kwh": consumed_grid,
        "consumed_from_pv_percent": consumed_self_rel,
        "consumed_from_grid_percent": consumed_grid_rel,
        "usage_fed_in_kwh": fed_in,
        "usage_self_consumed_kwh": consumed_self,
        "usage_fed_in_percent": usage_fed_in_rel,
        "usage_self_consumed_percent": usage_self_consumed_rel,
        "earned_feedin": earned,
        "earned_savings": saved,
        "earned_total": (earned+saved),
        "autarky": consumed_self_rel,
        "high_res": daily_high_res_data
    }
    return json.dumps(data)


# .../query?type=current
# .../query?type=dates
# .../query?type=historical&table=days&date=2022-08-03
# etc.

@app.route("/query", methods=['GET'])
def handle_request():
    '''Answers all query requests.'''
    try:
        _type = request.args['type']
        inst_id = request.args.get('inst_id')
        if inst_id:
            db_path = f"data/db_{inst_id}.sqlite"
        else:
            db_path = "data/db.sqlite"

        logging.debug(f"Server: REST request of type '{_type}' received")

        if _type == "current":
            data = get_json_data_current(db_path)
            return data
        elif _type == "dates":
            data = get_json_data_dates(db_path)
            return data
        elif _type == "historical":
            table = request.args['table']
            _date = request.args['date']
            data = get_json_data_history(table, _date, db_path)
            return data
        elif _type == "real_time":
            hours = request.args['h']
            data = get_json_data_real_time(hours, db_path)
            return data
        elif _type == "days_in_month":
            _month = request.args['date']
            data = get_json_data_history_details("days", _month, db_path)
            return data
        elif _type == "months_in_year":
            _year = request.args['date']
            data = get_json_data_history_details("months", _year, db_path)
            return data
        elif _type == "years_in_all_time":
            data = get_json_data_history_details("years", "", db_path)
            return data
        elif _type == "statistics":
            data = get_json_data_statistics(db_path)
            return data

    except Exception:
        logging.exception("Error while handling HTTP request")
        data = {"state": "error"}
        return json.dumps(data)


@app.route("/name", methods=['GET'])
def handle_name():
    
    try:
        return json.dumps(config.config_data['sunalyzer']['name'])
        logging.debug(f"Server: REST request of type 'name' received")
    except Exception:
        logging.exception("Error while handling HTTP request")
        data = {"state": "error"}
        return json.dumps(data)


# ---------------------------------------------------------------------------
# Platform bootstrap helper
# ---------------------------------------------------------------------------

def _seed_admin_user():
    """
    Create the default admin account on first startup if no users exist yet.

    Credentials come from environment variables (or safe development defaults).
    IMPORTANT: Change these in production via env vars:
        SUNALYZER_ADMIN_USER     (default: admin)
        SUNALYZER_ADMIN_EMAIL    (default: admin@sunalyzer.local)
        SUNALYZER_ADMIN_PASSWORD (default: changeme123)
    """
    import os
    db = PlatformDatabase()
    if db.list_users():
        return  # Already has users — skip seeding

    admin_username = os.environ.get("SUNALYZER_ADMIN_USER", "admin")
    admin_email = os.environ.get("SUNALYZER_ADMIN_EMAIL", "admin@sunalyzer.local")
    admin_password = os.environ.get("SUNALYZER_ADMIN_PASSWORD", "changeme123")

    user_id = db.create_user(
        username=admin_username,
        email=admin_email,
        password_hash=hash_password(admin_password),
        role="ADMIN",
    )
    logging.info(
        f"Server: seeded default admin user '{admin_username}' (id={user_id}). "
        f"Change the password immediately via PATCH /api/admin/users/{user_id}/reset-password"
    )


# Main loop
def main():
    '''Main loop.'''

    global config

    # Set up logging
    logging.basicConfig(
        filename='data/server.log', filemode='w',
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S')

    # Print version
    logging.info(f"Starting Sunalyzer server version {version.get_version()}")

    # Read the configuration from disk
    try:
        logging.info("Server: Reading backend configuration from config.yml")
        config = Config("data/config.yml")
    except Exception:
        exit()

    # Set log level
    logging.getLogger().setLevel(config.log_level)

    # Initialise platform database and seed default admin if needed
    _seed_admin_user()

    # Start the web server
    from waitress import serve
    serve(app,
          host=config.config_data['server']['ip'],
          port=config.config_data['server']['port'])

    # Exit
    logging.info("Server: Exiting main loop")
    logging.info("Server: Shutting down gracefully")


# Main entry point of the application
if __name__ == "__main__":
    main()
