"""
test_platform_db.py
===================
Tests for platform_db.PlatformDatabase — users, organisations, installations.
All tests use a temporary in-memory/temp-file database so they don't touch
the real data/platform.db.
"""

import os
import tempfile
import pytest

from platform_db import PlatformDatabase


@pytest.fixture
def db(tmp_path):
    """Return a fresh PlatformDatabase backed by a temp file."""
    db_path = str(tmp_path / "test_platform.db")
    return PlatformDatabase(db_path=db_path)


# ──────────────────────────────────────────────────────────────────────────────
# Organization tests
# ──────────────────────────────────────────────────────────────────────────────

class TestOrganizations:

    def test_create_and_get(self, db):
        org_id = db.create_organization(name="SolarCo", address="Tunis", country="Tunisia")
        assert org_id == 1

        org = db.get_organization(1)
        assert org is not None
        assert org["name"] == "SolarCo"
        assert org["country"] == "Tunisia"

    def test_list_is_empty_initially(self, db):
        assert db.list_organizations() == []

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_organization(999) is None


# ──────────────────────────────────────────────────────────────────────────────
# User tests
# ──────────────────────────────────────────────────────────────────────────────

class TestUsers:

    def _create_user(self, db, username="alice", role="USER"):
        return db.create_user(
            username=username,
            email=f"{username}@example.com",
            password_hash="hashed",
            role=role,
        )

    def test_create_and_get_by_id(self, db):
        uid = self._create_user(db, "alice")
        user = db.get_user_by_id(uid)
        assert user is not None
        assert user["username"] == "alice"
        assert user["role"] == "USER"
        assert user["is_active"] == 1

    def test_get_by_username(self, db):
        self._create_user(db, "bob")
        user = db.get_user_by_username("bob")
        assert user is not None
        assert user["email"] == "bob@example.com"

    def test_get_by_email(self, db):
        self._create_user(db, "carol")
        user = db.get_user_by_email("carol@example.com")
        assert user is not None

    def test_list_users(self, db):
        self._create_user(db, "user1")
        self._create_user(db, "user2")
        users = db.list_users()
        assert len(users) == 2

    def test_update_user_role(self, db):
        uid = self._create_user(db, "dan", role="USER")
        db.update_user(uid, {"role": "ADMIN"})
        user = db.get_user_by_id(uid)
        assert user["role"] == "ADMIN"

    def test_update_user_ignores_password_hash(self, db):
        """update_user should not allow changing password_hash directly."""
        uid = self._create_user(db, "eve")
        original_hash = db.get_user_by_id(uid)["password_hash"]
        db.update_user(uid, {"password_hash": "HACKED"})
        user = db.get_user_by_id(uid)
        assert user["password_hash"] == original_hash

    def test_update_password(self, db):
        uid = self._create_user(db, "frank")
        db.update_password(uid, "new_hash")
        user = db.get_user_by_id(uid)
        assert user["password_hash"] == "new_hash"

    def test_disable_user(self, db):
        uid = self._create_user(db, "grace")
        db.update_user(uid, {"is_active": 0})
        user = db.get_user_by_id(uid)
        assert user["is_active"] == 0

    def test_delete_user(self, db):
        uid = self._create_user(db, "henry")
        db.delete_user(uid)
        assert db.get_user_by_id(uid) is None

    def test_nonexistent_user_returns_none(self, db):
        assert db.get_user_by_id(9999) is None
        assert db.get_user_by_username("nobody") is None

    def test_user_with_organization(self, db):
        org_id = db.create_organization("TechOrg")
        uid = db.create_user(
            username="ida",
            email="ida@example.com",
            password_hash="hashed",
            role="USER",
            organization_id=org_id,
        )
        user = db.get_user_by_id(uid)
        assert user["organization_id"] == org_id


# ──────────────────────────────────────────────────────────────────────────────
# Installation tests
# ──────────────────────────────────────────────────────────────────────────────

class TestInstallations:

    @pytest.fixture
    def user_id(self, db):
        return db.create_user(
            username="installer",
            email="installer@example.com",
            password_hash="hashed",
            role="USER",
        )

    def _minimal_installation(self, user_id):
        return {
            "name": "Test Site",
            "owner_user_id": user_id,
            "installed_capacity_kwp": 10.5,
        }

    def test_create_and_get(self, db, user_id):
        inst_id = db.create_installation(self._minimal_installation(user_id))
        inst = db.get_installation(inst_id)
        assert inst is not None
        assert inst["name"] == "Test Site"
        assert inst["installed_capacity_kwp"] == pytest.approx(10.5)
        assert inst["status"] == "active"  # default value

    def test_full_installation(self, db, user_id):
        data = {
            "name": "Rooftop A",
            "owner_user_id": user_id,
            "latitude": 36.8190,
            "longitude": 10.1660,
            "city": "Tunis",
            "region": "Tunis",
            "country": "Tunisia",
            "installed_capacity_kwp": 15.0,
            "panel_manufacturer": "JinkoSolar",
            "panel_model": "Tiger Neo",
            "panel_technology": "monocrystalline",
            "panel_power_wp": 415.0,
            "number_of_panels": 36,
            "tilt": 30.0,
            "azimuth": 180.0,
            "inverter_manufacturer": "Huawei",
            "inverter_model": "SUN2000",
            "inverter_capacity_kw": 15.0,
            "installation_date": "2023-06-01",
            "status": "active",
        }
        inst_id = db.create_installation(data)
        inst = db.get_installation(inst_id)
        assert inst["latitude"]  == pytest.approx(36.8190)
        assert inst["longitude"] == pytest.approx(10.1660)
        assert inst["panel_manufacturer"] == "JinkoSolar"
        assert inst["number_of_panels"] == 36
        assert inst["tilt"] == pytest.approx(30.0)

    def test_list_installations_for_user(self, db, user_id):
        db.create_installation({**self._minimal_installation(user_id), "name": "Site 1"})
        db.create_installation({**self._minimal_installation(user_id), "name": "Site 2"})
        results = db.list_installations_for_user(user_id)
        assert len(results) == 2
        names = {r["name"] for r in results}
        assert names == {"Site 1", "Site 2"}

    def test_list_all_installations(self, db):
        uid1 = db.create_user("ua", "ua@x.com", "h", "USER")
        uid2 = db.create_user("ub", "ub@x.com", "h", "USER")
        db.create_installation({"name": "A", "owner_user_id": uid1})
        db.create_installation({"name": "B", "owner_user_id": uid2})
        all_insts = db.list_installations()
        assert len(all_insts) == 2

    def test_get_installation_for_user_ownership(self, db):
        uid1 = db.create_user("ux", "ux@x.com", "h", "USER")
        uid2 = db.create_user("uy", "uy@x.com", "h", "USER")
        inst_id = db.create_installation({"name": "Mine", "owner_user_id": uid1})
        # Owner can access
        assert db.get_installation_for_user(inst_id, uid1) is not None
        # Other user cannot
        assert db.get_installation_for_user(inst_id, uid2) is None

    def test_update_installation(self, db, user_id):
        inst_id = db.create_installation(self._minimal_installation(user_id))
        db.update_installation(inst_id, {"status": "maintenance", "city": "Sfax"})
        inst = db.get_installation(inst_id)
        assert inst["status"] == "maintenance"
        assert inst["city"] == "Sfax"

    def test_update_installation_ignores_unknown_fields(self, db, user_id):
        """Unknown fields should be silently ignored."""
        inst_id = db.create_installation(self._minimal_installation(user_id))
        db.update_installation(inst_id, {"hacked_field": "evil", "name": "Updated"})
        inst = db.get_installation(inst_id)
        assert inst["name"] == "Updated"

    def test_delete_installation(self, db, user_id):
        inst_id = db.create_installation(self._minimal_installation(user_id))
        db.delete_installation(inst_id)
        assert db.get_installation(inst_id) is None

    def test_get_nonexistent_installation(self, db):
        assert db.get_installation(9999) is None
