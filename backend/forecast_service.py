"""
forecast_service.py
===================
Backend service that dynamically loads ML models from the models/ directory
and generates PV power forecasts using real weather data from Open-Meteo
and air quality data from IQAir.

Supported models in models/:
  - arx_model.pkl (AutoReg with 30 lags & exogenous weather features)
  - xgb_PV1_Power_W_1.joblib (XGBoost Regressor)

Features used by all models:
  ['Solar_Radiation_Wm2', 'Outdoor_Temp_C', 'Wind_Speed_ms',
   'Humidity_pct', 'AQI_US']

AQI_US is fetched from the IQAir AirVisual API (IQAIR_API_KEY env var).
Falls back to a PM2.5-derived AQI estimate when the key is absent.
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

from iqair_service import IQAirService

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
    "Wind_Speed_ms",
    "Humidity_pct",
    "AQI_US",
]

ARX_EXOG_COLS = [
    "Wind_Speed_ms",
    "Humidity_pct",
    "Outdoor_Temp_C",
    "Solar_Radiation_Wm2",
    "AQI_US",
]


def _load_disabled_models() -> set:
    """Read the disabled_models.json sidecar produced by forecast_routes."""
    disabled_path = os.path.join(os.path.abspath(MODELS_DIR), "disabled_models.json")
    try:
        if os.path.isfile(disabled_path):
            with open(disabled_path, "r", encoding="utf-8") as fh:
                return set(json.load(fh).get("disabled", []))
    except Exception as exc:
        logging.warning(f"ForecastService: could not read disabled_models.json: {exc}")
    return set()


def discover_models():
    """Discover all enabled trained models in models/ folder."""
    models_dir = os.path.abspath(MODELS_DIR)
    if not os.path.isdir(models_dir):
        return []

    disabled = _load_disabled_models()
    found = []
    for ext in ("*.joblib", "*.pkl"):
        for path in sorted(glob.glob(os.path.join(models_dir, ext))):
            basename = os.path.basename(path)
            model_id = os.path.splitext(basename)[0]

            # Skip disabled models
            if model_id in disabled:
                continue

            if "xgb" in model_id.lower():
                label = model_id
                m_type = "xgb"
            elif "arx" in model_id.lower() or "armax" in model_id.lower():
                label = model_id
                m_type = "arx"
            else:
                label = model_id
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
        # Detect model family from the class name
        if "SARIMAX" in type_name or "ARIMAX" in type_name:
            model_type = "sarimax"
        elif "AutoReg" in type_name or "arx" in name.lower():
            model_type = "arx"
        else:
            model_type = "generic"
    elif ext == ".joblib":
        obj = _get_joblib().load(target_path)
        type_name = type(obj).__name__
        model_type = "xgb" if ("XGB" in type_name or "xgb" in name.lower()) else "generic"
    else:
        raise ValueError(f"Unsupported format {ext}")

    entry = {"type": model_type, "model": obj, "filename": os.path.basename(target_path)}
    _loaded_models[name] = entry
    logging.info(f"ForecastService: loaded {name} ({type_name}) → type={model_type}")
    return entry


def fetch_weather_dataframe(lat, lon, days=3):
    """
    Fetch weather from Open-Meteo and AQI_US from IQAir.
    Constructs a DataFrame with the 5 required model features:
      Solar_Radiation_Wm2, Outdoor_Temp_C, Wind_Speed_ms, Humidity_pct, AQI_US
    """
    url_w = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
        f"&hourly=shortwave_radiation,temperature_2m,"
        f"wind_speed_10m,relative_humidity_2m"
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

    # Fetch current AQI_US from IQAir (single value broadcast across all hours)
    iqair = IQAirService()
    aqi_us = iqair.get_aqi_us_with_fallback(lat, lon)
    aqi_list = [float(aqi_us)] * n

    df = pd.DataFrame({
        "time": pd.to_datetime(times),
        "Solar_Radiation_Wm2": np.array([(x if x is not None else 0.0) for x in dw.get("shortwave_radiation", [0.0]*n)], dtype=np.float64),
        "Outdoor_Temp_C":      np.array([(x if x is not None else 25.0) for x in dw.get("temperature_2m", [25.0]*n)], dtype=np.float64),
        "Wind_Speed_ms":       np.array([(x if x is not None else 2.0) for x in dw.get("wind_speed_10m", [2.0]*n)], dtype=np.float64),
        "Humidity_pct":        np.array([(x if x is not None else 50.0) for x in dw.get("relative_humidity_2m", [50.0]*n)], dtype=np.float64),
        "AQI_US":              np.array(aqi_list, dtype=np.float64),
    })

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

    elif m_type == "sarimax":
        # SARIMAX / ARIMAX: use get_forecast(steps, exog) for out-of-sample prediction.
        # The model was trained with exog_names matching WEATHER_FEATURES.
        model_obj = getattr(model, "model", model)
        exog_names = getattr(model_obj, "exog_names", None) or WEATHER_FEATURES
        # Keep only columns the model was trained with, in order
        available = [c for c in exog_names if c in df.columns]
        if len(available) != len(exog_names):
            missing = [c for c in exog_names if c not in df.columns]
            logging.warning(f"ForecastService SARIMAX: missing exog columns {missing}, padding 0")
            for c in missing:
                df[c] = 0.0
        exog = df[exog_names].values  # shape (n_steps, k_exog)
        n = len(df)
        fc = model.get_forecast(steps=n, exog=exog)
        raw_preds = fc.predicted_mean.values
        preds = np.where(df["Solar_Radiation_Wm2"].values <= 5.0, 0.0, np.maximum(0.0, raw_preds))
        return preds

    elif m_type == "arx":
        params = model.params
        param_names = list(params.index)

        # Derive structure from param names — works for any AutoReg fitted result.
        # statsmodels AutoReg param order: [const?] [DC.L1 … DC.Lk] [exog...]
        # We detect AR lags by looking for the ".L" lag pattern.
        has_const  = param_names[0] == "const"
        param_start = 1 if has_const else 0
        const      = params.iloc[0] if has_const else 0.0

        lag_names  = [n for n in param_names if ".L" in n]
        k_ar       = len(lag_names)
        ar_coefs   = params.iloc[param_start : param_start + k_ar].values
        exog_coefs = params.iloc[param_start + k_ar :].values

        # Build the exog matrix — use only the columns present in the model.
        # The param names after the AR lags are the exog column names.
        exog_col_names = param_names[param_start + k_ar:]
        # Validate all expected columns are in df; fall back gracefully if not
        available = [c for c in exog_col_names if c in df.columns]
        if len(available) != len(exog_col_names):
            missing = [c for c in exog_col_names if c not in df.columns]
            logging.warning(f"ForecastService ARX: missing exog columns {missing}, padding with 0")
            for c in missing:
                df[c] = 0.0
        exog = df[exog_col_names].values

        logging.debug(
            f"ForecastService ARX: k_ar={k_ar}, k_exog={len(exog_coefs)}, "
            f"exog_cols={exog_col_names}"
        )

        history = [0.0] * k_ar
        preds = []
        for i in range(len(df)):
            if df.loc[i, "Solar_Radiation_Wm2"] <= 5.0:
                val = 0.0
            else:
                ar_part   = sum(ar_coefs[j] * history[-1 - j] for j in range(k_ar))
                exog_part = np.dot(exog_coefs, exog[i]) if len(exog_coefs) > 0 else 0.0
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
