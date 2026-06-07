"""Unit tests for the at-rest encryption helpers (secrets_at_rest.encrypt_text /
decrypt_text). conftest.py sets a real (non-default) JWT_SECRET, so encryption
is active here — mirroring production behaviour."""

import json

from app.services.secrets_at_rest import (
    decrypt_text,
    encrypt_text,
    encryption_available,
)


def test_encryption_active_in_tests():
    # conftest sets JWT_SECRET to a non-default value → encryption is on.
    assert encryption_available() is True


def test_roundtrip_string():
    ct = encrypt_text("super-secret-token")
    assert ct != "super-secret-token"
    assert ct.startswith("gAAAAA")          # Fernet token prefix
    assert decrypt_text(ct) == "super-secret-token"


def test_none_passthrough():
    assert encrypt_text(None) is None
    assert decrypt_text(None) is None


def test_legacy_plaintext_still_readable():
    # Rows written before encryption was introduced are bare strings; they must
    # keep decrypting (backward compatibility) rather than erroring.
    assert decrypt_text("plain-oauth-token") == "plain-oauth-token"
    assert decrypt_text('{"meeting":"data"}') == '{"meeting":"data"}'


def test_snapshot_blob_roundtrip():
    # The BotSnapshot.data path encrypts a JSON blob of transcript + analysis.
    blob = json.dumps({"transcript": [{"text": "hello"}], "analysis": {"summary": "s"}})
    enc = encrypt_text(blob)
    assert enc.startswith("gAAAAA")
    assert json.loads(decrypt_text(enc)) == json.loads(blob)


def test_tampered_ciphertext_fails_closed():
    ct = encrypt_text("value")
    tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    # No configured key can decrypt a corrupted token → returns None (fail-closed),
    # never the raw ciphertext.
    assert decrypt_text(tampered) is None
