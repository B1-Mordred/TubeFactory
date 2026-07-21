from youtuber_api.audit import redact
from youtuber_api.security import hash_password, verify_password


def test_password_hash_is_argon2id_and_verifies() -> None:
    encoded = hash_password("correct horse battery staple")
    assert encoded.startswith("$argon2id$")
    assert verify_password(encoded, "correct horse battery staple")
    assert not verify_password(encoded, "wrong password")


def test_audit_context_redacts_nested_secret_fields() -> None:
    context = {
        "provider": "local",
        "credentials": {"api_token": "do-not-log", "endpoint": "http://model"},
        "items": [{"password": "do-not-log"}],
    }
    assert redact(context) == {
        "provider": "local",
        "credentials": "[REDACTED]",
        "items": [{"password": "[REDACTED]"}],
    }
