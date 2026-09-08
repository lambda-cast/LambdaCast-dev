"""
auth.py
=======
Authentication and authorisation helpers.

Design decisions
----------------
- Stateless JWT tokens (no server-side session storage).
- Tokens are signed with HMAC-SHA256 using a secret from the config file or
  the environment variable SUNALYZER_SECRET_KEY.
- Passwords are hashed with PBKDF2-HMAC-SHA256 via werkzeug.security
  (bundled with Flask – no extra dependency).
- Two roles: ADMIN and USER (extensible).
- Token lifetime is 24 hours by default (configurable via config.yml).

Usage
-----
    # Hash a password at registration
    h = hash_password("my-password")

    # Verify at login
    ok = verify_password("my-password", h)

    # Issue a token
    token = create_token(user_id=1, role="USER")

    # Validate a token (returns payload dict or raises AuthError)
    payload = decode_token(token)

    # Flask decorator – protects a route, injects current_user into g
    @require_auth
    def my_endpoint(): ...

    # Flask decorator – additionally requires ADMIN role
    @require_role("ADMIN")
    def admin_endpoint(): ...
"""

import os
import time
import hmac
import hashlib
import base64
import json
import logging
from functools import wraps

from flask import request, g, jsonify
from werkzeug.security import generate_password_hash, check_password_hash

from platform_db import PlatformDatabase


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Secret key used to sign JWT tokens.
# Override via env var in production; fallback for development only.
_DEFAULT_SECRET = "sunalyzer-change-me-in-production"
_SECRET_KEY: bytes = os.environ.get("SUNALYZER_SECRET_KEY", _DEFAULT_SECRET).encode()

TOKEN_LIFETIME_SECONDS = int(os.environ.get("TOKEN_LIFETIME_S", 86400))  # 24 h


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    """Return a salted PBKDF2 hash of the given password."""
    return generate_password_hash(plain, method="pbkdf2:sha256:260000")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if *plain* matches *hashed*."""
    return check_password_hash(hashed, plain)


# ---------------------------------------------------------------------------
# Minimal JWT implementation (header.payload.signature, HS256)
# No third-party JWT library needed — keeps the dependency list lean.
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.urlsafe_b64decode(s)


def create_token(user_id: int, role: str) -> str:
    """Issue a signed JWT for the given user."""
    header = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(
        json.dumps(
            {
                "sub": user_id,
                "role": role,
                "iat": int(time.time()),
                "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
            }
        ).encode()
    )
    sig_input = f"{header}.{payload}".encode()
    signature = _b64url_encode(
        hmac.new(_SECRET_KEY, sig_input, hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> dict:
    """
    Validate and decode a JWT.
    Raises AuthError on any problem (expired, tampered, malformed).
    """
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except ValueError:
        raise AuthError("Malformed token")

    # Verify signature
    expected_sig = _b64url_encode(
        hmac.new(
            _SECRET_KEY,
            f"{header_b64}.{payload_b64}".encode(),
            hashlib.sha256,
        ).digest()
    )
    if not hmac.compare_digest(expected_sig, sig_b64):
        raise AuthError("Invalid token signature")

    # Decode payload
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        raise AuthError("Could not decode token payload")

    # Check expiry (use <= so a zero-lifetime token is immediately invalid)
    if payload.get("exp", 0) <= int(time.time()):
        raise AuthError("Token has expired")

    return payload


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Raised when authentication or authorisation fails."""


# ---------------------------------------------------------------------------
# Flask decorators
# ---------------------------------------------------------------------------

def _extract_token() -> str | None:
    """Pull the Bearer token from the Authorization header or cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    # Also accept token in a cookie for browser pages
    return request.cookies.get("access_token")


def require_auth(f):
    """
    Decorator that validates the JWT and populates g.current_user with the
    full user dict from the database.  Returns 401 on failure.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        token = _extract_token()
        if not token:
            return jsonify({"error": "Authentication required"}), 401
        try:
            payload = decode_token(token)
        except AuthError as e:
            return jsonify({"error": str(e)}), 401

        db = PlatformDatabase()
        user = db.get_user_by_id(payload["sub"])
        if not user or not user["is_active"]:
            return jsonify({"error": "User not found or disabled"}), 401

        g.current_user = user
        return f(*args, **kwargs)

    return decorated


def require_role(role: str):
    """
    Decorator factory.  Usage::

        @require_role("ADMIN")
        def admin_only(): ...

    Must be applied **after** @require_auth so that g.current_user is set.
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            token = _extract_token()
            if not token:
                return jsonify({"error": "Authentication required"}), 401
            try:
                payload = decode_token(token)
            except AuthError as e:
                return jsonify({"error": str(e)}), 401

            db = PlatformDatabase()
            user = db.get_user_by_id(payload["sub"])
            if not user or not user["is_active"]:
                return jsonify({"error": "User not found or disabled"}), 401

            if user["role"] != role:
                return jsonify({"error": f"Role '{role}' required"}), 403

            g.current_user = user
            return f(*args, **kwargs)

        return decorated
    return decorator
