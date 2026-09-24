import json
import logging
import re
import time
from typing import Callable, Optional

import requests
from peewee import DoesNotExist

from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.db.book import Book
from cozy.db.file import File
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.db.track import Track
from cozy.db.track_to_file import TrackToFile
from cozy.server.audiobookshelf_client import AudiobookshelfError

log = logging.getLogger("abs_importer")

NANOSECONDS_PER_SECOND = 1_000_000_000
UNKNOWN = "Unknown"
PAUSE_BETWEEN_ITEMS = 0.5


def _parse_series(
    media: dict, metadata: dict
) -> tuple[list[tuple[str, Optional[float]]], Optional[float]]:
    metadata_part = _to_float(metadata.get("seriesPart"))

    for raw in (media.get("series"), metadata.get("series"), metadata.get("seriesName")):
        entries = _series_entries(raw)
        if not entries:
            continue

        if entries[0][1] is None and metadata_part is not None:
            entries[0] = (entries[0][0], metadata_part)

        return entries, entries[0][1]

    return [], metadata_part


def _series_entries(raw) -> list[tuple[str, Optional[float]]]:
    if not raw:
        return []

    if isinstance(raw, str):
        return _split_series_names(raw)

    if isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, list):
        return []

    entries = []
    for entry in raw:
        if isinstance(entry, str):
            entries.extend(_split_series_names(entry))
        elif isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue

            entries.append((name, _to_float(entry.get("sequence"))))

    return entries


def _split_series_names(value: str) -> list[tuple[str, Optional[float]]]:
    entries = []
    for part in re.split(r"[,;|]", value):
        name, series_part = _split_series_name(part)
        if name:
            entries.append((name, series_part))

    return entries


def _split_series_name(value: str) -> tuple[Optional[str], Optional[float]]:
    text = value.strip()

    if "#" not in text:
        return text, None

    name, _, raw_part = text.rpartition("#")
    name = name.strip()
    if not name:
        return text, None

    part = _to_float(raw_part.strip())
    if part is None:
        return text, None

    return name, part


def _to_float(value) -> Optional[float]:
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_series(name: str, part: Optional[float]) -> str:
    if part is None:
        return name

    if part.is_integer():
        return f"{name} #{int(part)}"

    return f"{name} #{part:g}"


def _to_int(value) -> Optional[int]:
    if value is None or value == "":
        return None

    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


class SyncResult:
    def __init__(self):
        self.created = 0
        self.updated = 0
        self.skipped = 0
        self.removed = 0
        self.changed_books: list[int] = []
        self.removed_cached_files: list[str] = []
        self.failed_items: list[str] = []
        self.metadata_backfilled = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.skipped


class AbsImporter:
    def __init__(self, client, server: AudiobookshelfServer):
        self._client = client
        self._server = server

    def sync(
        self, progress_callback: Optional[Callable[[float], None]] = None, force: bool = False
    ) -> SyncResult:
        items = self._client.get_library_items(self._server.library_id)
        progress_map = self._get_progress_map()
        total = max(len(items), 1)
        seen_item_ids = set()
        result = SyncResult()

        if force:
            log.info("Running a forced sync, the server is the source of truth")

        for index, item in enumerate(items, start=1):
            if progress_callback:
                progress_callback(index / total)

            item_id = item["id"]
            updated_at = item.get("updatedAt", 0)
            media = item.get("media") or {}
            progress = progress_map.get(item_id) or media.get("progress")
            metadata = media.get("metadata") or {}

            mapping = self._get_mapping(item_id)
            if not force and mapping is not None and mapping.updated_at == updated_at:
                seen_item_ids.add(item_id)
                result.skipped += 1
                self._apply_progress_to_mapping(mapping, progress)
                self._apply_metadata_to_mapping(mapping, metadata, media)
                if metadata:
                    self._backfill_metadata(mapping, item_id, result)
                continue

            time.sleep(PAUSE_BETWEEN_ITEMS)

            try:
                detail = self._client.get_item(item_id)
                files_changed, removed_cached_files = self._import_item(
                    detail, progress, force=force
                )
            except (AudiobookshelfError, requests.RequestException) as e:
                log.warning("Skipping item %s: %s", item_id, e)
                result.failed_items.append(self._item_name(item, item_id))
                seen_item_ids.add(item_id)
                continue

            result.removed_cached_files.extend(removed_cached_files)

            seen_item_ids.add(item_id)
            if mapping is not None:
                result.updated += 1
                if files_changed:
                    result.changed_books.append(mapping.book.id)
            else:
                result.created += 1

        result.removed = self._remove_departed_books(seen_item_ids)

        log.info(
            "Sync finished: %d created, %d updated, %d unchanged, %d metadata updates, "
            "%d hidden, %d failed",
            result.created,
            result.updated,
            result.skipped,
            result.metadata_backfilled,
            result.removed,
            len(result.failed_items),
        )

        return result

    def _apply_metadata(self, book: Book, metadata: dict, media: dict = None) -> None:
        entries, series_part = _parse_series(media or {}, metadata)
        book.series = ", ".join(_format_series(name, part) for name, part in entries) or None
        book.series_part = series_part
        book.description = (metadata.get("description") or "").strip() or None
        book.publisher = (metadata.get("publisher") or "").strip() or None
        book.published_year = _to_int(metadata.get("publishedYear"))
        book.language = (metadata.get("language") or "").strip() or None
        book.asin = (metadata.get("asin") or metadata.get("isbn") or "").strip() or None
        book.metadata_json = (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None
        )

    @staticmethod
    def _item_name(item: dict, item_id: str) -> str:
        metadata = (item.get("media") or {}).get("metadata") or {}
        return metadata.get("title") or item_id

    def _import_item(
        self, item: dict, progress: dict = None, force: bool = False
    ) -> tuple[bool, list[str]]:
        item_id = item["id"]
        library_id = item.get("libraryId", "")
        updated_at = item.get("updatedAt", 0)
        media = item.get("media", {})
        metadata = media.get("metadata", {})
        audio_tracks = media.get("tracks") or media.get("audioTracks") or []

        if not audio_tracks:
            self._mark_synced(item_id, library_id, updated_at)
            return False, []

        mapping = self._get_or_create_mapping(item_id)
        book = self._get_book(mapping)

        book.name = metadata.get("title") or ""
        book.author = metadata.get("authorName") or UNKNOWN
        book.reader = metadata.get("narratorName") or UNKNOWN
        book.hidden = False
        self._apply_metadata(book, metadata, media)

        existing_paths = self._file_paths_for_book(book)
        removed_cached_files = self._delete_content(book)

        for audio_track in audio_tracks:
            self._import_audio_track(book, audio_track, media.get("chapters") or [])

        cover = self._client.get_cover(item_id)
        if cover:
            book.cover = cover

        files_changed = bool(existing_paths) and self._file_paths_for_book(book) != existing_paths

        if progress:
            self._apply_progress_to_book(book, progress)
        elif mapping is not None and (files_changed or force):
            book.position = 0
            book.save(only=[Book.position])

        book.save()
        self._update_mapping(mapping, item_id, library_id, updated_at, book)

        return files_changed, removed_cached_files

    def _get_progress_map(self) -> dict:
        try:
            return self._client.get_progress()
        except (AttributeError, AudiobookshelfError, requests.RequestException) as e:
            log.warning("Could not read playback progress from the server: %s", e)
            return {}

    @staticmethod
    @staticmethod
    def _file_paths_for_book(book: Book) -> set[str]:
        query = File.select(File.path).join(TrackToFile).join(Track).where(Track.book == book)
        return {row.path for row in query}

    def _backfill_metadata(
        self, mapping: AudiobookshelfBook, item_id: str, result: SyncResult
    ) -> None:
        if not self._needs_metadata(mapping):
            return

        time.sleep(PAUSE_BETWEEN_ITEMS)

        try:
            detail = self._client.get_item(item_id)
        except (AudiobookshelfError, requests.RequestException) as e:
            log.warning("Could not fetch metadata for %s: %s", item_id, e)
            return

        detail_media = detail.get("media") or {}
        detail_metadata = detail_media.get("metadata") or {}
        if self._apply_metadata_to_mapping(mapping, detail_metadata, detail_media):
            result.metadata_backfilled += 1

    @staticmethod
    def _needs_metadata(mapping: AudiobookshelfBook) -> bool:
        try:
            db_book = mapping.book
        except DoesNotExist:
            return False

        return not (db_book.series or db_book.publisher or db_book.description or db_book.asin)

    def _apply_metadata_to_mapping(
        self, mapping: AudiobookshelfBook, metadata: dict, media: dict = None
    ) -> bool:
        if not metadata:
            return False

        try:
            db_book = mapping.book
        except DoesNotExist:
            return False

        self._apply_metadata(db_book, metadata, media)
        if not db_book.is_dirty():
            return False

        log.debug(
            "Updated metadata for %s (series=%r, publisher=%r)",
            db_book.name,
            db_book.series,
            db_book.publisher,
        )
        db_book.save(only=db_book.dirty_fields)
        return True

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

        if progress.get("isFinished"):
            db_book.position = -1
            db_book.save(only=[Book.position])
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
    def _delete_content(book: Book) -> list[str]:
        file_ids = set()
        mappings = TrackToFile.select().join(Track).join(Book).where(Track.book == book)
        for mapping in mappings:
            file_ids.add(mapping.file.id)

        track_ids = [track.id for track in Track.select().where(Track.book == book)]
        TrackToFile.delete().where(TrackToFile.track.in_(track_ids)).execute()
        Track.delete().where(Track.book == book).execute()

        removed_cached_files = []
        for file_id in file_ids:
            if not TrackToFile.select().join(File).where(TrackToFile.file.id == file_id).exists():
                cache_entries = OfflineCacheModel.select().where(
                    OfflineCacheModel.original_file == file_id
                )
                removed_cached_files.extend(entry.cached_file for entry in cache_entries)
                OfflineCacheModel.delete().where(
                    OfflineCacheModel.original_file == file_id
                ).execute()
                File.delete().where(File.id == file_id).execute()

        return removed_cached_files

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
