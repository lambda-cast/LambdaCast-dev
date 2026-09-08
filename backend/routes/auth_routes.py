"""
routes/auth_routes.py
=====================
Authentication endpoints: register, login, logout, me.

Blueprint prefix: /api/auth
"""

import logging
from flask import Blueprint, request, jsonify, make_response, g

from auth import (
    hash_password,
    verify_password,
    create_token,
    require_auth,
)
from platform_db import PlatformDatabase


auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


# ---------------------------------------------------------------------------
# POST /api/auth/register
# ---------------------------------------------------------------------------
@auth_bp.route("/register", methods=["POST"])
def register():
    """
    Register a new USER account.

    Body (JSON):
        username        – required, unique
        email           – required, unique
        password        – required, min 8 chars
        organization_id – optional
    """
    data = request.get_json(silent=True) or {}

    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    organization_id = data.get("organization_id")

    # Basic validation
    if not username or not email or not password:
        return jsonify({"error": "username, email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "password must be at least 8 characters"}), 400
    if "@" not in email:
        return jsonify({"error": "invalid email address"}), 400

    db = PlatformDatabase()

    if db.get_user_by_username(username):
        return jsonify({"error": "username already taken"}), 409
    if db.get_user_by_email(email):
        return jsonify({"error": "email already registered"}), 409

    # Validate org if supplied
    if organization_id is not None:
        if not db.get_organization(int(organization_id)):
            return jsonify({"error": "organization not found"}), 404

    user_id = db.create_user(
        username=username,
        email=email,
        password_hash=hash_password(password),
        role="USER",
        organization_id=organization_id,
    )

    logging.info(f"Auth: new user registered: {username} (id={user_id})")
    return jsonify({"message": "Account created", "user_id": user_id}), 201


# ---------------------------------------------------------------------------
# POST /api/auth/login
# ---------------------------------------------------------------------------
@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Authenticate and return a JWT.

    Body (JSON):
        username  – username or email
        password
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get("username") or "").strip()
    password = data.get("password") or ""

    if not identifier or not password:
        return jsonify({"error": "username and password are required"}), 400

    db = PlatformDatabase()

    # Accept username OR email
    user = db.get_user_by_username(identifier)
    if not user:
        user = db.get_user_by_email(identifier.lower())

    if not user or not verify_password(password, user["password_hash"]):
        return jsonify({"error": "Invalid credentials"}), 401

    if not user["is_active"]:
        return jsonify({"error": "Account is disabled"}), 403

    db.update_last_login(user["id"])

    token = create_token(user_id=user["id"], role=user["role"])

    # Return token in JSON body AND set it as an HttpOnly cookie for browser use
    resp = make_response(
        jsonify(
            {
                "access_token": token,
                "token_type": "Bearer",
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "email": user["email"],
                    "role": user["role"],
                },
            }
        )
    )
    resp.set_cookie(
        "access_token",
        token,
        httponly=True,
        samesite="Lax",
        max_age=86400,  # 24 h
    )
    logging.info(f"Auth: user logged in: {user['username']}")
    return resp


# ---------------------------------------------------------------------------
# POST /api/auth/logout
# ---------------------------------------------------------------------------
@auth_bp.route("/logout", methods=["POST"])
def logout():
    """Clear the auth cookie."""
    resp = make_response(jsonify({"message": "Logged out"}))
    resp.delete_cookie("access_token")
    return resp


# ---------------------------------------------------------------------------
# GET /api/auth/me
# ---------------------------------------------------------------------------
@auth_bp.route("/me", methods=["GET"])
@require_auth
def me():
    """Return the current user's profile."""
    u = g.current_user
    return jsonify(
        {
            "id": u["id"],
            "username": u["username"],
            "email": u["email"],
            "role": u["role"],
            "organization_id": u["organization_id"],
            "created_at": u["created_at"],
            "last_login": u["last_login"],
        }
    )
