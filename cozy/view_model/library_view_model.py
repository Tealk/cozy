import logging
import os
from enum import Enum, auto
from typing import Optional

import inject
from gi.repository import Gio, Gtk

from cozy.architecture.event_sender import EventSender
from cozy.architecture.observable import Observable
from cozy.control.filesystem_monitor import FilesystemMonitor
from cozy.enums import OpenView
from cozy.media.importer import Importer, ScanStatus
from cozy.media.player import Player
from cozy.model.book import Book
from cozy.model.library import Library, split_strings_to_set
from cozy.settings import ApplicationSettings
from cozy.ui.delete_book_view import DeleteBookView
from cozy.ui.file_not_found_dialog import FileNotFoundDialog
from cozy.ui.import_failed_dialog import ImportFailedDialog
from cozy.ui.widgets.book_card import BookCard
from cozy.view_model.storages_view_model import StoragesViewModel

log = logging.getLogger("library_view_model")

NO_SERIES = _("No series")


class LibraryViewMode(Enum):
    CURRENT = auto()
    AUTHOR = auto()
    READER = auto()
    SERIES = auto()


class LibraryViewModel(Observable, EventSender):
    _application_settings: ApplicationSettings = inject.attr(ApplicationSettings)
    _fs_monitor: FilesystemMonitor = inject.attr("FilesystemMonitor")
    _model = inject.attr(Library)
    _importer: Importer = inject.attr(Importer)
    _player: Player = inject.attr(Player)
    _storages: StoragesViewModel = inject.attr(StoragesViewModel)

    def __init__(self):
        super().__init__()
        super(Observable, self).__init__()

        self._library_view_mode: LibraryViewMode = LibraryViewMode.CURRENT
        self._selected_filter: str = _("All")

        self._connect()

    def _connect(self):
        self._fs_monitor.add_listener(self._on_fs_monitor_event)
        self._application_settings.add_listener(self._on_application_setting_changed)
        self._importer.add_listener(self._on_importer_event)
        self._player.add_listener(self._on_player_event)
        self._model.add_listener(self._on_model_event)
        self._storages.add_listener(self._on_storages_event)

    @property
    def books(self):
        return self._model.books

    @property
    def library_view_mode(self):
        return self._library_view_mode

    @library_view_mode.setter
    def library_view_mode(self, value):
        self._library_view_mode = value
        self._notify("library_view_mode")
        self.emit_event(OpenView.LIBRARY, None)

    @property
    def selected_filter(self):
        return self._selected_filter

    @selected_filter.setter
    def selected_filter(self, value):
        self._selected_filter = value
        self._notify("selected_filter")

    @property
    def is_any_book_recent(self) -> bool:
        return any(book.last_played > 0 for book in self.books)

    @property
    def authors(self):
        is_book_online = self._fs_monitor.get_book_online
        show_offline_books = not self._application_settings.hide_offline

        authors = {
            book.author
            for book in self._model.books
            if is_book_online(book) or show_offline_books or book.downloaded
        }

        return sorted(split_strings_to_set(authors))

    @property
    def readers(self):
        is_book_online = self._fs_monitor.get_book_online
        show_offline_books = not self._application_settings.hide_offline

        readers = {
            book.reader
            for book in self._model.books
            if is_book_online(book) or show_offline_books or book.downloaded
        }

        return sorted(split_strings_to_set(readers))

    @property
    def series(self):
        is_book_online = self._fs_monitor.get_book_online
        show_offline_books = not self._application_settings.hide_offline

        names = set()
        has_books_without_series = False

        for book in self._model.books:
            if not (is_book_online(book) or show_offline_books or book.downloaded):
                continue

            entries = book.series_entries
            if entries:
                names.update(name for name, _ in entries)
            else:
                has_books_without_series = True

        result = sorted(names, key=str.lower)
        if has_books_without_series:
            result.append(NO_SERIES)

        return result

    @property
    def current_book_in_playback(self) -> Optional[Book]:
        return self._player.loaded_book

    @property
    def playing(self) -> bool:
        return self._player.playing

    def remove_book(self, book):
        DeleteBookView(self._on_remove_book_response, book).present()

    def _on_remove_book_response(self, _, response, book):
        if response != "remove":
            return

        if self.book_files_exist(book):
            book.remove()
        else:
            book.remove(delete_db_objects=True)

        self._model.invalidate()
        self._notify("authors")
        self._notify("readers")
        self._notify("books")
        self._notify("books-filter")

    def jump_to_folder(self, book):
        track = False

        # find first chapter with available file,
        # (all this file stuff should probably be moved to the book class)
        for chapter in book.chapters:
            if os.path.exists(chapter.file):
                track = chapter.file
                break

        if track:
            file_launcher = Gtk.FileLauncher(file=Gio.File.new_for_path(track))
            file_launcher.open_containing_folder(
                None, None, lambda d, r: d.open_containing_folder_finish(r)
            )
        else:
            FileNotFoundDialog(book.chapters[0]).present()

    def display_book_filter(self, book_element: BookCard):
        book = book_element.book

        hide_offline_books = self._application_settings.hide_offline
        book_is_online = self._fs_monitor.get_book_online(book)

        if book.hidden or (
            hide_offline_books
            and not ((book_is_online or book.downloaded) and self.book_files_exist(book))
        ):
            return False

        if self.library_view_mode == LibraryViewMode.CURRENT:
            return book.last_played > 0

        if self.selected_filter == _("All"):
            return True
        elif self.library_view_mode == LibraryViewMode.AUTHOR:
            return self.selected_filter in book.author
        elif self.library_view_mode == LibraryViewMode.READER:
            return self.selected_filter in book.reader
        elif self.library_view_mode == LibraryViewMode.SERIES:
            if self.selected_filter == NO_SERIES:
                return not book.series

            return any(name == self.selected_filter for name, _ in book.series_entries)

    def display_book_sort(self, book_element1, book_element2):
        if self.library_view_mode == LibraryViewMode.CURRENT:
            return book_element1.book.last_played < book_element2.book.last_played

        if self.library_view_mode == LibraryViewMode.SERIES:
            first, second = book_element1.book, book_element2.book
            first_name, first_part = self._sort_series(first)
            second_name, second_part = self._sort_series(second)

            if first_name != second_name:
                return first_name > second_name

            if first_part != second_part:
                return first_part > second_part

        return book_element1.book.name.lower() > book_element2.book.name.lower()

    def series_label_for(self, book: Book) -> str:
        if self.library_view_mode != LibraryViewMode.SERIES:
            return ""

        if self.selected_filter in (_("All"), NO_SERIES):
            return book.series_text

        for name, part in book.series_entries:
            if name == self.selected_filter:
                if part is None:
                    return name

                return f"{name} #{part:g}"

        return ""

    def _sort_series(self, book: Book) -> tuple[str, float]:
        selected = self.selected_filter

        for name, part in book.series_entries:
            if selected not in (_("All"), NO_SERIES) and name == selected:
                return name, part if part is not None else 0.0

        if book.series_entries:
            name, part = book.series_entries[0]
            return name, part if part is not None else 0.0

        return "", 0.0

    def open_library(self):
        self._notify("library_view_mode")

    def refresh_books(self):
        self._model.invalidate()
        self._notify("authors")
        self._notify("readers")
        self._notify("series")
        self._notify("books")
        self._notify("books-filter")

    def book_files_exist(self, book: Book) -> bool:
        return any(os.path.isfile(chapter.file) for chapter in book.chapters)

    def _on_fs_monitor_event(self, event, _):
        if event in {"storage-online", "storage-offline"}:
            self._notify("authors")
            self._notify("readers")
            self._notify("books-filter")

    def _on_application_setting_changed(self, event, _):
        if event == "hide-offline":
            self._notify("authors")
            self._notify("readers")
            self._notify("books-filter")
        elif event == "swap-author-reader":
            self._notify("authors")
            self._notify("readers")
            self._notify("books")
            self._notify("books-filter")
        elif event == "prefer-external-cover":
            self._notify("books")

    def _on_importer_event(self, event, message):
        if event == "scan" and message == ScanStatus.SUCCESS:
            self._notify("authors")
            self._notify("readers")
            self._notify("books")
            self._notify("books-filter")
            self._notify("library_view_mode")
        elif event == "import-failed":
            ImportFailedDialog(message).show()

    def _on_player_event(self, event, message):
        if event == "play" and message:
            self._notify("current_book_in_playback")
            self._notify("playing")
            self._notify("books-filter")
        elif event == "pause":
            self._notify("playing")
        elif event == "chapter-changed":
            self._notify("current_book_in_playback")
            self._notify("playing")
        elif event == "stop":
            self._notify("playing")
            self._notify("current_book_in_playback")
        elif event in {"position", "book-finished"}:
            self._notify("book-progress")

    def _on_storages_event(self, event: str, message):
        if event == "storage-removed":
            for property in (
                "authors",
                "readers",
                "books",
                "books-filter",
                "current_book_in_playback",
                "playing",
            ):
                self._notify(property)

    def _on_model_event(self, event: str, message):
        if event == "rebase-finished":
            self.emit_event("work-done")

    def open_book_detail(self, book: Book):
        self.emit_event(OpenView.BOOK, book)

    def play_book(self, book: Book):
        self._player.play_pause_book(book)
