import json
import logging
from contextlib import suppress

import inject
from peewee import DoesNotExist, SqliteDatabase

from cozy.architecture.event_sender import EventSender
from cozy.architecture.observable import Observable
from cozy.db.book import Book as BookModel
from cozy.db.collation import collate_natural
from cozy.db.track import Track as TrackModel
from cozy.db.track_to_file import TrackToFile
from cozy.model.chapter import Chapter
from cozy.control import time_format
from cozy.model.settings import Settings
from cozy.model.track import Track, TrackInconsistentData
from cozy.settings import ApplicationSettings

log = logging.getLogger("BookModel")


class BookIsEmpty(Exception):
    pass


KNOWN_METADATA_KEYS = frozenset(
    {
        "title",
        "titleIgnorePrefix",
        "authorName",
        "authorNameLF",
        "narratorName",
        "narratorNameLF",
        "seriesName",
        "series",
        "seriesPart",
        "publishedYear",
        "publisher",
        "description",
        "language",
        "isbn",
        "asin",
    }
)


def _humanize_metadata_key(key: str) -> str:
    special = {
        "authorNameLF": _("Author (sorted)"),
        "narratorNameLF": _("Narrator (sorted)"),
        "titleIgnorePrefix": _("Title without prefix"),
    }

    if key in special:
        return special[key]

    return key.replace("_", " ").strip().capitalize()


def _metadata_value_to_text(value) -> str:
    if value is None or value == "":
        return ""

    if isinstance(value, bool):
        return _("Yes") if value else _("No")

    if isinstance(value, (list, tuple)):
        return ", ".join(_metadata_value_to_text(entry) for entry in value)

    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    return str(value)


def _split_series_name(value: str) -> tuple[str, float | None]:
    text = value.strip()

    if "#" not in text:
        return text, None

    name, _, raw_part = text.rpartition("#")
    name = name.strip()
    if not name:
        return text, None

    part_text = raw_part.strip()
    try:
        return name, float(part_text)
    except ValueError:
        leading = part_text.split("-")[0].strip()
        try:
            return name, float(leading)
        except ValueError:
            return name, None


def _format_series_part(part: float) -> str:
    if part and part.is_integer():
        return str(int(part))

    return f"{part:g}"


class Book(Observable, EventSender):
    _chapters: list[Chapter] = None
    _settings: Settings = inject.attr(Settings)
    _app_settings: ApplicationSettings = inject.attr(ApplicationSettings)

    def __init__(self, db: SqliteDatabase, book: BookModel):
        super().__init__()
        super(Observable, self).__init__()

        self._db: SqliteDatabase = db
        self.id: int = book.id

        self._db_object: BookModel = book

        if TrackModel.select().where(TrackModel.book == self._db_object).count() < 1:
            raise BookIsEmpty

    @property
    def name(self):
        return self._db_object.name

    @name.setter
    def name(self, new_name: str):
        self._db_object.name = new_name
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def author(self):
        if not self._app_settings.swap_author_reader:
            return self._db_object.author
        else:
            return self._db_object.reader

    @author.setter
    def author(self, new_author: str):
        if not self._app_settings.swap_author_reader:
            self._db_object.author = new_author
        else:
            self._db_object.reader = new_author

        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def reader(self):
        if not self._app_settings.swap_author_reader:
            return self._db_object.reader
        else:
            return self._db_object.author

    @reader.setter
    def reader(self, new_reader: str):
        if not self._app_settings.swap_author_reader:
            self._db_object.reader = new_reader
        else:
            self._db_object.author = new_reader

        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def hidden(self):
        return self._db_object.hidden

    @hidden.setter
    def hidden(self, value: bool):
        self._db_object.hidden = value

        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def position(self) -> int:
        return self._db_object.position

    @position.setter
    def position(self, new_position: int):
        self._db_object.position = new_position
        self._db_object.save(only=self._db_object.dirty_fields)
        self._notify("position")
        self._notify("current_chapter")

    @property
    def rating(self):
        return self._db_object.rating

    @rating.setter
    def rating(self, new_rating: int):
        self._db_object.rating = new_rating
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def cover(self):
        return self._db_object.cover

    @cover.setter
    def cover(self, new_cover: bytes):
        self._db_object.cover = new_cover
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def series(self):
        return self._db_object.series or ""

    @series.setter
    def series(self, new_series: str):
        self._db_object.series = new_series or None
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def series_part(self):
        return self._db_object.series_part

    @property
    def series_entries(self) -> list[tuple[str, float | None]]:
        if not self.series:
            return []

        entries: list[tuple[str, float | None]] = []
        positions: dict[str, int] = {}

        for index, raw_name in enumerate(self.series.split(",")):
            name, embedded_part = _split_series_name(raw_name)
            if not name:
                continue

            part = embedded_part
            if part is None and index == 0:
                part = self.series_part

            if name in positions:
                position = positions[name]
                if entries[position][1] is None and part is not None:
                    entries[position] = (name, part)
                continue

            positions[name] = len(entries)
            entries.append((name, part))

        return entries

    @property
    def series_text(self) -> str:
        if not self.series:
            return ""

        order: list[str] = []
        parts: dict[str, float | None] = {}
        labels: dict[str, str] = {}

        for segment in self.series.split(","):
            raw = segment.strip()
            if not raw:
                continue

            name, embedded_part = _split_series_name(raw)
            if not name:
                continue

            if name not in parts:
                order.append(name)
                parts[name] = embedded_part
                labels[name] = raw
            elif parts[name] is None and embedded_part is not None:
                parts[name] = embedded_part
                labels[name] = raw

        if not order:
            return ""

        if self.series_part is not None and parts[order[0]] is None:
            parts[order[0]] = self.series_part

        texts = []
        for name in order:
            label = labels[name]
            if parts[name] is None or "#" in label:
                texts.append(label)
            else:
                texts.append(f"{name} #{_format_series_part(parts[name])}")

        return ", ".join(texts)

    def series_part_for(self, series: str) -> float | None:
        for name, part in self.series_entries:
            if name == series:
                return part

        return None

    @property
    def description(self):
        return self._db_object.description or ""

    @property
    def publisher(self):
        return self._db_object.publisher or ""

    @property
    def published_year(self):
        return self._db_object.published_year

    @property
    def language(self):
        return self._db_object.language or ""

    @property
    def asin(self):
        return self._db_object.asin or ""

    @property
    def metadata(self) -> dict:
        if not self._db_object.metadata_json:
            return {}

        try:
            data = json.loads(self._db_object.metadata_json)
        except (TypeError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    @property
    def extra_metadata(self) -> list[tuple[str, str]]:
        extras = []
        for key, value in self.metadata.items():
            if key in KNOWN_METADATA_KEYS:
                continue

            text = _metadata_value_to_text(value)
            if not text:
                continue

            extras.append((_humanize_metadata_key(key), text))

        return extras

    @property
    def status_text(self) -> str:
        if self.position == -1:
            return _("Finished")

        if self.position == 0 or self.progress <= 0:
            return _("Not started")

        percent = int(self.progress / self.duration * 100) if self.duration else 0
        return _("{percent} % · {progress} of {total}").format(
            percent=percent,
            progress=time_format.ns_to_human_readable(self.progress),
            total=time_format.ns_to_human_readable(self.duration),
        )

    @property
    def has_status(self) -> bool:
        return self.position != 0 or self.position == -1

    @property
    def has_details(self) -> bool:
        return bool(
            self.series
            or self.description
            or self.publisher
            or self.published_year
            or self.language
            or self.asin
            or self.extra_metadata
            or self.has_status
        )

    @property
    def playback_speed(self):
        return self._db_object.playback_speed

    @playback_speed.setter
    def playback_speed(self, new_playback_speed: float):
        self._db_object.playback_speed = new_playback_speed
        self._db_object.save(only=self._db_object.dirty_fields)
        self._notify("playback_speed")

    @property
    def last_played(self):
        return self._db_object.last_played

    @last_played.setter
    def last_played(self, new_last_played: int):
        self._db_object.last_played = new_last_played
        self._db_object.save(only=self._db_object.dirty_fields)
        self._notify("last_played")

    @property
    def offline(self):
        return self._db_object.offline

    @offline.setter
    def offline(self, new_offline: bool):
        self._db_object.offline = new_offline
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def downloaded(self):
        return self._db_object.downloaded

    @downloaded.setter
    def downloaded(self, new_downloaded: bool):
        self._db_object.downloaded = new_downloaded
        self._db_object.save(only=self._db_object.dirty_fields)

    @property
    def chapters(self):
        if not self._chapters:
            self._fetch_chapters()

        return self._chapters

    @property
    def current_chapter(self):
        return next(
            (chapter for chapter in self.chapters if chapter.id == self.position), self.chapters[0]
        )

    @property
    def duration(self):
        return sum(chapter.length for chapter in self.chapters)

    @property
    def progress(self):
        progress = 0

        if self.position == 0:
            return 0
        elif self.position == -1:
            return self.duration

        for chapter in self.chapters:
            if chapter.id == self.position:
                relative_position = max(chapter.position - chapter.start_position, 0)
                progress += relative_position
                return progress

            progress += chapter.length

        return progress

    def reset(self) -> None:
        self.last_played = 0

    def mark_as_read(self) -> None:
        self.position = -1

    def mark_as_unread(self) -> None:
        self.position = 0

    def remove(self, delete_db_objects: bool = False):
        if (
            self._settings.last_played_book
            and self._settings.last_played_book.id == self._db_object.id
        ):
            self._settings.last_played_book = None

        if delete_db_objects:
            book_tracks = [TrackModel.get_by_id(chapter.id) for chapter in self.chapters]
            track_to_files = (
                TrackToFile.select().join(TrackModel).where(TrackToFile.track << book_tracks)
            )

            for track in track_to_files:
                try:
                    track.file.delete_instance(recursive=True)
                except DoesNotExist:
                    track.delete_instance()

            for track in book_tracks:
                track.delete_instance(recursive=True)

            self._db_object.delete_instance(recursive=True)
        else:
            self.hidden = True

        self.destroy_listeners()
        self._destroy_observers()

    def _fetch_chapters(self):
        tracks = (
            TrackModel.select()
            .where(TrackModel.book == self._db_object)
            .order_by(
                TrackModel.disk, TrackModel.number, collate_natural.collation(TrackModel.name)
            )
        )

        self._chapters = []
        for track in tracks:
            try:
                track_model = Track(self._db, track)
                self._chapters.append(track_model)
            except TrackInconsistentData:
                log.warning("Skipping inconsistent model")
            except Exception as e:
                log.error("Could not create chapter object: %s", e)

        for chapter in self._chapters:
            chapter.add_listener(self._on_chapter_event)

    def _on_chapter_event(self, event: str, chapter: Chapter):
        if event == "chapter-deleted":
            with suppress(ValueError):
                self.chapters.remove(chapter)

            if len(self._chapters) < 1:
                if (
                    self._settings.last_played_book
                    and self._settings.last_played_book.id == self._db_object.id
                ):
                    self._settings.last_played_book = None

                self._db_object.delete_instance(recursive=True)
                self.emit_event("book-deleted", self)
                self.destroy_listeners()
                self._destroy_observers()
