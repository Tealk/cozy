from threading import Thread

import inject
from gi.repository import Adw, GLib, Gtk

from cozy.control.offline_cache import OfflineCache
from cozy.ui.toaster import ToastNotifier


def format_size(size: int) -> str:
    if size < 1024:
        return _("{size} B").format(size=size)

    for unit in ("kB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024:
            return _("{size:.1f} {unit}").format(size=size, unit=unit)

    return _("{size:.1f} PB").format(size=size / 1024)


class OfflineCacheGroup(Adw.PreferencesGroup):
    __gtype_name__ = "OfflineCacheGroup"

    _offline_cache: OfflineCache = inject.attr(OfflineCache)
    _toast: ToastNotifier = inject.attr(ToastNotifier)

    def __init__(self) -> None:
        super().__init__(
            title=_("Offline Cache"),
            description=_("Audiobooks that are stored on this computer for offline playback"),
        )

        self.size_row = Adw.ActionRow(title=_("Cache size"), subtitle=_("Calculating…"))
        self.add(self.size_row)

        self.copy_path_row = Adw.ActionRow(
            title=_("Copy cache folder path"),
            subtitle=str(self._offline_cache.cache_dir),
            activatable=True,
        )
        self.copy_path_row.add_prefix(Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
        self.copy_path_row.connect("activated", self._on_copy_path)
        self.add(self.copy_path_row)

        self.remove_offline_row = Adw.ActionRow(
            title=_("Remove offline books"), subtitle=_("Delete all downloaded audio files")
        )
        self.remove_offline_row.add_prefix(Gtk.Image.new_from_icon_name("edit-delete-symbolic"))
        self.remove_offline_row.set_activatable(True)
        self.remove_offline_row.connect("activated", self._on_remove_offline_books)
        self.add(self.remove_offline_row)

        self.clear_row = Adw.ActionRow(
            title=_("Clear cache"),
            subtitle=_("Delete all cached audio files and reset the download state"),
        )
        self.clear_row.add_prefix(Gtk.Image.new_from_icon_name("edit-clear-all-symbolic"))
        self.clear_row.set_activatable(True)
        self.clear_row.connect("activated", self._on_clear_cache)
        self.add(self.clear_row)

        self._offline_cache.add_listener(self._on_offline_cache_event)
        self.refresh_size()

    def refresh_size(self) -> None:
        def update():
            size = self._offline_cache.get_cache_size()
            books = len(self._offline_cache.get_offline_books())
            GLib.idle_add(
                self._size_row_update,
                format_size(size),
                _("{books} books available offline").format(books=books),
            )

        Thread(target=update, name="OfflineCacheSizeThread", daemon=True).start()

    def _size_row_update(self, size: str, books: str) -> bool:
        self.size_row.set_subtitle(f"{size} · {books}")
        return False

    def _on_offline_cache_event(self, event: str, message) -> None:
        if event == "insufficient-space" and isinstance(message, str):
            GLib.idle_add(self._toast.show, message)
        elif event in {"book-offline", "book-offline-removed", "finished"}:
            self.refresh_size()

    def _on_copy_path(self, *_args):
        self.get_clipboard().set(str(self._offline_cache.cache_dir))
        self._toast.show(_("Cache folder path copied to clipboard"))

    def _on_remove_offline_books(self, *_args):
        def remove():
            self._offline_cache.remove_offline_books()
            GLib.idle_add(self._toast.show, _("Removed all offline books"))
            GLib.idle_add(self.refresh_size)

        Thread(target=remove, name="OfflineCacheRemoveThread", daemon=True).start()

    def _on_clear_cache(self, *_args):
        def clear():
            self._offline_cache.clear_cache()
            GLib.idle_add(self._toast.show, _("Offline cache cleared"))
            GLib.idle_add(self.refresh_size)

        Thread(target=clear, name="OfflineCacheClearThread", daemon=True).start()
