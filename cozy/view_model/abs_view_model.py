import logging
from threading import Thread
from typing import Optional

import inject
import requests
from gi.repository import GLib

from cozy.architecture.event_sender import EventSender
from cozy.architecture.observable import Observable
from cozy.control import secrets
from cozy.control.offline_cache import OfflineCache
from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.model.library import Library
from cozy.server.abs_importer import AbsImporter
from cozy.server.audiobookshelf_client import AudiobookshelfClient, AudiobookshelfError
from cozy.view_model.library_view_model import LibraryViewModel

log = logging.getLogger("abs_view_model")


class AbsViewModel(Observable, EventSender):
    _library: Library = inject.attr(Library)
    _library_view_model: LibraryViewModel = inject.attr(LibraryViewModel)
    _offline_cache: OfflineCache = inject.attr(OfflineCache)

    def __init__(self) -> None:
        super().__init__()
        super(Observable, self).__init__()

    @property
    def servers(self) -> list[AudiobookshelfServer]:
        return list(AudiobookshelfServer.select())

    def add_server(
        self,
        name: str,
        url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
    ) -> Optional[str]:
        client = AudiobookshelfClient(url, token=token, username=username, password=password)
        server = AudiobookshelfServer(name=name, url=url)

        try:
            real_token = client.authorize()
            library_id = self._default_book_library(client)
        except (AudiobookshelfError, KeyError, requests.RequestException) as e:
            log.error("Could not add server %s: %s", url, e)
            return str(e)

        server.save()
        secrets.store_server_token(server.id, real_token)
        server.username = username
        server.library_id = library_id
        server.save()

        self._notify("servers")
        return None

    def sync(self, server: AudiobookshelfServer) -> None:
        Thread(target=self._sync_background, args=(server.id,), name="AbsSyncThread").start()

    def remove(self, server: AudiobookshelfServer) -> None:
        for mapping in AudiobookshelfBook.select().where(AudiobookshelfBook.server == server.id):
            book = next((b for b in self._library.books if b.id == mapping.book_id), None)
            if book is not None:
                book.remove(delete_db_objects=True)
            mapping.delete_instance()

        secrets.clear_server_token(server.id)
        server.delete_instance()

        self._notify("servers")

    def _sync_background(self, server_id: int) -> None:
        server = AudiobookshelfServer.get_or_none(server_id)
        if server is None:
            return

        token = secrets.get_server_token(server.id)
        client = AudiobookshelfClient(server.url, token=token)

        try:
            result = AbsImporter(client, server).sync()
        except (AudiobookshelfError, requests.RequestException) as e:
            log.error("Sync failed for server %s: %s", server.url, e)
            self.emit_event_main_thread("sync-failed", str(e))
            return

        self._apply_offline_cache_changes(result)
        self.emit_event_main_thread("sync-finished", (server, result))
        GLib.idle_add(self._library_view_model.refresh_books)

    def _apply_offline_cache_changes(self, result) -> None:
        self._offline_cache.forget_cached_files(result.removed_cached_files)

        for book_id in result.changed_books:
            book = next((book for book in self._library.books if book.id == book_id), None)
            if book is None or not book.offline:
                continue

            book.downloaded = False
            self._offline_cache.add(book)

    @staticmethod
    def _default_book_library(client: AudiobookshelfClient) -> str:
        libraries = client.get_libraries()
        book_libraries = [library for library in libraries if library.get("mediaType") == "book"]
        default = book_libraries or libraries
        return default[0]["id"] if default else ""
