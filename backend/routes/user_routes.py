"""
routes/user_routes.py
=====================
User and organisation management — ADMIN only.
Also includes admin-level map and solar fleet statistics endpoints.

Blueprint prefix: /api/admin
"""

import logging
from flask import Blueprint, request, jsonify, g

from auth import require_role, hash_password
from platform_db import PlatformDatabase


admin_bp = Blueprint("admin", __name__, url_prefix="/api/admin")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_user(u: dict) -> dict:
    """Strip the password hash before returning a user to the client."""
    return {k: v for k, v in u.items() if k != "password_hash"}


# ===========================================================================
# Users
# ===========================================================================

# GET /api/admin/users
@admin_bp.route("/users", methods=["GET"])
@require_role("ADMIN")
def list_users():
    db = PlatformDatabase()
    users = db.list_users()
    return jsonify([_safe_user(u) for u in users])


# GET /api/admin/users/<id>
@admin_bp.route("/users/<int:user_id>", methods=["GET"])
@require_role("ADMIN")
def get_user(user_id: int):
    db = PlatformDatabase()
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify(_safe_user(user))


# PATCH /api/admin/users/<id>
@admin_bp.route("/users/<int:user_id>", methods=["PATCH"])
@require_role("ADMIN")
def update_user(user_id: int):
    """Update role, is_active, or organization_id."""
    db = PlatformDatabase()
    user = db.get_user_by_id(user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = {"role", "is_active", "organization_id"}
    updates = {k: v for k, v in data.items() if k in allowed}

    # Validate role
    if "role" in updates and updates["role"] not in ("ADMIN", "USER"):
        return jsonify({"error": "role must be 'ADMIN' or 'USER'"}), 400

    db.update_user(user_id, updates)
    logging.info(f"Admin: updated user {user_id}: {updates}")
    return jsonify({"message": "User updated"})


# DELETE /api/admin/users/<id>
@admin_bp.route("/users/<int:user_id>", methods=["DELETE"])
@require_role("ADMIN")
def delete_user(user_id: int):
    """Soft-delete: disable the user, keep their data."""
    if user_id == g.current_user["id"]:
        return jsonify({"error": "Cannot delete yourself"}), 400
    db = PlatformDatabase()
    if not db.get_user_by_id(user_id):
        return jsonify({"error": "User not found"}), 404
    db.update_user(user_id, {"is_active": 0})
    logging.info(f"Admin: disabled user {user_id}")
    return jsonify({"message": "User disabled"})


# POST /api/admin/users/<id>/reset-password
@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@require_role("ADMIN")
def reset_password(user_id: int):
    db = PlatformDatabase()
    if not db.get_user_by_id(user_id):
        return jsonify({"error": "User not found"}), 404

    data = request.get_json(silent=True) or {}
    new_password = data.get("password") or ""
    if len(new_password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400

    db.update_password(user_id, hash_password(new_password))
    logging.info(f"Admin: password reset for user {user_id}")
    return jsonify({"message": "Password updated"})


# ===========================================================================
# Organisations
# ===========================================================================

# GET /api/admin/organizations
@admin_bp.route("/organizations", methods=["GET"])
@require_role("ADMIN")
def list_organizations():
    db = PlatformDatabase()
    return jsonify(db.list_organizations())


# POST /api/admin/organizations
@admin_bp.route("/organizations", methods=["POST"])
@require_role("ADMIN")
def create_organization():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    db = PlatformDatabase()
    org_id = db.create_organization(
        name=name,
        address=data.get("address"),
        country=data.get("country", "Tunisia"),
    )
    logging.info(f"Admin: created organization '{name}' (id={org_id})")
    return jsonify({"message": "Organization created", "id": org_id}), 201


# GET /api/admin/organizations/<id>
@admin_bp.route("/organizations/<int:org_id>", methods=["GET"])
@require_role("ADMIN")
def get_organization(org_id: int):
    db = PlatformDatabase()
    org = db.get_organization(org_id)
    if not org:
        return jsonify({"error": "Organization not found"}), 404
    return jsonify(org)


# ===========================================================================
# Admin view of all installations
# ===========================================================================

# GET /api/admin/installations
@admin_bp.route("/installations", methods=["GET"])
@require_role("ADMIN")
def list_all_installations():
    """Return all installations across all users (fleet overview).
    
    Query params (all optional):
        governorate – filter by governorate name
        status      – filter by status (active|inactive|maintenance)
        org_id      – filter by organization_id
    """
    db = PlatformDatabase()
    installations = db.list_installations()

    # Optional filtering
    gov    = request.args.get("governorate")
    status = request.args.get("status")
    org_id = request.args.get("org_id")

    if gov:
        installations = [i for i in installations
                         if (i.get("governorate") or "").lower() == gov.lower()]
    if status:
        installations = [i for i in installations if i.get("status") == status]
    if org_id:
        try:
            oid = int(org_id)
            installations = [i for i in installations if i.get("organization_id") == oid]
        except ValueError:
            return jsonify({"error": "org_id must be an integer"}), 400

    return jsonify(installations)


# ===========================================================================
# Admin map — GeoJSON of entire fleet
# ===========================================================================

@admin_bp.route("/installations/map", methods=["GET"])
@require_role("ADMIN")
def get_admin_map():
    """
    GET /api/admin/installations/map
    Returns GeoJSON FeatureCollection of ALL installations with coordinates.
    
    Query params (all optional):
        governorate – filter by governorate
        status      – filter by status
        org_id      – filter by organization_id
    """
    db   = PlatformDatabase()
    rows = db.list_installations_with_coords(user_id=None)  # all installations

    # Optional filtering
    gov    = request.args.get("governorate")
    status = request.args.get("status")
    org_id = request.args.get("org_id")
    if gov:
        rows = [r for r in rows if (r.get("governorate") or "").lower() == gov.lower()]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    if org_id:
        try:
            oid = int(org_id)
            rows = [r for r in rows if r.get("organization_id") == oid]
        except ValueError:
            return jsonify({"error": "org_id must be an integer"}), 400

    features = [{
        "type": "Feature",
        "geometry": {
            "type": "Point",
            "coordinates": [r["longitude"], r["latitude"]],
        },
        "properties": {
            "id":                     r["id"],
            "name":                   r["name"],
            "owner_username":         r.get("owner_username"),
            "installed_capacity_kwp": r.get("installed_capacity_kwp"),
            "status":                 r.get("status", "active"),
            "city":                   r.get("city"),
            "governorate":            r.get("governorate"),
            "organization_id":        r.get("organization_id"),
        },
    } for r in rows]

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
    })


# ===========================================================================
# Solar fleet statistics
# ===========================================================================

@admin_bp.route("/solar-statistics", methods=["GET"])
@require_role("ADMIN")
def get_solar_statistics():
    """
    GET /api/admin/solar-statistics
    Returns fleet-wide aggregated statistics.
    """
    db   = PlatformDatabase()
    stats = db.get_solar_statistics()
    return jsonify(stats)


# ===========================================================================
# Admin: Assign / reassign an installation to a different user
# ===========================================================================

@admin_bp.route("/installations/<int:installation_id>/assign", methods=["PATCH"])
@require_role("ADMIN")
def assign_installation(installation_id: int):
    """
    PATCH /api/admin/installations/<id>/assign
    Reassign an installation to a different user.

    Body (JSON):
        user_id  – the target user's ID
    """
    db = PlatformDatabase()

    inst = db.get_installation(installation_id)
    if not inst:
        return jsonify({"error": "Installation not found"}), 404

    data = request.get_json(silent=True) or {}
    new_user_id = data.get("user_id")
    if not new_user_id:
        return jsonify({"error": "user_id is required"}), 400

    try:
        new_user_id = int(new_user_id)
    except (TypeError, ValueError):
        return jsonify({"error": "user_id must be an integer"}), 400

    target_user = db.get_user_by_id(new_user_id)
    if not target_user:
        return jsonify({"error": "Target user not found"}), 404
    if not target_user["is_active"]:
        return jsonify({"error": "Cannot assign to a disabled user"}), 400

    # Use execute directly to update owner_user_id (not in the allowed columns of update_installation)
    db.execute(
        "UPDATE installations SET owner_user_id = ? WHERE id = ?",
        (new_user_id, installation_id),
    )
    logging.info(
        f"Admin: installation {installation_id} reassigned "
        f"from user {inst['owner_user_id']} to user {new_user_id}"
    )
    return jsonify({
        "message": "Installation assigned successfully",
        "installation_id": installation_id,
        "new_owner_id": new_user_id,
        "new_owner_username": target_user["username"],
    })


# ===========================================================================
# Admin: Create a new user (admin-side, not self-registration)
# ===========================================================================

@admin_bp.route("/users", methods=["POST"])
@require_role("ADMIN")
def create_user():
    """
    POST /api/admin/users
    Create a new user account as admin.

    Body (JSON):
        username        – required
        email           – required
        password        – required, min 8 chars
        role            – USER | ADMIN  (default: USER)
        organization_id – optional
    """
    from auth import hash_password as hp

    data     = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    email    = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    role     = data.get("role", "USER")
    org_id   = data.get("organization_id")

    if not username or not email or not password:
        return jsonify({"error": "username, email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if "@" not in email:
        return jsonify({"error": "invalid email address"}), 400
    if role not in ("ADMIN", "USER"):
        return jsonify({"error": "role must be ADMIN or USER"}), 400

    db = PlatformDatabase()
    if db.get_user_by_username(username):
        return jsonify({"error": "Username already taken"}), 409
    if db.get_user_by_email(email):
        return jsonify({"error": "Email already registered"}), 409

    user_id = db.create_user(
        username=username,
        email=email,
        password_hash=hp(password),
        role=role,
        organization_id=org_id,
    )
    logging.info(f"Admin: created user '{username}' (id={user_id}, role={role})")
    return jsonify({"message": "User created", "user_id": user_id}), 201
