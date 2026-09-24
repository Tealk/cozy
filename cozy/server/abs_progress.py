import logging
import threading

from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.server.audiobookshelf_client import AudiobookshelfClient

log = logging.getLogger("abs_progress")

NANOSECONDS_PER_SECOND = 1_000_000_000


def progress_in_seconds(book) -> tuple[float, float]:
    duration = book.duration / NANOSECONDS_PER_SECOND
    current_time = min(book.progress / NANOSECONDS_PER_SECOND, duration)
    return current_time, duration


def push_book_progress(book, background: bool = True) -> None:
    if book is None:
        return

    mapping = AudiobookshelfBook.get_or_none(AudiobookshelfBook.book == book.id)
    if mapping is None:
        return

    server = AudiobookshelfServer.get_or_none(id=mapping.server_id)
    if server is None:
        return

    current_time, duration = progress_in_seconds(book)
    if duration <= 0:
        return

    try:
        token = secrets.get_server_token(server.id)
    except Exception as e:
        log.warning("ABS progress: no token available: %s", e)
        return

    def send():
        try:
            AudiobookshelfClient(server.url, token=token).post_progress(
                mapping.library_item_id, current_time, duration, is_finished=book.position == -1
            )
        except Exception as e:
            log.warning("Could not push progress to ABS: %s", e)

    if not background:
        send()
        return

    threading.Thread(target=send, name="AbsProgressSync", daemon=True).start()