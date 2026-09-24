"""
routes/forecast_routes.py
=========================
Admin-only endpoints for managing ML forecast models.

Blueprint prefix: /api/admin/forecast

Endpoints
---------
GET    /api/admin/forecast/models              – list all models in models/ folder
POST   /api/admin/forecast/models              – upload a new model file (.pkl / .joblib)
DELETE /api/admin/forecast/models/<model_id>   – delete a model file by ID
"""

import os
import logging
from flask import Blueprint, request, jsonify, g

from auth import require_auth, require_role

forecast_bp = Blueprint("forecast", __name__, url_prefix="/api/admin/forecast")

# Resolve the models directory relative to this file's location:
# backend/routes/forecast_routes.py  →  ../../models/
_MODELS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "models")
)
_ALLOWED_EXTENSIONS = {".pkl", ".joblib"}
_MAX_FILE_SIZE_MB = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _model_id(filename: str) -> str:
    """Strip extension to get the model ID used by forecast_service."""
    return os.path.splitext(filename)[0]


def _infer_type(model_id: str) -> str:
    if "xgb" in model_id.lower():
        return "xgb"
    if "arx" in model_id.lower():
        return "arx"
    return "generic"


def _build_label(model_id: str, m_type: str) -> str:
    if m_type == "xgb":
        return f"XGBoost ({model_id})"
    if m_type == "arx":
        return f"ARX ({model_id})"
    return f"Modèle ({model_id})"


def _list_models() -> list:
    """Scan models/ directory and return metadata for each model file."""
    if not os.path.isdir(_MODELS_DIR):
        return []
    results = []
    for fname in sorted(os.listdir(_MODELS_DIR)):
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _ALLOWED_EXTENSIONS:
            continue
        full_path = os.path.join(_MODELS_DIR, fname)
        mid = _model_id(fname)
        mtype = _infer_type(mid)
        stat = os.stat(full_path)
        results.append({
            "id":         mid,
            "filename":   fname,
            "label":      _build_label(mid, mtype),
            "type":       mtype,
            "size_kb":    round(stat.st_size / 1024, 1),
            "uploaded_at": None,   # filesystem mtime as ISO string
        })
        # Enrich with mtime
        import datetime
        results[-1]["uploaded_at"] = datetime.datetime.fromtimestamp(
            stat.st_mtime
        ).strftime("%Y-%m-%d %H:%M")
    return results


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@forecast_bp.route("/models", methods=["GET"])
@require_role("ADMIN")
def list_models():
    """
    GET /api/admin/forecast/models
    Returns all model files found in models/ directory.
    """
    return jsonify({"models": _list_models()})


@forecast_bp.route("/models", methods=["POST"])
@require_role("ADMIN")
def upload_model():
    """
    POST /api/admin/forecast/models
    Upload a new forecast model file.

    Expects multipart/form-data with field 'model_file'.
    Optional field 'model_name' overrides the filename stem.
    """
    if "model_file" not in request.files:
        return jsonify({"error": "No model_file in request"}), 400

    f = request.files["model_file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in _ALLOWED_EXTENSIONS:
        return jsonify({
            "error": f"Unsupported file type '{ext}'. Only .pkl and .joblib are allowed."
        }), 400

    # Optional override name
    custom_name = (request.form.get("model_name") or "").strip()
    if custom_name:
        # Sanitise: keep only alphanumeric, underscores, hyphens
        import re
        custom_name = re.sub(r"[^\w\-]", "_", custom_name)
        dest_filename = custom_name + ext
    else:
        # Sanitise original filename
        import re
        safe_stem = re.sub(r"[^\w\-]", "_", os.path.splitext(f.filename)[0])
        dest_filename = safe_stem + ext

    dest_path = os.path.join(_MODELS_DIR, dest_filename)

    # Refuse to overwrite without explicit flag
    if os.path.exists(dest_path):
        overwrite = request.form.get("overwrite", "false").lower() in ("1", "true", "yes")
        if not overwrite:
            return jsonify({
                "error": f"Model '{dest_filename}' already exists. "
                         f"Send overwrite=true to replace it."
            }), 409

    # Save the file
    os.makedirs(_MODELS_DIR, exist_ok=True)
    f.save(dest_path)

    # Evict the old loaded-model cache entry if any (so next forecast picks up new file)
    try:
        import forecast_service as fs
        fs._loaded_models.pop(_model_id(dest_filename), None)
    except Exception:
        pass

    mid = _model_id(dest_filename)
    mtype = _infer_type(mid)
    size_kb = round(os.path.getsize(dest_path) / 1024, 1)
    logging.info(
        f"ForecastAdmin: user {g.current_user['id']} uploaded model "
        f"'{dest_filename}' ({size_kb} KB)"
    )
    return jsonify({
        "message":   "Model uploaded successfully",
        "id":        mid,
        "filename":  dest_filename,
        "label":     _build_label(mid, mtype),
        "type":      mtype,
        "size_kb":   size_kb,
    }), 201


@forecast_bp.route("/models/<path:model_id>", methods=["DELETE"])
@require_role("ADMIN")
def delete_model(model_id: str):
    """
    DELETE /api/admin/forecast/models/<model_id>
    Deletes a model file from the models/ directory.
    model_id is the filename stem (no extension).
    """
    # Find the file — try both extensions
    target_path = None
    for ext in _ALLOWED_EXTENSIONS:
        candidate = os.path.join(_MODELS_DIR, model_id + ext)
        if os.path.isfile(candidate):
            target_path = candidate
            break

    if not target_path:
        return jsonify({"error": f"Model '{model_id}' not found"}), 404

    # Safety: stay inside models/ directory (path traversal guard)
    real_target = os.path.realpath(target_path)
    real_models = os.path.realpath(_MODELS_DIR)
    if not real_target.startswith(real_models + os.sep):
        return jsonify({"error": "Invalid model path"}), 400

    os.remove(target_path)

    # Evict from in-memory cache
    try:
        import forecast_service as fs
        fs._loaded_models.pop(model_id, None)
    except Exception:
        pass

    logging.info(
        f"ForecastAdmin: user {g.current_user['id']} deleted model '{model_id}'"
    )
    return jsonify({"message": f"Model '{model_id}' deleted"})
