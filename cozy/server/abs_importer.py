import logging
import time
from typing import Callable, Optional

from peewee import DoesNotExist

from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.db.book import Book
from cozy.db.file import File
from cozy.db.track import Track
from cozy.db.track_to_file import TrackToFile

log = logging.getLogger("abs_importer")

NANOSECONDS_PER_SECOND = 1_000_000_000
UNKNOWN = "Unknown"
PAUSE_BETWEEN_ITEMS = 0.5


class SyncResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.removed = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


class AbsImporter:
    def __init__(self, client, server: AudiobookshelfServer):
        self._client = client
        self._server = server

    def sync(self, progress_callback: Optional[Callable[[float], None]] = None) -> SyncResult:
        items = self._client.get_library_items(self._server.library_id)
        total = max(len(items), 1)
        seen_item_ids = set()
        result = SyncResult()

        for index, item in enumerate(items, start=1):
            if progress_callback:
                progress_callback(index / total)

            item_id = item["id"]
            updated_at = item.get("updatedAt", 0)
            progress = (item.get("media") or {}).get("progress")

            mapping = self._get_mapping(item_id)
            if mapping is not None and mapping.updated_at == updated_at:
                seen_item_ids.add(item_id)
                result.skipped += 1
                self._apply_progress_to_mapping(mapping, progress)
                continue

            time.sleep(PAUSE_BETWEEN_ITEMS)
            detail = self._client.get_item(item_id)
            self._import_item(detail, progress)

            seen_item_ids.add(item_id)
            if mapping is not None:
                result.updated += 1
            else:
                result.created += 1

        result.removed = self._remove_departed_books(seen_item_ids)

        return result

    def _import_item(self, item: dict, progress: dict = None) -> None:
        item_id = item["id"]
        library_id = item.get("libraryId", "")
        updated_at = item.get("updatedAt", 0)
        media = item.get("media", {})
        metadata = media.get("metadata", {})
        audio_tracks = media.get("tracks") or media.get("audioTracks") or []

        if not audio_tracks:
            self._mark_synced(item_id, library_id, updated_at)
            return

        mapping = self._get_or_create_mapping(item_id)
        book = self._get_book(mapping)

        book.name = metadata.get("title") or ""
        book.author = metadata.get("authorName") or UNKNOWN
        book.reader = metadata.get("narratorName") or UNKNOWN
        book.hidden = False

        self._delete_content(book)

        if mapping is not None:
            book.position = 0

        for audio_track in audio_tracks:
            self._import_audio_track(book, audio_track, media.get("chapters") or [])

        cover = self._client.get_cover(item_id)
        if cover:
            book.cover = cover

        if progress:
            self._apply_progress_to_book(book, progress)

        book.save()
        self._update_mapping(mapping, item_id, library_id, updated_at, book)

    def _apply_progress_to_mapping(
        self, mapping: AudiobookshelfBook, progress: Optional[dict]
    ) -> None:
        if not progress:
            return

        try:
            db_book = mapping.book
        except DoesNotExist:
            return

        self._apply_progress_to_book(db_book, progress)

    @staticmethod
    def _apply_progress_to_book(db_book: Book, progress: Optional[dict]) -> None:
        if not progress:
            return

        current_time_ns = int(float(progress.get("currentTime") or 0) * NANOSECONDS_PER_SECOND)
        if current_time_ns <= 0:
            return

        duration_ns = int(
            float(progress.get("duration") or 0) * NANOSECONDS_PER_SECOND
        )
        if duration_ns and current_time_ns >= duration_ns - NANOSECONDS_PER_SECOND:
            db_book.position = -1
            db_book.save(only=[Book.position])
            return

        tracks = list(
            Track.select().where(Track.book == db_book).order_by(Track.disk, Track.number)
        )
        if not tracks:
            return

        completed_ns = 0
        for track in tracks:
            start_at = TrackToFile.get(TrackToFile.track == track).start_at
            length_ns = int(float(track.length) * NANOSECONDS_PER_SECOND)

            if completed_ns + length_ns > current_time_ns:
                track.position = start_at + max(current_time_ns - completed_ns, 0)
                track.save(only=[Track.position])
                db_book.position = track.id
                db_book.save(only=[Book.position])
                return

            completed_ns += length_ns

        db_book.position = -1
        db_book.save(only=[Book.position])

    def _import_audio_track(self, book: Book, audio_track: dict, book_chapters: list[dict]) -> None:
        content_url = audio_track.get("contentUrl", "")
        file_path = (
            self._client.base_url
            + ("/" if content_url and not content_url.startswith("/") else "")
            + content_url
        )
        duration = audio_track.get("duration") or 0.0
        start_offset = audio_track.get("startOffset") or 0.0

        metadata = audio_track.get("metadata") or {}
        filename = metadata.get("filename") or audio_track.get("title") or ""

        file_model = File.get_or_none(File.path == file_path)
        if file_model is None:
            file_model = File.create(path=file_path, modified=0)

        track_chapters = audio_track.get("chapters") or book_chapters
        if track_chapters:
            for number, chapter in enumerate(track_chapters, start=1):
                self._import_book_chapter(book, file_model, chapter, number, start_offset, duration)
        else:
            track = Track.create(
                name=filename,
                number=int(audio_track.get("index") or 1),
                disk=1,
                position=0,
                book=book,
                length=duration,
            )
            TrackToFile.create(track=track, file=file_model, start_at=0)

    @staticmethod
    def _import_book_chapter(
        book: Book,
        file_model: File,
        chapter: dict,
        number: int,
        start_offset: float,
        duration: float,
    ) -> None:
        start = max(float(chapter.get("start") or 0.0), start_offset)
        end = min(float(chapter.get("end") or start), start_offset + duration)
        if end <= start:
            return

        track = Track.create(
            name=chapter.get("title") or "",
            number=number,
            disk=1,
            position=0,
            book=book,
            length=end - start,
        )
        start_at = int((start - start_offset) * NANOSECONDS_PER_SECOND)
        TrackToFile.create(track=track, file=file_model, start_at=start_at)

    @staticmethod
    def _delete_content(book: Book) -> None:
        file_ids = set()
        mappings = TrackToFile.select().join(Track).join(Book).where(Track.book == book)
        for mapping in mappings:
            file_ids.add(mapping.file.id)

        track_ids = [track.id for track in Track.select().where(Track.book == book)]
        TrackToFile.delete().where(TrackToFile.track.in_(track_ids)).execute()
        Track.delete().where(Track.book == book).execute()

        for file_id in file_ids:
            if not TrackToFile.select().join(File).where(TrackToFile.file.id == file_id).exists():
                File.delete().where(File.id == file_id).execute()

    def _remove_departed_books(self, seen_item_ids: set[str]) -> int:
        removed = 0
        mappings = AudiobookshelfBook.select().where(
            AudiobookshelfBook.server == self._server.id,
            AudiobookshelfBook.library_id == self._server.library_id,
        )
        for mapping in mappings:
            if mapping.library_item_id in seen_item_ids:
                continue

            book = mapping.book
            book.hidden = True
            book.save(only=[Book.hidden])
            removed += 1

        return removed

    def _get_mapping(self, item_id: str) -> Optional[AudiobookshelfBook]:
        return AudiobookshelfBook.get_or_none(
            AudiobookshelfBook.server == self._server.id,
            AudiobookshelfBook.library_item_id == item_id,
        )

    def _get_or_create_mapping(self, item_id: str) -> AudiobookshelfBook:
        mapping = self._get_mapping(item_id)
        if mapping is None:
            book = Book.create(name="", author=UNKNOWN, reader=UNKNOWN, position=0, rating=-1)
            mapping = AudiobookshelfBook.create(
                server=self._server.id, book=book, library_item_id=item_id, library_id=""
            )
        return mapping

    def _get_book(self, mapping: AudiobookshelfBook) -> Book:
        try:
            return mapping.book
        except DoesNotExist:
            book = Book.create(name="", author=UNKNOWN, reader=UNKNOWN, position=0, rating=-1)
            mapping.book = book
            mapping.save()
            return book

    def _mark_synced(self, item_id: str, library_id: str, updated_at: int) -> None:
        mapping = self._get_mapping(item_id)
        if mapping is None:
            return

        mapping.library_id = library_id
        mapping.updated_at = updated_at
        mapping.save()

    @staticmethod
    def _update_mapping(
        mapping: AudiobookshelfBook, item_id: str, library_id: str, updated_at: int, book: Book
    ) -> None:
        mapping.book = book
        mapping.library_item_id = item_id
        mapping.library_id = library_id
        mapping.updated_at = updated_at
        mapping.save()
