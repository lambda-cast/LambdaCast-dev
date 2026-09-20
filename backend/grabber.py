import os
import time
import logging
import importlib
import signal
from os.path import exists
from datetime import date, datetime

# Project imports
from config import Config
from database import Database
import version


# Real time (24h) data
NUM_REAL_TIME_VALUES = 24*60  # 24h * 60 Minutes
# Per-installation real-time counters (keyed by db_path)
real_time_counters = {}
config = None
run = True


# Helper function to insert new values into the DB
def insert_historical_values(
        db,
        table_name,
        date_string,
        produced,
        consumed,
        fed_in):
    '''Helper function to insert new values into the DB.

    Schema: (date, produced_a, produced_b, consumed_a, consumed_b, fed_in_a, fed_in_b)
    The _a columns hold the cumulative meter reading at the START of the period.
    The _b columns hold the latest cumulative reading.
    Period production = b - a.

    Special case: if the row was first written when the device was offline
    (all zeros), the _a baseline is wrong (0 instead of the actual meter
    value at period start).  We detect this by checking whether _a == 0
    while the current reading is non-zero, and correct _a in that case so
    that the period delta starts from "now" rather than showing lifetime
    totals.
    '''
    query = f"SELECT * FROM {table_name} WHERE date='{date_string}'"
    rows = db.execute(query)

    if len(rows) == 0:
        # New period — baseline = current meter reading
        query = (f"INSERT INTO {table_name} VALUES ('{date_string}',"
                 f"{str(produced)}, {str(produced)}, "
                 f"{str(consumed)}, {str(consumed)}, "
                 f"{str(fed_in)}, {str(fed_in)})")
        db.execute(query)
    else:
        # Row exists — check if baseline was written as zero while device
        # was offline (produced_a == 0 but we now have a real reading).
        # Note: all_time intentionally starts with a=0 (lifetime accumulator),
        # so we only apply the baseline correction for time-bounded periods.
        existing_produced_a = rows[0][1]
        if (table_name != "all_time"
                and existing_produced_a == 0.0
                and produced > 0.0):
            # Correct the baseline: treat this moment as the start of the
            # period so no historical kWh are attributed to it.
            query = (f"UPDATE {table_name} SET "
                     f"produced_a = {str(produced)}, produced_b = {str(produced)}, "
                     f"consumed_a = {str(consumed)}, consumed_b = {str(consumed)}, "
                     f"fed_in_a = {str(fed_in)}, fed_in_b = {str(fed_in)} "
                     f"WHERE date='{date_string}'")
            logging.info(
                f"Grabber: corrected zero baseline in {table_name}/{date_string} "
                f"(produced_a set to {produced:.3f} kWh)"
            )
        else:
            # Normal update — only advance the _b (latest) reading
            query = (f"UPDATE {table_name} SET "
                     f"produced_b = {str(produced)}, "
                     f"consumed_b = {str(consumed)}, "
                     f"fed_in_b = {str(fed_in)} WHERE date='{date_string}'")
        db.execute(query)


# Helper function to insert current values into the DB
def insert_current_values(
        db,
        produced,
        consumed_grid,
        consumed_pv,
        consumed_total,
        fed_in):
    '''Helper function to insert current values into the DB.'''
    rows = db.execute("SELECT * FROM current WHERE date='cur'")

    if len(rows) == 0:
        # Create day row
        query = (f"INSERT INTO current VALUES ('cur', "
                 f"{str(produced)}, {str(consumed_grid)}, "
                 f"{str(consumed_pv)}, {str(consumed_total)}, "
                 f"{str(fed_in)})")
        db.execute(query)
    else:
        # Update existing row
        query = (f"UPDATE current SET "
                 f"produced = {str(produced)}, "
                 f"consumed_grid = {str(consumed_grid)}, "
                 f"consumed_pv = {str(consumed_pv)}, "
                 f"consumed_total = {str(consumed_total)}, "
                 f"fed_in = {str(fed_in)} "
                 f"WHERE date='cur'")
        db.execute(query)


# Helper function to insert the high score values into the DB
def insert_high_scores(
        db,
        date_str,
        current_production_kw):
    '''Helper function to insert high score values into the DB.'''
    # Make sure table exists
    query = ("CREATE TABLE IF NOT EXISTS highscores "
             "(type STRING PRIMARY KEY, date STRING, value REAL)")
    db.execute(query)
    # Get current value
    query = "SELECT * FROM highscores WHERE type IS 'production'"
    rows = db.execute(query)
    if not rows:
        cur_high_score_value = 0.0
        query = ("INSERT INTO highscores (type,date,value) "
                 "VALUES('production','...',0.0);")
        db.execute(query)
    else:
        cur_high_score_value = rows[0][2]
    # Check if we have a high score
    if current_production_kw > cur_high_score_value:
        query = (f"UPDATE highscores SET "
                 f"value = {str(current_production_kw)}, "
                 f"date = '{date_str}' WHERE type IS 'production'")
        db.execute(query)


# Helper function to insert new values into the DB
def insert_real_time_values(db, time_string2, produced, consumed, fed_in):
    '''Helper function to insert new values into the DB.'''
    # Insert new data
    query = (f"INSERT INTO real_time (time, produced, consumed, fed_in) "
             f"VALUES('{time_string2}', {produced}, {consumed}, {fed_in})")
    db.execute(query)
    # Limit data
    query = (f"DELETE FROM real_time WHERE ID IN ("
             f"SELECT ID FROM real_time "
             f"ORDER BY ID DESC "
             f"LIMIT -1 OFFSET {NUM_REAL_TIME_VALUES})")
    db.execute(query)


# Helper function to insert high res values into the DB
def insert_high_res_values(
        db,
        day_string,
        time_string,
        produced,
        consumed,
        fed_in):
    '''Helper function to insert high res values into the DB.'''
    # Make sure table exists
    query = ("create table if not exists high_res "
             "(date STRING PRIMARY KEY, hrvalues STRING)")
    db.execute(query)

    # Get current entry
    query = (f"SELECT * FROM high_res WHERE date='{day_string}'")
    rows = db.execute(query)
    old_values = ""
    if not rows:
        # Create new row
        query = (f"INSERT INTO high_res (date,hrvalues) "
                 f"VALUES ('{day_string}', '');")
        db.execute(query)
    else:
        old_values = rows[0][1]

    # Append new values to old values
    new_value = (f"[\"{time_string}\","
                 f"{str(round(produced, 3))},"
                 f"{str(round(consumed, 3))},"
                 f"{str(round(fed_in, 3))}],")
    old_values += new_value

    # Update DB
    query = (f"UPDATE high_res SET "
             f"hrvalues = '{old_values}' "
             f"WHERE date='{day_string}'")
    db.execute(query)


# Helper function to create a new DB
def create_new_db(db_path):
    '''Helper function to create a new DB.'''
    new_db = Database(db_path)

    # Historical data tables
    table_names = ["days", "months", "years", "all_time"]
    for name in table_names:
        query = (f"create table if not exists {name} ("
                 "date STRING PRIMARY KEY,"
                 "produced_a REAL, produced_b REAL,"
                 "consumed_a REAL, consumed_b REAL,"
                 "fed_in_a REAL, fed_in_b REAL)")
        new_db.execute(query)

    # Add initial all time row
    query = ("INSERT INTO all_time VALUES ('all_time',0,0,0,0,0,0)")
    new_db.execute(query)

    # Current data table
    query = ("create table if not exists current"
             "(date STRING PRIMARY KEY, "
             "produced REAL, consumed_grid REAL, consumed_pv REAL, "
             "consumed_total REAL, fed_in REAL)")
    new_db.execute(query)

    # Real time data table
    query = ("create table if not exists real_time"
             "(ID INTEGER PRIMARY KEY AUTOINCREMENT, "
             "time STRING, produced REAL, consumed REAL, fed_in REAL)")
    new_db.execute(query)
    # Insert null data
    for x in range(NUM_REAL_TIME_VALUES):  # 24h * 60 minutes
        query = (f"INSERT INTO real_time VALUES"
                 f"('{str(x)}', '...', '0.0', '0.0', '0.0')")
        new_db.execute(query)

    # Add highscores
    query = ("CREATE TABLE IF NOT EXISTS highscores "
             "(type STRING PRIMARY KEY, date STRING, value REAL)")
    new_db.execute(query)
    query = ("INSERT INTO highscores (type,date,value) "
             "VALUES('production','...',0.0);")
    new_db.execute(query)

    # Add high res data table
    query = ("CREATE TABLE IF NOT EXISTS high_res "
             "(date STRING PRIMARY KEY, hrvalues STRING)")
    new_db.execute(query)


# Loads the device class with the given name
def load_device_plugin(device_name, device_config):
    '''Loads the device class with the given name.'''
    module = importlib.import_module("devices." + device_name)
    class_ = getattr(module, device_name)
    device = class_(type("ConfigMock", (), {"config_data": {"device": device_config}})())
    return device


# Sets the time zone environment variable
def set_time_zone(tz):
    '''Sets the time zone environment variable.'''
    if tz is None:
        logging.warn("Grabber: Warning: No time zone set")
    else:
        logging.info(f"Grabber: Setting tme zone to {tz}")
        os.environ['TZ'] = tz
        time.tzset()
        logging.info(f"Grabber: Time is now {time.strftime('%X %x %Z')}")


# Updates data in the data base

def update_data(device, db_path):
    '''Updates data in the data base.'''
    global real_time_counters

    # Per-installation real-time countdown (seconds until next 1-minute sample)
    if db_path not in real_time_counters:
        real_time_counters[db_path] = 0

    # Download new data from the actual PV device
    device.update()

    # Open connection to data base
    db = Database(db_path)


    # Time strings
    year_string = date.today().strftime("%Y")
    month_string = year_string + "-" + date.today().strftime("%m")
    day_string = month_string + "-" + date.today().strftime("%d")

    # Capture daily data
    insert_historical_values(
        db,
        "days",
        day_string,
        device.total_energy_produced_kwh,
        device.total_energy_consumed_kwh,
        device.total_energy_fed_in_kwh)

    # Capture monthly data
    insert_historical_values(
        db,
        "months", month_string,
        device.total_energy_produced_kwh,
        device.total_energy_consumed_kwh,
        device.total_energy_fed_in_kwh)

    # Capture yearly data
    insert_historical_values(
        db,
        "years",
        year_string,
        device.total_energy_produced_kwh,
        device.total_energy_consumed_kwh,
        device.total_energy_fed_in_kwh)

    # Capture all time data
    insert_historical_values(
        db,
        "all_time",
        "all_time",
        device.total_energy_produced_kwh,
        device.total_energy_consumed_kwh,
        device.total_energy_fed_in_kwh)

    # Store the current values
    insert_current_values(
        db,
        device.current_power_produced_kw,
        device.current_power_consumed_from_grid_kw,
        device.current_power_consumed_from_pv_kw,
        device.current_power_consumed_total_kw,
        device.current_power_fed_in_kw)

    # Store the high scores
    insert_high_scores(db, day_string, device.current_power_produced_kw)

    # Store the real time data
    real_time_counters[db_path] -= config.config_data['grabber']['interval_s']
    if real_time_counters[db_path] <= 0:
        # Time string
        time_string = datetime.now().strftime("%H:%M")
        # Store in data base
        if logging.getLogger().level == logging.DEBUG:
            logging.debug((f"Grabber: capturing real time data({time_string}:"
                           f"{device.current_power_produced_kw}, "
                           f"{device.current_power_consumed_total_kw}, "
                           f"{device.current_power_fed_in_kw})"))

        insert_real_time_values(
            db,
            time_string,
            device.current_power_produced_kw,
            device.current_power_consumed_total_kw,
            device.current_power_fed_in_kw)

        insert_high_res_values(
            db,
            day_string,
            time_string,
            device.current_power_produced_kw,
            device.current_power_consumed_total_kw,
            device.current_power_fed_in_kw)

        real_time_counters[db_path] = 60  # Reset counter to one minute


# This is called when SIGTERM is received
def handler_stop_signals(signum, frame):
    global run
    logging.debug("Grabber: SIGTERM/SIGINT received")
    run = False


# Main loop
def main():
    '''Main loop.'''
    global config

    # Set up signal handlers
    signal.signal(signal.SIGINT, handler_stop_signals)
    signal.signal(signal.SIGTERM, handler_stop_signals)

    # Set up logging
    logging.basicConfig(
        filename='data/grabber.log', filemode='w',
        format='%(asctime)s %(levelname)-8s %(message)s',
        level=logging.INFO,
        datefmt='%Y-%m-%d %H:%M:%S')

    # Print version
    logging.info(f"Starting Sunalyzer grabber version {version.get_version()}")

    # Read the configuration from disk
    try:
        logging.info("Grabber: Reading backend configuration from config.yml")
        config = Config("data/config.yml")
    except Exception:
        exit()

    # Set log level
    logging.getLogger().setLevel(config.log_level)

    # Set time zone
    set_time_zone(config.config_data.get("time_zone"))

    # Dynamically load the devices
    # Source 1: platform.db (installations with device_type set)
    # Source 2: config.yml devices section (legacy / override)
    # config.yml takes precedence when the same inst_id appears in both.
    devices = {}
    try:
        import json as _json
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from platform_db import PlatformDatabase

        pdb = PlatformDatabase()
        db_installations = pdb.fetchall(
            "SELECT id, name, device_type, device_params "
            "FROM installations WHERE device_type IS NOT NULL AND device_type != ''"
        )
        for row in db_installations:
            inst_id    = str(row["id"])
            dev_type   = row["device_type"]
            dev_params = _json.loads(row["device_params"]) if row.get("device_params") else {}
            dev_params["type"] = dev_type
            logging.info(
                f"Grabber: [platform.db] Loading '{dev_type}' "
                f"for installation {inst_id} ({row['name']})"
            )
            try:
                devices[inst_id] = load_device_plugin(dev_type, dev_params)
            except Exception:
                logging.exception(
                    f"Grabber: failed to load device '{dev_type}' "
                    f"for installation {inst_id} — skipping"
                )
    except Exception:
        logging.exception("Grabber: failed to read device configs from platform.db")

    # config.yml devices section (adds or overrides)
    try:
        if 'devices' in config.config_data:
            for inst_id, dev_cfg in config.config_data['devices'].items():
                inst_id = str(inst_id)
                device_name = dev_cfg['type']
                if inst_id in devices:
                    logging.info(
                        f"Grabber: [config.yml] Overriding installation {inst_id} "
                        f"with '{device_name}'"
                    )
                else:
                    logging.info(
                        f"Grabber: [config.yml] Loading '{device_name}' "
                        f"for installation {inst_id}"
                    )
                try:
                    devices[inst_id] = load_device_plugin(device_name, dev_cfg)
                except Exception:
                    logging.exception(
                        f"Grabber: failed to load device '{device_name}' "
                        f"for installation {inst_id} from config.yml"
                    )
        elif not devices and 'device' in config.config_data:
            # Fallback: old single-device config
            device_name = config.config_data['device']['type']
            logging.info(f"Grabber: [config.yml legacy] Loading '{device_name}'")
            devices['default'] = load_device_plugin(device_name, config.config_data['device'])
    except Exception:
        logging.exception("Grabber: failed to load devices from config.yml")

    if not devices:
        logging.error("Grabber: no devices configured — nothing to poll. "
                      "Add installations via the web UI or config.yml.")
        exit()

    # Prepare the data bases
    for inst_id in devices.keys():
        db_path = f"data/db_{inst_id}.sqlite" if inst_id != 'default' else "data/db.sqlite"
        logging.info(f"Grabber: Checking if data base exists at {db_path}")
        if not exists(db_path):
            logging.info(f"Grabber: Data base {db_path} does not exist. Creating new one")
            create_new_db(db_path)

    # Grabber main loop
    logging.debug("Grabber: Entering main loop")
    while run:
        if logging.getLogger().level == logging.DEBUG:
            time_string = datetime.now().strftime("%H:%M")
            logging.debug(f"Grabber: {time_string}: Updating device data")

        for inst_id, device in devices.items():
            db_path = f"data/db_{inst_id}.sqlite" if inst_id != 'default' else "data/db.sqlite"
            try:
                update_data(device, db_path)
            except Exception as e:
                logging.exception(f"Updating data from device for installation {inst_id} failed")

        time.sleep(config.config_data['grabber']['interval_s'])

    # Exit
    logging.info("Grabber: Exiting main loop")
    logging.info("Grabber: Shutting down gracefully")


# Main entry point of the application
if __name__ == "__main__":
    main()
