import logging

from cozy.db.abs_server import AudiobookshelfServer

log = logging.getLogger("secrets")

_SCHEMA_NAME = "com.github.geigi.cozy.Audiobookshelf"
_ATTRIBUTE_KEY = "server_id"

_libsecret = None


def _get_libsecret():
    global _libsecret

    if _libsecret is False:
        return None

    if _libsecret is not None:
        return _libsecret

    try:
        import gi

        gi.require_version("Secret", "1")
        from gi.repository import Secret

        schema = Secret.Schema.new(
            _SCHEMA_NAME,
            Secret.SchemaFlags.NONE,
            {_ATTRIBUTE_KEY: Secret.SchemaAttributeType.STRING},
        )
        _libsecret = (Secret, schema)
    except (ImportError, ValueError) as e:
        log.warning("libsecret not available, falling back to database storage: %s", e)
        _libsecret = False
        return None

    return _libsecret


def store_server_token(server_id: int, token: str) -> None:
    (
        _store_with_libsecret(server_id, token)
        if _get_libsecret()
        else _store_in_database(server_id, token)
    )


def get_server_token(server_id: int) -> str | None:
    if _get_libsecret():
        token = _get_from_libsecret(server_id)
        if token is not None:
            return token

    return _get_from_database(server_id)


def clear_server_token(server_id: int) -> None:
    if _get_libsecret():
        _clear_with_libsecret(server_id)

    _clear_in_database(server_id)


def _store_with_libsecret(server_id: int, token: str) -> None:
    Secret, schema = _get_libsecret()
    attributes = {_ATTRIBUTE_KEY: str(server_id)}

    try:
        Secret.password_store_sync(
            schema,
            attributes,
            Secret.COLLECTION_DEFAULT,
            "Audiobookshelf server token",
            token,
            None,
        )
    except Exception as e:
        log.warning("Storing token in libsecret failed, using database storage: %s", e)
        _store_in_database(server_id, token)


def _get_from_libsecret(server_id: int) -> str | None:
    Secret, schema = _get_libsecret()
    attributes = {_ATTRIBUTE_KEY: str(server_id)}

    try:
        return Secret.password_lookup_sync(schema, attributes, None)
    except Exception as e:
        log.warning("Reading token from libsecret failed: %s", e)
        return None


def _clear_with_libsecret(server_id: int) -> None:
    Secret, schema = _get_libsecret()
    attributes = {_ATTRIBUTE_KEY: str(server_id)}

    try:
        Secret.password_clear_sync(schema, attributes, None)
    except Exception as e:
        log.warning("Clearing token in libsecret failed: %s", e)


def _store_in_database(server_id: int, token: str) -> None:
    server = AudiobookshelfServer.get_or_none(AudiobookshelfServer.id == server_id)
    if server is None:
        return

    server.token = token
    server.save(only=[AudiobookshelfServer.token])


def _get_from_database(server_id: int) -> str | None:
    server = AudiobookshelfServer.get_or_none(AudiobookshelfServer.id == server_id)
    return server.token if server is not None else None


def _clear_in_database(server_id: int) -> None:
    server = AudiobookshelfServer.get_or_none(AudiobookshelfServer.id == server_id)
    if server is None:
        return

    server.token = None
    server.save(only=[AudiobookshelfServer.token])
