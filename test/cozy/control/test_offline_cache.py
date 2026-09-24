import logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
import requests

from cozy.control.offline_cache import (
    DOWNLOAD_CANCELLED,
    DOWNLOAD_COMPLETED,
    DOWNLOAD_FAILED,
    DOWNLOAD_NO_SPACE,
    OfflineCache,
    download_remote_file,
)
from cozy.db.file import File
from cozy.db.model_base import get_sqlite_database
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.tools import StoppableThread


class FakeResponse:
    def __init__(self, status_code: int, chunks: list[bytes], content_length=None):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def iter_content(self, chunk_size=None):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests = []

    def get(self, url, headers=None, stream=None, timeout=None):
        self.requests.append({"url": url, "headers": headers or {}})
        return self.response


def test_download_remote_file_writes_file(tmp_path: Path):
    destination = tmp_path / "cached"
    session = FakeSession(FakeResponse(200, [b"abc", b"def"], content_length=6))
    progress = []

    result = download_remote_file(
        "https://abs.local/s/track",
        destination,
        cancelled=lambda: False,
        progress_callback=lambda current, total: progress.append((current, total)),
        session=session,
    )

    assert result == DOWNLOAD_COMPLETED
    assert destination.read_bytes() == b"abcdef"
    assert not (tmp_path / "cached.part").exists()
    assert session.requests[0]["headers"] == {}
    assert progress[-1] == (6, 6)


def test_download_remote_file_resumes_partial_file(tmp_path: Path):
    destination = tmp_path / "cached"
    (tmp_path / "cached.part").write_bytes(b"abc")
    session = FakeSession(FakeResponse(206, [b"def"], content_length=3))

    result = download_remote_file(
        "https://abs.local/s/track", destination, cancelled=lambda: False, session=session
    )

    assert result == DOWNLOAD_COMPLETED
    assert destination.read_bytes() == b"abcdef"
    assert session.requests[0]["headers"] == {"Range": "bytes=3-"}


def test_download_remote_file_restarts_when_range_ignored(tmp_path: Path):
    destination = tmp_path / "cached"
    (tmp_path / "cached.part").write_bytes(b"stale")
    session = FakeSession(FakeResponse(200, [b"new"], content_length=3))

    result = download_remote_file(
        "https://abs.local/s/track", destination, cancelled=lambda: False, session=session
    )

    assert result == DOWNLOAD_COMPLETED
    assert destination.read_bytes() == b"new"
    assert session.requests[0]["headers"] == {"Range": "bytes=5-"}


def test_download_remote_file_keeps_partial_file_on_cancel(tmp_path: Path):
    destination = tmp_path / "cached"
    session = FakeSession(FakeResponse(200, [b"abc", b"def"], content_length=6))

    result = download_remote_file(
        "https://abs.local/s/track", destination, cancelled=lambda: True, session=session
    )

    assert result == DOWNLOAD_CANCELLED
    assert not destination.exists()
    assert not (tmp_path / "cached.part").exists()


def test_download_remote_file_returns_false_on_error_status(tmp_path: Path):
    destination = tmp_path / "cached"
    session = FakeSession(FakeResponse(404, []))

    result = download_remote_file(
        "https://abs.local/s/track", destination, cancelled=lambda: False, session=session
    )

    assert result == DOWNLOAD_FAILED
    assert not destination.exists()


def test_download_remote_file_returns_false_on_request_exception(tmp_path: Path):
    class FailingSession:
        def get(self, *args, **kwargs):
            raise requests.ConnectionError("boom")

    result = download_remote_file(
        "https://abs.local/s/track",
        tmp_path / "cached",
        cancelled=lambda: False,
        session=FailingSession(),
    )

    assert result == DOWNLOAD_FAILED


def test_download_remote_file_closes_response(tmp_path: Path):
    response = FakeResponse(200, [b"abc"], content_length=3)

    download_remote_file(
        "https://abs.local/s/track",
        tmp_path / "cached",
        cancelled=lambda: False,
        session=FakeSession(response),
    )

    assert response.closed is True


def test_download_remote_file_raises_unexpected_errors(tmp_path: Path):
    class BrokenSession:
        def get(self, *args, **kwargs):
            raise ValueError("unexpected")

    with pytest.raises(ValueError):
        download_remote_file(
            "https://abs.local/s/track",
            tmp_path / "cached",
            cancelled=lambda: False,
            session=BrokenSession(),
        )


def _cache(current=None, current_file_progress=(0, 0)):
    cache = OfflineCache.__new__(OfflineCache)
    cache._listeners = []
    cache.queue = []
    cache.current = current
    cache.current_file_progress = current_file_progress
    return cache


def _book(*file_ids):
    return SimpleNamespace(chapters=[SimpleNamespace(file_id=file_id) for file_id in file_ids])


def _cache_entry(path: str, copied: bool):
    file = File.create(path=path, modified=0)
    return OfflineCacheModel.create(original_file=file, cached_file=path, copied=copied)


def _entry_for_book(book, copied: bool):
    file_id = book.chapters[0].file_id
    return OfflineCacheModel.create(
        original_file=file_id, cached_file=f"cached-{file_id}", copied=copied
    )


def test_get_book_progress_returns_none_without_cache_entries(peewee_database):
    assert _cache().get_book_progress(_book(1, 2)) is None


def test_get_book_progress_counts_copied_files(peewee_database):
    first = _cache_entry("file-1", copied=True)
    _cache_entry("file-2", copied=False)

    progress = _cache().get_book_progress(_book(first.original_file_id, 999))

    assert progress == 0.5


def test_get_book_progress_includes_running_file(peewee_database):
    first = _cache_entry("file-1", copied=True)
    second = _cache_entry("file-2", copied=False)

    cache = _cache(current=second, current_file_progress=(500, 1000))
    progress = cache.get_book_progress(_book(first.original_file_id, second.original_file_id))

    assert progress == 0.75


def test_get_book_progress_is_complete_when_all_files_copied(peewee_database):
    first = _cache_entry("file-1", copied=True)
    second = _cache_entry("file-2", copied=True)

    progress = _cache().get_book_progress(_book(first.original_file_id, second.original_file_id))

    assert progress == 1.0


class _Handler(BaseHTTPRequestHandler):
    payload = b""
    status = 200
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        self.send_response(type(self).status)
        self.send_header("Content-Length", str(len(type(self).payload)))
        self.end_headers()
        self.wfile.write(type(self).payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _model_book(url: str):
    from cozy.db.book import Book
    from cozy.db.track import Track
    from cozy.db.track_to_file import TrackToFile
    from cozy.model.book import Book as ModelBook

    db_book = Book.create(name="Book", author="A", reader="R", position=0, rating=-1)
    file = File.create(path=url, modified=0)
    track = Track.create(name="01", number=1, disk=1, position=0, book=db_book, length=600)
    TrackToFile.create(track=track, file=file, start_at=0)

    book = ModelBook(get_sqlite_database(), db_book)
    book._settings = None
    return book


def _cache_with_file(url: str, tmp_path: Path):
    book = _model_book(url)

    book.downloaded = False

    cache = _cache()
    cache.cache_dir = tmp_path
    cache.thread = StoppableThread(target=lambda: None)
    cache._library = SimpleNamespace(books=[book])

    return cache, book


def test_process_queue_downloads_remote_file(peewee_database, tmp_path, http_server):
    _Handler.payload = b"x" * 4096
    _Handler.status = 200
    _Handler.requests = 0
    url = f"http://127.0.0.1:{http_server.server_address[1]}/s/item/li_1/01.mp3"
    cache, book = _cache_with_file(url, tmp_path)
    events = []
    cache.add_listener(lambda event, message: events.append((event, message)))

    cache.add(book)
    cache._process_queue()

    entry = OfflineCacheModel.get()
    assert entry.copied is True
    assert (tmp_path / entry.cached_file).read_bytes() == b"x" * 4096
    assert _Handler.requests == 1
    assert book.downloaded is True
    assert ("finished", None) in events


def test_process_queue_does_not_retry_failed_download_in_same_run(
    peewee_database, tmp_path, http_server
):
    _Handler.payload = b""
    _Handler.status = 404
    _Handler.requests = 0
    url = f"http://127.0.0.1:{http_server.server_address[1]}/s/item/li_1/missing.mp3"
    cache, book = _cache_with_file(url, tmp_path)

    cache.add(book)
    cache._process_queue()

    assert _Handler.requests == 1
    assert OfflineCacheModel.get().copied is False


def test_download_remote_file_does_not_log_token(tmp_path: Path, caplog):
    url = "https://abs.local/s/track?token=supersecret"
    session = FakeSession(FakeResponse(200, [b"abc"], content_length=3))

    with caplog.at_level(logging.DEBUG, logger="offline_cache"):
        download_remote_file(url, tmp_path / "cached", cancelled=lambda: False, session=session)

    assert "supersecret" not in caplog.text
    assert "https://abs.local/s/track" in caplog.text


def test_download_remote_file_reports_insufficient_space(tmp_path: Path, monkeypatch):
    import cozy.control.offline_cache as offline_cache_module

    monkeypatch.setattr(offline_cache_module, "free_disk_space", lambda path: 0)
    session = FakeSession(FakeResponse(200, [b"abc"], content_length=3))

    result = download_remote_file(
        "https://abs.local/s/track", tmp_path / "cached", cancelled=lambda: False, session=session
    )

    assert result == DOWNLOAD_NO_SPACE
    assert not (tmp_path / "cached").exists()


def test_get_cache_size_sums_cached_files(peewee_database, tmp_path):
    (tmp_path / "one").write_bytes(b"x" * 100)
    (tmp_path / "two").write_bytes(b"x" * 250)

    cache = _cache()
    cache.cache_dir = tmp_path

    assert cache.get_cache_size() == 350


def test_clear_cache_removes_files_and_resets_flags(peewee_database, tmp_path):
    cache, book = _cache_with_file("https://abs.local/s/track.mp3", tmp_path)
    book.offline = True
    book.downloaded = True
    entry = _entry_for_book(book, copied=True)
    (tmp_path / entry.cached_file).write_bytes(b"x" * 10)

    cache.clear_cache()

    assert not (tmp_path / entry.cached_file).exists()
    assert not OfflineCacheModel.select().exists()
    assert book.downloaded is False
    assert book.offline is False


def test_remove_offline_books_only_removes_offline_books(peewee_database, tmp_path):
    cache, offline_book = _cache_with_file("https://abs.local/s/track.mp3", tmp_path)
    offline_book.offline = True
    entry = _entry_for_book(offline_book, copied=True)
    (tmp_path / entry.cached_file).write_bytes(b"x" * 10)

    online_book = _model_book("https://abs.local/s/other.mp3")
    cache._library = SimpleNamespace(books=[offline_book, online_book])

    cache.remove_offline_books()

    assert not OfflineCacheModel.select().exists()
    assert not (tmp_path / entry.cached_file).exists()
    assert offline_book.offline is False
    assert online_book.offline is False


def test_format_size():
    from cozy.ui.widgets.offline_cache import format_size

    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1024) == "1.0 kB"
    assert format_size(1024 * 1024 * 3) == "3.0 MB"
    assert format_size(1024**3 * 2) == "2.0 GB"
