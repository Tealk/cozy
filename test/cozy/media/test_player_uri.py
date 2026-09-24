from unittest.mock import MagicMock
from urllib.parse import quote

import pytest

from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfServer
from cozy.media.player import GstPlayer


@pytest.fixture
def bare_player(peewee_database):
    player = object.__new__(GstPlayer)
    player._player = MagicMock()
    return player


def test_load_file_builds_file_uri(bare_player, tmp_path):
    path = tmp_path / "book.mp3"
    path.write_bytes(b"data")

    bare_player.load_file(str(path))

    bare_player._player.set_property.assert_any_call("uri", "file://" + quote(str(path)))


def test_load_file_raises_for_missing_local_file(bare_player):
    with pytest.raises(FileNotFoundError):
        bare_player.load_file("/nonexistent/book.mp3")


def test_load_file_resolves_remote_uri_with_token(bare_player):
    server = AudiobookshelfServer.create(
        name="abs", url="http://abs.local:13378", library_id="lib_1"
    )
    secrets.store_server_token(server.id, "token123")

    bare_player.load_file("http://abs.local:13378/s/item/li_1/01.mp3")

    expected = "http://abs.local:13378/s/item/li_1/01.mp3?token=token123"
    bare_player._player.set_property.assert_any_call("uri", expected)
