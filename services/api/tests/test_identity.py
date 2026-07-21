from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from pydantic import ValidationError

from editorial_core.authorization import Role
from youtuber_api import identity_crypto
from youtuber_api.identity_security import consume_second_factor, generate_recovery_codes, recovery_code_hash, role_from_oidc_claim
from youtuber_api.models import UserModel, UserRole
from youtuber_api.routers.oidc import _select_jwk
from youtuber_api.schemas import (
    BootstrapRequest,
    LoginRequest,
    OIDCConfigurationWrite,
    TOTPDisable,
    TOTPEnrollmentStart,
    UserCreate,
)


def test_local_account_passwords_require_at_least_eight_characters() -> None:
    BootstrapRequest(username="admin", display_name="Admin", password="12345678")
    LoginRequest(username="admin", password="12345678")
    TOTPEnrollmentStart(current_password="12345678")
    TOTPDisable(current_password="12345678", code="123456")
    UserCreate(username="viewer", display_name="Viewer", password="12345678", role=Role.VIEWER)

    invalid_payloads = (
        lambda: BootstrapRequest(username="admin", display_name="Admin", password="1234567"),
        lambda: LoginRequest(username="admin", password="1234567"),
        lambda: TOTPEnrollmentStart(current_password="1234567"),
        lambda: TOTPDisable(current_password="1234567", code="123456"),
        lambda: UserCreate(username="viewer", display_name="Viewer", password="1234567", role=Role.VIEWER),
    )
    for validate in invalid_payloads:
        with pytest.raises(ValidationError):
            validate()


def test_login_normalizes_username_without_modifying_password() -> None:
    request = LoginRequest(username="  GATE-ADMIN  ", password=" Olhaf.99 ")
    assert request.username == "gate-admin"
    assert request.password == " Olhaf.99 "


def test_recovery_codes_are_unique_and_normalized_before_hashing() -> None:
    codes = generate_recovery_codes()
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(code) == 17 and code[8] == "-" for code in codes)
    assert recovery_code_hash(codes[0]) == recovery_code_hash(codes[0].lower().replace("-", ""))


def test_oidc_role_mapping_is_explicit_and_handles_group_lists() -> None:
    mapping = {"studio-reviewers": "reviewer", "studio-admins": "admin"}
    assert role_from_oidc_claim(["unrelated", "studio-reviewers"], mapping, None) is Role.REVIEWER
    assert role_from_oidc_claim(["studio-reviewers", "studio-admins"], mapping, None) is Role.ADMIN
    assert role_from_oidc_claim("unrelated", mapping, "viewer") is Role.VIEWER
    with pytest.raises(ValueError, match="no explicitly mapped"):
        role_from_oidc_claim("unrelated", mapping, None)


def test_oidc_schema_requires_openid_and_a_role_policy() -> None:
    common = {
        "issuer": "https://identity.example.test",
        "client_id": "evidence-studio",
        "client_secret": "test-secret-value",
        "authorization_endpoint": "https://identity.example.test/authorize",
        "token_endpoint": "https://identity.example.test/token",
        "jwks_uri": "https://identity.example.test/jwks",
        "comment": "fixture configuration",
    }
    with pytest.raises(ValidationError, match="include openid"):
        OIDCConfigurationWrite(**common, scopes=["profile"], default_role=Role.VIEWER)
    with pytest.raises(ValidationError, match="role mapping"):
        OIDCConfigurationWrite(**common, scopes=["openid"], role_mapping={}, default_role=None)
    item = OIDCConfigurationWrite(**common, scopes=["openid"], role_mapping={"studio-viewers": Role.VIEWER})
    assert item.role_mapping["studio-viewers"] is Role.VIEWER


def test_totp_and_one_use_recovery_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(identity_crypto, "get_settings", lambda: type("Settings", (), {"identity_encryption_key": key})())
    secret = pyotp.random_base32()
    recovery = "A1B2C3D4-E5F60708"
    user = UserModel(
        username="totp-fixture", display_name="TOTP fixture", password_hash="fixture",
        identity_provider="local", oidc_subject=None, role=UserRole.VIEWER,
        totp_secret_encrypted=identity_crypto.encrypt_identity_secret(secret), totp_enabled=True,
        totp_recovery_hashes=[recovery_code_hash(recovery)], enabled=True, version=1,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc), deleted_at=None,
    )
    accepted, used_recovery = consume_second_factor(user, pyotp.TOTP(secret).now())
    assert accepted and not used_recovery
    accepted, used_recovery = consume_second_factor(user, recovery.lower())
    assert accepted and used_recovery and user.totp_recovery_hashes == []
    assert consume_second_factor(user, recovery) == (False, False)


def test_oidc_signed_id_token_fixture_selects_only_the_declared_key() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk["kid"] = "fixture-signing-key"
    now = datetime.now(timezone.utc)
    claims = {
        "iss": "https://identity.example.test", "aud": "evidence-studio", "sub": "fixture-subject",
        "iat": now, "exp": now + timedelta(minutes=5), "nonce": "fixture-nonce",
        "groups": ["studio-reviewers"],
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "fixture-signing-key"})
    key, algorithm = _select_jwk({"keys": [public_jwk]}, token)
    decoded = jwt.decode(token, key, algorithms=[algorithm], audience="evidence-studio", issuer="https://identity.example.test")
    assert decoded["sub"] == "fixture-subject"
    with pytest.raises(HTTPException) as caught:
        _select_jwk({"keys": [{**public_jwk, "kid": "different"}]}, token)
    assert "signing key" in caught.value.detail
