import pytest

from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfServer


def test_secrets_store_and_get(peewee_database):
    server = AudiobookshelfServer.create(
        name="abs", url="http://abs.local:13378", library_id="lib_1"
    )

    secrets.store_server_token(server.id, "token123")

    assert secrets.get_server_token(server.id) == "token123"


def test_secrets_clear(peewee_database):
    server = AudiobookshelfServer.create(
        name="abs", url="http://abs.local:13378", library_id="lib_1"
    )
    secrets.store_server_token(server.id, "token123")

    secrets.clear_server_token(server.id)

    assert secrets.get_server_token(server.id) is None


def test_secrets_get_missing_server_returns_none(peewee_database):
    assert secrets.get_server_token(9999) is None


def _secret_service_available() -> bool:
    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret
    except (ImportError, ValueError):
        return False

    schema = Secret.Schema.new(
        "com.github.geigi.cozy.TestSecrets",
        Secret.SchemaFlags.NONE,
        {"probe": Secret.SchemaAttributeType.STRING},
    )
    attrs = {"probe": "availability"}
    try:
        stored = Secret.password_store_sync(
            schema, attrs, Secret.COLLECTION_DEFAULT, "cozy probe", "1", None
        )
    except Exception:
        return False

    if not stored:
        return False

    Secret.password_clear_sync(schema, attrs, None)
    return True


@pytest.mark.skipif(not _secret_service_available(), reason="no secret service available")
def test_secrets_libsecret_roundtrip(peewee_database, monkeypatch):
    server = AudiobookshelfServer.create(
        name="abs", url="http://abs.local:13378", library_id="lib_1"
    )
    secrets._libsecret = None
    monkeypatch.setattr(secrets, "_get_libsecret", secrets._original_get_libsecret)

    secrets.store_server_token(server.id, "token123")
    assert secrets.get_server_token(server.id) == "token123"

    secrets.clear_server_token(server.id)
    assert secrets.get_server_token(server.id) is None
