from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfServer
from cozy.server.playback import resolve_playback_uri


def test_resolve_playback_uri_appends_token(peewee_database):
    server = AudiobookshelfServer.create(
        name="abs", url="http://abs.local:13378", library_id="lib_1"
    )
    secrets.store_server_token(server.id, "token123")

    uri = resolve_playback_uri("http://abs.local:13378/s/item/li_1/01.mp3")

    assert uri == "http://abs.local:13378/s/item/li_1/01.mp3?token=token123"


def test_resolve_playback_uri_keeps_local_path(peewee_database):
    uri = resolve_playback_uri("/home/user/books/book.mp3")

    assert uri == "/home/user/books/book.mp3"


def test_resolve_playback_uri_returns_path_when_no_token(peewee_database):
    AudiobookshelfServer.create(name="abs", url="http://abs.local:13378", library_id="lib_1")

    uri = resolve_playback_uri("http://abs.local:13378/s/item/li_1/01.mp3")

    assert uri == "http://abs.local:13378/s/item/li_1/01.mp3"


def test_resolve_playback_uri_returns_path_when_no_server_matches(peewee_database):
    uri = resolve_playback_uri("http://other.local:13378/s/item/li_1/01.mp3")

    assert uri == "http://other.local:13378/s/item/li_1/01.mp3"
