import logging
import os
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

import inject
import requests
from gi.repository import Gio

import cozy.tools as tools
from cozy.architecture.event_sender import EventSender
from cozy.control.application_directories import get_offline_cache_dir
from cozy.db.file import File
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.db.track_to_file import TrackToFile
from cozy.model.book import Book
from cozy.model.chapter import Chapter
from cozy.report import reporter
from cozy.server.playback import is_remote_file, resolve_playback_uri
from cozy.view_model.settings_view_model import SettingsViewModel

log = logging.getLogger("offline_cache")

DOWNLOAD_CHUNK_SIZE = 1024 * 1024
PROGRESS_INTERVAL = 0.2
LOG_PROGRESS_INTERVAL = 5.0
BYTES_PER_MEGABYTE = 1024 * 1024
SUMMARY_INTERVAL = 30.0
SUMMARY_FILE_COUNT = 25


def _url_for_log(url: str) -> str:
    return url.split("?", 1)[0]


def download_remote_file(
    url: str,
    destination: Path,
    cancelled: Callable[[], bool],
    progress_callback: Optional[Callable[[int, int], None]] = None,
    session=None,
) -> bool:
    session = session or requests
    part_path = destination.parent / (destination.name + ".part")

    if cancelled():
        return False

    downloaded = part_path.stat().st_size if part_path.exists() else 0
    headers = {"Range": f"bytes={downloaded}-"} if downloaded else {}
    response = None

    if downloaded:
        log.debug(
            "Resuming download of %s at %.1f MB", _url_for_log(url), downloaded / BYTES_PER_MEGABYTE
        )

    try:
        response = session.get(url, headers=headers, stream=True, timeout=(10, 60))

        if response.status_code not in (200, 206):
            log.warning(
                "Could not download %s: server responded with status %s",
                _url_for_log(url),
                response.status_code,
            )
            return False

        if response.status_code == 200:
            downloaded = 0

        total = downloaded + int(response.headers.get("Content-Length") or 0)
        last_update = 0.0
        last_log = 0.0

        with open(part_path, "ab" if downloaded else "wb") as part_file:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                if not chunk:
                    continue

                if cancelled():
                    log.info(
                        "Download of %s cancelled at %.1f MB",
                        _url_for_log(url),
                        downloaded / BYTES_PER_MEGABYTE,
                    )
                    return False

                part_file.write(chunk)
                downloaded += len(chunk)

                now = time.monotonic()
                if now - last_log >= LOG_PROGRESS_INTERVAL:
                    last_log = now
                    if total > 0:
                        log.debug(
                            "Downloaded %.1f%% of %s (%.1f of %.1f MB)",
                            min(downloaded / total, 1) * 100,
                            _url_for_log(url),
                            downloaded / BYTES_PER_MEGABYTE,
                            total / BYTES_PER_MEGABYTE,
                        )
                    else:
                        log.debug(
                            "Downloaded %.1f MB of %s",
                            downloaded / BYTES_PER_MEGABYTE,
                            _url_for_log(url),
                        )

                if progress_callback and now - last_update >= PROGRESS_INTERVAL:
                    last_update = now
                    try:
                        progress_callback(downloaded, total)
                    except Exception as e:
                        log.warning("Progress callback failed, disabling it: %s", e)
                        progress_callback = None
    except requests.RequestException as e:
        log.warning("Could not download %s: %s", _url_for_log(url), e)
        return False
    finally:
        if response is not None:
            response.close()

    os.replace(part_path, destination)
    log.debug(
        "Finished download of %s (%.1f MB)", _url_for_log(url), downloaded / BYTES_PER_MEGABYTE
    )

    if progress_callback:
        progress_callback(downloaded, downloaded)

    return True


class OfflineCache(EventSender):
    """
    This class is responsible for all actions on the offline cache.
    This includes operations like copying to the cache and adding or removing files from
    the cache.
    """

    queue = []
    total_batch_count = 0
    current_batch_count = 0
    current = None
    thread = None
    filecopy_cancel = None
    last_ui_update = 0
    current_book_processing = None
    current_file_progress = (0, 0)
    files_done = 0
    bytes_done = 0
    last_summary = 0.0

    def __init__(self):
        super().__init__()

        from cozy.media.importer import Importer

        self._importer = inject.instance(Importer)

        from cozy.model.library import Library

        self._library = inject.instance(Library)

        self._importer.add_listener(self._on_importer_event)

        self.cache_dir = get_offline_cache_dir()

        self._start_processing()

        inject.instance(SettingsViewModel).add_listener(self.__on_settings_changed)

    def add(self, book: Book):
        """
        Add all tracks of a book to the offline cache and start copying.
        """
        file_ids = {chapter.file_id for chapter in book.chapters}
        known_ids = {
            entry.original_file.id
            for entry in OfflineCacheModel.select().where(
                OfflineCacheModel.original_file << file_ids
            )
        }
        files_to_cache = []
        for file_id in file_ids - known_ids:
            cached_file_name = str(uuid.uuid4())
            files_to_cache.append((file_id, cached_file_name))
        chunks = [files_to_cache[x:x + 500] for x in range(0, len(files_to_cache), 500)]
        for chunk in chunks:
            query = OfflineCacheModel.insert_many(chunk, fields=[OfflineCacheModel.original_file,
                                                                 OfflineCacheModel.cached_file])
            query.execute()

        self._start_processing()

    def forget_cached_files(self, cached_file_names) -> None:
        for cached_file_name in cached_file_names:
            self._delete_cached_file(cached_file_name)

    def remove(self, book: Book):
        """
        Remove all tracks of the given book from the cache.
        """
        self._stop_processing()
        ids = {t.file_id for t in book.chapters}
        offline_elements = OfflineCacheModel.select().join(File).where(OfflineCacheModel.original_file.id << ids)

        for element in offline_elements:
            self._delete_cached_file(element.cached_file)

            for item in self.queue:
                if self.current and item.id == self.current.id:
                    self.filecopy_cancel.cancel()

        entries_to_delete = OfflineCacheModel.select().join(File).where(OfflineCacheModel.original_file.id << ids)
        ids_to_delete = [t.id for t in entries_to_delete]
        OfflineCacheModel.delete().where(OfflineCacheModel.id << ids_to_delete).execute()
        book.downloaded = False
        self.emit_event("book-offline-removed", book)
        self.queue = []

        self._start_processing()

    def remove_all_for_storage(self, storage):
        for element in OfflineCacheModel.select().join(File).where(
                storage.path in OfflineCacheModel.original_file.path):
            file_path = self.cache_dir / element.cached_file
            if file_path == self.cache_dir:
                continue

            file = Gio.File.new_for_path(str(file_path))
            if file.query_exists():
                file.delete()

        OfflineCacheModel.delete().where(storage.path in OfflineCacheModel.original_file.path).execute()

    def get_cached_path(self, chapter: Chapter) -> Path:
        query = OfflineCacheModel.select().where(OfflineCacheModel.original_file == chapter.file_id,
                                                 OfflineCacheModel.copied)
        if query.count() > 0:
            return self.cache_dir / query.get().cached_file
        else:
            return None

    def get_book_progress(self, book: Book) -> Optional[float]:
        file_ids = {chapter.file_id for chapter in book.chapters}
        if not file_ids:
            return None

        entries = list(
            OfflineCacheModel.select().where(OfflineCacheModel.original_file << file_ids)
        )
        if not entries:
            return None

        copied = sum(1 for entry in entries if entry.copied)
        progress = copied / len(file_ids)

        if self.current and self.current.original_file.id in file_ids:
            current, total = self.current_file_progress
            if total > 0:
                progress += min(current / total, 1) / len(file_ids)

        return min(progress, 1.0)

    def update_cache(self, paths):
        """
        Update the cached version of the given files.
        """
        if OfflineCacheModel.select().count() > 0:
            OfflineCacheModel.update(copied=False).where(
                OfflineCacheModel.original_file.path in paths).execute()
            self._fill_queue_from_db()

    def delete_cache(self):
        """
        Deletes the entire offline cache files.
        Doesn't delete anything from the cozy.db.
        """

        import shutil

        shutil.rmtree(self.cache_dir)

    def _stop_processing(self):
        """ """
        if not self._is_processing() or not self.thread:
            return

        self.filecopy_cancel.cancel()
        self.thread.stop()

    def _start_processing(self):
        """
        """
        if self._is_processing():
            return

        self.thread = tools.StoppableThread(target=self._process_queue)
        self.thread.start()

    def _process_queue(self):
        log.info("Started processing offline cache queue")
        self.filecopy_cancel = Gio.Cancellable()

        self.total_batch_count = 0
        self.current_batch_count = 0
        announced_start = False
        processed_ids = set()

        while True:
            self.total_batch_count += self._fill_queue_from_db(processed_ids)

            if not self.queue or self.thread.stopped():
                break

            if not announced_start:
                self.current_book_processing = self._get_book_to_file(self.queue[0].original_file).id
                self.emit_event_main_thread("start")
                announced_start = True

            self.current_batch_count += 1
            item = self.queue[0]
            processed_ids.add(item.id)

            log.debug("Processing item: %r", item)

            query = OfflineCacheModel.select().where(OfflineCacheModel.id == item.id)
            if not query.exists():
                self.queue.remove(item)
                continue

            new_item = OfflineCacheModel.get(OfflineCacheModel.id == item.id)

            book = self._get_book_to_file(new_item.original_file)
            if self.current_book_processing != book.id:
                self._update_book_download_status(self.current_book_processing)
                self.current_book_processing = book.id
                self.files_done = 0
                self.bytes_done = 0
                self.last_summary = time.monotonic()

            if not new_item.copied:
                if is_remote_file(new_item.original_file.path):
                    self._download_remote_file(new_item, book)
                elif os.path.exists(new_item.original_file.path):
                    self._copy_local_file(new_item, book)

            self.queue.remove(item)

        if self.current_book_processing:
            self._update_book_download_status(self.current_book_processing)

        self.current = None
        self.current_file_progress = (0, 0)
        self.emit_event_main_thread("finished")

    def _copy_local_file(self, new_item, book: Book) -> None:
        log.info("Copying item: %r", new_item)
        self.emit_event_main_thread("message", _("Copying") + " " + book.name)
        self.current = new_item
        self.current_file_progress = (0, 1)

        destination = Gio.File.new_for_path(os.path.join(self.cache_dir, new_item.cached_file))
        source = Gio.File.new_for_path(new_item.original_file.path)
        flags = Gio.FileCopyFlags.OVERWRITE
        try:
            copied = source.copy(destination, flags, self.filecopy_cancel, self.__update_copy_status, None)
        except Exception as e:
            if e.code == Gio.IOErrorEnum.CANCELLED:
                log.info("Download of book was cancelled.")
                self.thread.stop()
                return
            reporter.exception("offline_cache", e)
            log.error("Could not copy file %r to offline cache: %s", new_item.original_file.path, e)
            return

        if copied:
            OfflineCacheModel.update(copied=True).where(
                OfflineCacheModel.id == new_item.id).execute()

    def _download_remote_file(self, new_item, book: Book) -> None:
        log.debug(
            "Downloading %s (file %d of %d)",
            _url_for_log(new_item.original_file.path),
            self.current_batch_count,
            max(self.total_batch_count, 1),
        )
        self.emit_event_main_thread("message", _("Downloading") + " " + book.name)
        self.current = new_item
        self.current_file_progress = (0, 0)

        destination = self.cache_dir / new_item.cached_file

        try:
            downloaded = download_remote_file(
                resolve_playback_uri(new_item.original_file.path),
                destination,
                cancelled=lambda: self.thread.stopped(),
                progress_callback=lambda current, total: self.__update_copy_status(
                    current, total, None
                ),
            )
        except Exception as e:
            reporter.exception("offline_cache", e)
            log.error("Could not download %r to offline cache: %s", new_item.original_file.path, e)
            return

        if downloaded:
            OfflineCacheModel.update(copied=True).where(
                OfflineCacheModel.id == new_item.id).execute()
            self._count_downloaded_file(book, destination)
        else:
            log.info("Download of %r was cancelled or failed.", new_item.original_file.path)

    def _count_downloaded_file(self, book: Book, destination: Path) -> None:
        self.files_done += 1
        self.bytes_done += destination.stat().st_size

        now = time.monotonic()
        due_files = self.files_done % SUMMARY_FILE_COUNT == 0
        if not due_files and now - self.last_summary < SUMMARY_INTERVAL:
            return

        self.last_summary = now
        log.info(
            "Downloading %s: file %d of %d (%.1f MB)",
            book.name,
            self.files_done,
            max(self.total_batch_count, 1),
            self.bytes_done / BYTES_PER_MEGABYTE,
        )

    def _delete_cached_file(self, cached_file_name: str) -> None:
        for path in (
            self.cache_dir / cached_file_name,
            self.cache_dir / (cached_file_name + ".part"),
        ):
            file = Gio.File.new_for_path(str(path))
            if file.query_exists():
                file.delete()

    def _get_book_to_file(self, file: File):
        track_to_file = TrackToFile.select().join(File).where(TrackToFile.file == file.id).get()
        return track_to_file.track.book

    def _update_book_download_status(self, book_id):
        book = next(book for book in self._library.books if book.id == book_id)
        downloaded = self._is_book_downloaded(book)

        book.downloaded = downloaded

        if downloaded:
            self.emit_event("book-offline", book)
        else:
            self.emit_event("book-offline-removed", book)

    def _is_book_downloaded(self, book: Book):
        file_ids = [chapter.file_id for chapter in book.chapters]
        offline_files = OfflineCacheModel.select().where(
            (OfflineCacheModel.original_file << file_ids) & OfflineCacheModel.copied
        )
        offline_file_ids = [file.original_file.id for file in offline_files]

        return all(chapter.file_id in offline_file_ids for chapter in book.chapters)

    def _is_processing(self):
        """
        """
        if self.thread:
            return self.thread.is_alive()
        else:
            return False

    def _fill_queue_from_db(self, processed_ids=None) -> int:
        processed_ids = processed_ids if processed_ids is not None else set()
        added = 0
        pending = OfflineCacheModel.select().where(~OfflineCacheModel.copied)
        for item in pending:
            if item.id in processed_ids:
                continue

            if not any(item.id == queued.id for queued in self.queue):
                self.queue.append(item)
                added += 1

        return added

    def _on_importer_event(self, event: str, message):
        if event == "new-or-updated-files":
            self.update_cache(message)
            self._start_processing()

    def __update_copy_status(self, current_num_bytes, total_num_bytes, _):
        self.current_file_progress = (current_num_bytes, total_num_bytes)
        total_batch_count = max(self.total_batch_count, 1)

        finished_files_progress = (self.current_batch_count - 1) / total_batch_count
        current_file_progress = current_num_bytes / max(total_num_bytes, 1)

        progress = finished_files_progress + (current_file_progress / total_batch_count)
        self.emit_event_main_thread("progress", min(progress, 1))

    def __on_settings_changed(self, event, message):
        if event == "storage-removed" or event == "external-storage-removed":
            self.remove_all_for_storage(message)
