"""
test_auth.py
============
Tests for auth.py: password hashing, JWT creation/decoding, expiry.
No Flask context or database required.
"""

import time
import pytest

import auth as auth_mod
from auth import (
    hash_password,
    verify_password,
    create_token,
    decode_token,
    AuthError,
)


# ──────────────────────────────────────────────────────────────────────────────
# Password helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestPasswords:

    def test_hash_is_not_plaintext(self):
        h = hash_password("secret123")
        assert h != "secret123"

    def test_verify_correct_password(self):
        h = hash_password("mypassword")
        assert verify_password("mypassword", h) is True

    def test_verify_wrong_password(self):
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_hashes_are_unique(self):
        """Two hashes of the same password should differ (salted)."""
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # different salts


# ──────────────────────────────────────────────────────────────────────────────
# JWT helpers
# ──────────────────────────────────────────────────────────────────────────────

class TestJWT:

    def test_create_and_decode_round_trip(self):
        token = create_token(user_id=42, role="USER")
        payload = decode_token(token)
        assert payload["sub"] == 42
        assert payload["role"] == "USER"

    def test_admin_role_preserved(self):
        token = create_token(user_id=1, role="ADMIN")
        payload = decode_token(token)
        assert payload["role"] == "ADMIN"

    def test_malformed_token_raises(self):
        with pytest.raises(AuthError):
            decode_token("not.a.token")

    def test_tampered_signature_raises(self):
        token = create_token(user_id=1, role="USER")
        parts = token.split(".")
        # Flip a character in the signature
        tampered_sig = parts[2][:-1] + ("A" if parts[2][-1] != "A" else "B")
        tampered = f"{parts[0]}.{parts[1]}.{tampered_sig}"
        with pytest.raises(AuthError):
            decode_token(tampered)

    def test_expired_token_raises(self, monkeypatch):
        # Set token lifetime to 0 (immediately expired)
        monkeypatch.setattr(auth_mod, "TOKEN_LIFETIME_SECONDS", 0)
        token = create_token(user_id=5, role="USER")
        # Even a tiny sleep isn't needed — exp == iat means already expired
        with pytest.raises(AuthError, match="expired"):
            decode_token(token)

    def test_token_has_exp_field(self):
        token = create_token(user_id=1, role="USER")
        payload = decode_token(token)
        assert "exp" in payload
        assert payload["exp"] > int(time.time())

    def test_empty_string_raises(self):
        with pytest.raises(AuthError):
            decode_token("")
