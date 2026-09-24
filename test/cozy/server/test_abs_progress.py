from types import SimpleNamespace

from cozy.server.abs_progress import progress_in_seconds


def test_progress_in_seconds_converts_nanoseconds():
    book = SimpleNamespace(progress=250_000_000_000, duration=600_000_000_000)

    current_time, duration = progress_in_seconds(book)

    assert current_time == 250.0
    assert duration == 600.0


def test_progress_in_seconds_clamps_to_duration():
    book = SimpleNamespace(progress=700_000_000_000, duration=600_000_000_000)

    current_time, duration = progress_in_seconds(book)

    assert current_time == 600.0
    assert duration == 600.0


def _file():
    from cozy.db.file import File

    return File.create(path="/tmp/book.mp3", modified=0)


def test_push_book_progress_can_run_synchronously(peewee_database, monkeypatch):
    from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
    from cozy.db.book import Book
    from cozy.db.track import Track
    from cozy.db.track_to_file import TrackToFile
    from cozy.db.model_base import get_sqlite_database
    from cozy.model.book import Book as ModelBook
    from cozy.server import abs_progress

    db_book = Book.create(name="Book", author="A", reader="R", position=0, rating=-1)
    track = Track.create(name="01", number=1, disk=1, position=0, book=db_book, length=600)
    TrackToFile.create(track=track, file=_file(), start_at=0)
    server = AudiobookshelfServer.create(name="abs", url="http://abs.local", library_id="lib_1")
    mapping = AudiobookshelfBook.create(
        server=server.id, book=db_book, library_item_id="li_1", library_id="lib_1"
    )

    book = ModelBook(get_sqlite_database(), db_book)
    book._db_object.position = track.id
    book._chapters = None
    book._settings = None

    calls = []

    class FakeClient:
        def __init__(self, url, token=None):
            calls.append(url)

        def post_progress(self, item_id, current_time, duration, is_finished=False):
            calls.append((item_id, current_time, duration, is_finished))

    monkeypatch.setattr(abs_progress.secrets, "get_server_token", lambda server_id: "token")
    monkeypatch.setattr(abs_progress, "AudiobookshelfClient", FakeClient)

    abs_progress.push_book_progress(book, background=False)

    assert calls[0] == "http://abs.local"
    assert calls[1][0] == "li_1"
    assert mapping.library_item_id == "li_1"


def test_push_book_progress_starts_thread_by_default(peewee_database, monkeypatch):
    from cozy.db.book import Book
    from cozy.model.book import Book as ModelBook
    from cozy.server import abs_progress

    from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
    from cozy.db.track import Track
    from cozy.db.track_to_file import TrackToFile
    from cozy.db.model_base import get_sqlite_database

    started = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(abs_progress.threading, "Thread", FakeThread)
    db_book = Book.create(name="x", author="a", reader="r", position=0, rating=-1)
    track = Track.create(name="01", number=1, disk=1, position=0, book=db_book, length=600)
    TrackToFile.create(track=track, file=_file(), start_at=0)
    server = AudiobookshelfServer.create(name="abs", url="http://abs.local", library_id="lib_1")
    AudiobookshelfBook.create(
        server=server.id, book=db_book, library_item_id="li_2", library_id="lib_1"
    )

    book = ModelBook(get_sqlite_database(), db_book)
    book._settings = None

    abs_progress.push_book_progress(book)

    assert started == ["AbsProgressSync"]
