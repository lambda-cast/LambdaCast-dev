"""
forecast_service.py
===================
Backend service that dynamically loads ML models from the models/ directory
and generates PV power forecasts using real weather data from Open-Meteo.

Supported models in models/:
  - arx_model.pkl (AutoReg with 30 lags & exogenous weather features)
  - xgb_PV1_Power_W_1.joblib (XGBoost Regressor)

Features required by both models:
  ['Solar_Radiation_Wm2', 'Outdoor_Temp_C', 'Dew_Point_C', 'Wind_Speed_ms',
   'Wind_Dir_deg', 'Humidity_pct', 'Rain_mm', 'PM25_ugm3', 'PM10_ugm3']
"""

import os
import glob
import logging
import pickle
import json
import urllib.request
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd

# Lazy joblib import
_joblib = None


def _get_joblib():
    global _joblib
    if _joblib is None:
        import joblib
        _joblib = joblib
    return _joblib


MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models")
_loaded_models = {}

WEATHER_FEATURES = [
    "Solar_Radiation_Wm2",
    "Outdoor_Temp_C",
    "Dew_Point_C",
    "Wind_Speed_ms",
    "Wind_Dir_deg",
    "Humidity_pct",
    "Rain_mm",
    "PM25_ugm3",
    "PM10_ugm3",
]

ARX_EXOG_COLS = [
    "Wind_Dir_deg",
    "Wind_Speed_ms",
    "Humidity_pct",
    "Outdoor_Temp_C",
    "Solar_Radiation_Wm2",
    "PM10_ugm3",
    "PM25_ugm3",
    "Rain_mm",
    "dew_point_2m (°C)",
]


def discover_models():
    """Discover all trained models in models/ folder."""
    models_dir = os.path.abspath(MODELS_DIR)
    if not os.path.isdir(models_dir):
        return []

    found = []
    for ext in ("*.joblib", "*.pkl"):
        for path in sorted(glob.glob(os.path.join(models_dir, ext))):
            basename = os.path.basename(path)
            model_id = os.path.splitext(basename)[0]
            if "xgb" in model_id.lower():
                label = f"XGBoost ({model_id})"
                m_type = "xgb"
            elif "arx" in model_id.lower():
                label = f"ARX ({model_id})"
                m_type = "arx"
            else:
                label = f"Modèle ({model_id})"
                m_type = "generic"

            found.append({
                "id": model_id,
                "label": label,
                "type": m_type,
                "filename": basename,
            })
    return found


def load_model(name):
    """Load a model by its filename or ID."""
    if name in _loaded_models:
        return _loaded_models[name]

    models_dir = os.path.abspath(MODELS_DIR)
    # Match ID or filename
    target_path = None
    for ext in (".joblib", ".pkl"):
        candidate = os.path.join(models_dir, name + ext)
        if os.path.isfile(candidate):
            target_path = candidate
            break
        candidate_direct = os.path.join(models_dir, name)
        if os.path.isfile(candidate_direct):
            target_path = candidate_direct
            break

    if not target_path:
        raise ValueError(f"Model '{name}' not found in {MODELS_DIR}")

    ext = os.path.splitext(target_path)[1].lower()
    if ext == ".pkl":
        with open(target_path, "rb") as f:
            obj = pickle.load(f)
        type_name = type(obj).__name__
        model_type = "arx" if ("AutoReg" in type_name or "arx" in name.lower()) else "generic"
    elif ext == ".joblib":
        obj = _get_joblib().load(target_path)
        type_name = type(obj).__name__
        model_type = "xgb" if ("XGB" in type_name or "xgb" in name.lower()) else "generic"
    else:
        raise ValueError(f"Unsupported format {ext}")

    entry = {"type": model_type, "model": obj, "filename": os.path.basename(target_path)}
    _loaded_models[name] = entry
    logging.info(f"ForecastService: loaded {name} ({type_name})")
    return entry


def fetch_weather_dataframe(lat, lon, days=3):
    """
    Fetch weather from Open-Meteo weather & air quality APIs.
    Constructs a DataFrame with all 9 required features for the given coordinates.
    """
    url_w = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=shortwave_radiation,temperature_2m,dew_point_2m,"
        f"wind_speed_10m,wind_direction_10m,relative_humidity_2m,rain"
        f"&wind_speed_unit=ms&forecast_days={days}&timezone=auto"
    )

    try:
        req = urllib.request.Request(url_w, headers={"User-Agent": "LambdaCast/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            dw = json.loads(r.read().decode())["hourly"]
    except Exception as e:
        logging.error(f"ForecastService: Open-Meteo weather failed: {e}")
        raise

    times = dw["time"]
    n = len(times)

    # Fetch air quality for PM2.5 and PM10
    pm10_list = [22.0] * n
    pm25_list = [11.0] * n
    try:
        url_aq = (
            f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
            f"&hourly=pm10,pm2_5&forecast_days={days}&timezone=auto"
        )
        req_aq = urllib.request.Request(url_aq, headers={"User-Agent": "LambdaCast/1.0"})
        with urllib.request.urlopen(req_aq, timeout=6) as r:
            daq = json.loads(r.read().decode())["hourly"]
            if "pm10" in daq and daq["pm10"]:
                pm10_list = [(x if x is not None else 20.0) for x in daq["pm10"]][:n]
            if "pm2_5" in daq and daq["pm2_5"]:
                pm25_list = [(x if x is not None else 10.0) for x in daq["pm2_5"]][:n]
    except Exception as e:
        logging.warning(f"ForecastService: Open-Meteo air quality fallback: {e}")

    df = pd.DataFrame({
        "time": pd.to_datetime(times),
        "Solar_Radiation_Wm2": np.array([(x if x is not None else 0.0) for x in dw.get("shortwave_radiation", [0.0]*n)], dtype=np.float64),
        "Outdoor_Temp_C": np.array([(x if x is not None else 25.0) for x in dw.get("temperature_2m", [25.0]*n)], dtype=np.float64),
        "Dew_Point_C": np.array([(x if x is not None else 15.0) for x in dw.get("dew_point_2m", [15.0]*n)], dtype=np.float64),
        "Wind_Speed_ms": np.array([(x if x is not None else 2.0) for x in dw.get("wind_speed_10m", [2.0]*n)], dtype=np.float64),
        "Wind_Dir_deg": np.array([(x if x is not None else 180.0) for x in dw.get("wind_direction_10m", [180.0]*n)], dtype=np.float64),
        "Humidity_pct": np.array([(x if x is not None else 50.0) for x in dw.get("relative_humidity_2m", [50.0]*n)], dtype=np.float64),
        "Rain_mm": np.array([(x if x is not None else 0.0) for x in dw.get("rain", [0.0]*n)], dtype=np.float64),
        "PM25_ugm3": np.array(pm25_list, dtype=np.float64),
        "PM10_ugm3": np.array(pm10_list, dtype=np.float64),
    })

    df["dew_point_2m (°C)"] = df["Dew_Point_C"]
    df = df.bfill().ffill().fillna(0.0)
    return df


def predict_model(model_entry, df):
    """Run prediction on the weather dataframe."""
    m_type = model_entry["type"]
    model = model_entry["model"]

    if m_type == "xgb":
        X = df[WEATHER_FEATURES]
        raw_preds = model.predict(X)
        # Solar constraint: no output when radiation is 0 (nighttime)
        preds = np.where(df["Solar_Radiation_Wm2"] <= 5.0, 0.0, np.maximum(0.0, raw_preds))
        return preds

    elif m_type == "arx":
        params = model.params
        const = params.iloc[0]
        ar_coefs = params.iloc[1:31].values
        exog_coefs = params.iloc[31:].values
        exog = df[ARX_EXOG_COLS].values

        history = [0.0] * 30
        preds = []
        for i in range(len(df)):
            if df.loc[i, "Solar_Radiation_Wm2"] <= 5.0:
                val = 0.0
            else:
                ar_part = sum(ar_coefs[j] * history[-1 - j] for j in range(30))
                exog_part = np.dot(exog_coefs, exog[i])
                val = max(0.0, float(const + ar_part + exog_part))
            preds.append(val)
            history.append(val)
        return np.array(preds)

    else:
        # Generic fallback
        X = df[WEATHER_FEATURES]
        raw_preds = model.predict(X)
        return np.where(df["Solar_Radiation_Wm2"] <= 5.0, 0.0, np.maximum(0.0, raw_preds))


def get_forecast(model_name=None, lat=34.73, lon=10.72, target_date=None, capacity_kwp=None):
    """
    Produce forecast results for API consumption.
    
    capacity_kwp: installed capacity of the target installation (kWp).
                  Used to scale model predictions to the correct magnitude.
    """
    models = discover_models()
    if not models:
        raise ValueError("No models available in models/ folder")

    if not model_name:
        model_name = models[0]["id"]

    model_entry = load_model(model_name)
    df = fetch_weather_dataframe(lat, lon, days=3)
    preds = predict_model(model_entry, df)

    # ── Capacity scaling ──────────────────────────────────────────────────────
    # Models may have been trained on a different installation than the target.
    # We estimate the training installation's peak power from the model's own
    # predictions under ideal conditions (Solar_Radiation≈900 W/m², no rain).
    # Then we rescale so that the target installation's predictions are
    # proportional to its actual installed capacity.
    if capacity_kwp and capacity_kwp > 0:
        # Estimate what the model considers "peak" output
        peak_mask = df["Solar_Radiation_Wm2"] >= 800
        if peak_mask.any():
            model_peak_w = float(np.percentile(preds[peak_mask], 90))
        else:
            model_peak_w = float(preds.max()) if preds.max() > 0 else 1.0

        # Assume a standard 0.75 performance ratio when estimating training capacity
        PERFORMANCE_RATIO = 0.75
        training_capacity_kwp = model_peak_w / (1000 * PERFORMANCE_RATIO)  # W → kWp

        if training_capacity_kwp > 0 and abs(training_capacity_kwp - capacity_kwp) > 0.05:
            scale = capacity_kwp / training_capacity_kwp
            preds = preds * scale
            logging.info(
                f"ForecastService: scaled '{model_name}' predictions by {scale:.3f} "
                f"(model peak {model_peak_w:.0f}W → training ~{training_capacity_kwp:.2f}kWp, "
                f"target {capacity_kwp:.2f}kWp)"
            )
    # ─────────────────────────────────────────────────────────────────────────
    df["predicted_w"] = preds
    df["date_str"] = df["time"].dt.strftime("%Y-%m-%d")
    df["hour_str"] = df["time"].dt.strftime("%H:%M")

    # If a target date is specified, filter hourly to that date (e.g. 24 hours of that day)
    # otherwise default to today
    today_str = datetime.now().strftime("%Y-%m-%d")
    selected_date = target_date or today_str

    day_df = df[df["date_str"] == selected_date]
    if day_df.empty:
        day_df = df[df["date_str"] == today_str]

    hourly_list = []
    for _, row in day_df.iterrows():
        hourly_list.append({
            "time": row["hour_str"],
            "hour": row["hour_str"],
            "predicted_w": round(float(row["predicted_w"]), 1),
            "solar_radiation": round(float(row["Solar_Radiation_Wm2"]), 1),
            "temp_c": round(float(row["Outdoor_Temp_C"]), 1),
            "humidity_pct": round(float(row["Humidity_pct"]), 1),
        })

    # Daily aggregation
    daily_groups = df.groupby("date_str").agg(
        total_kwh=("predicted_w", lambda x: max(0.0, round(float(x.sum()) / 1000.0, 2))),
        peak_w=("predicted_w", lambda x: round(float(x.max()), 1)),
    ).reset_index()

    daily_summary = []
    for _, r in daily_groups.iterrows():
        daily_summary.append({
            "date": r["date_str"],
            "total_kwh": r["total_kwh"],
            "peak_w": r["peak_w"],
        })

    # Tomorrow's stats
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    tom_df = df[df["date_str"] == tomorrow_str]
    peak_hour = "13:00"
    tomorrow_kwh = 0.0
    if not tom_df.empty:
        peak_idx = tom_df["predicted_w"].idxmax()
        peak_hour = tom_df.loc[peak_idx, "hour_str"]
        tomorrow_kwh = round(float(tom_df["predicted_w"].sum()) / 1000.0, 2)

    return {
        "status": "ok",
        "selected_model": model_name,
        "model_label": next((m["label"] for m in models if m["id"] == model_name), model_name),
        "available_models": models,
        "date": selected_date,
        "hourly": hourly_list,
        "daily": daily_summary,
        "tomorrow_kwh": tomorrow_kwh,
        "peak_hour_tomorrow": peak_hour,
    }
