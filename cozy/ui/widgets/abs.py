from threading import Thread

import inject
from gi.repository import Adw, GLib, Gtk

from cozy.db.abs_server import AudiobookshelfServer
from cozy.ui.toaster import ToastNotifier
from cozy.view_model.abs_view_model import AbsViewModel


class AbsServerRow(Adw.ActionRow):
    def __init__(self, server: AudiobookshelfServer) -> None:
        self.server = server

        super().__init__(title=server.name or server.url)

        subtitle = server.url
        if server.library_id:
            subtitle += " · " + server.library_id
        self.set_subtitle(subtitle)

        self.sync_button = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER)
        self.sync_button.set_tooltip_text(_("Sync"))
        self.add_suffix(self.sync_button)

        self.remove_button = Gtk.Button(icon_name="edit-delete-symbolic", valign=Gtk.Align.CENTER)
        self.remove_button.set_tooltip_text(_("Remove server"))
        self.add_suffix(self.remove_button)


class AbsServers(Adw.PreferencesGroup):
    __gtype_name__ = "AbsServers"

    _view_model: AbsViewModel = inject.attr(AbsViewModel)
    _toast: ToastNotifier = inject.attr(ToastNotifier)

    def __init__(self) -> None:
        super().__init__(
            title=_("Servers"), description=_("Stream audiobooks from an Audiobookshelf server")
        )

        self._view_model.bind_to("servers", self._reload)
        self._view_model.add_listener(self._on_sync_event)

        self._server_list = Gtk.ListBox()
        self._server_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.add(self._server_list)

        self._new_server_button = Adw.ButtonRow(title=_("Add Server"))
        self._new_server_button.set_activatable(True)
        self._new_server_button.set_end_icon_name("list-add-symbolic")
        self._new_server_button.connect("activated", self._on_add_server)
        self.add(self._new_server_button)

        self._reload()

    def _reload(self) -> None:
        self._server_list.remove_all()

        for server in self._view_model.servers:
            row = AbsServerRow(server)
            row.sync_button.connect("clicked", self._on_sync, server)
            row.remove_button.connect("clicked", self._on_remove, server)
            self._server_list.append(row)

    def _on_add_server(self, *_):
        dialog = AbsServerDialog(self._view_model, self._toast)
        dialog.present(self.get_root())

    def _on_sync(self, button: Gtk.Button, server: AudiobookshelfServer) -> None:
        button.set_sensitive(False)
        self._view_model.sync(server)

    def _on_remove(self, _, server: AudiobookshelfServer) -> None:
        self._view_model.remove(server)

    def _on_sync_event(self, event: str, message) -> None:
        if event == "sync-finished":
            server, result = message
            count = result.created + result.updated
            self._toast.show(
                _("Synced {count} books from {name}").format(count=count, name=server.name)
            )
            self._reload()
        elif event == "sync-failed":
            self._toast.show(_("Synchronization failed: ") + message)
            self._reload()


class AbsServerDialog(Adw.Dialog):
    def __init__(self, view_model: AbsViewModel, toast: ToastNotifier) -> None:
        super().__init__()

        self._view_model = view_model
        self._toast = toast

        self.set_title(_("Add Audiobookshelf Server"))
        self._build()

    def _build(self) -> None:
        self.name_entry = Adw.EntryRow(title=_("Name"))
        self.url_entry = Adw.EntryRow(title=_("URL"))
        self.username_entry = Adw.EntryRow(title=_("Username"))
        self.password_entry = Adw.PasswordEntryRow(title=_("Password"))

        entry_list = Gtk.ListBox()
        entry_list.set_selection_mode(Gtk.SelectionMode.NONE)
        entry_list.append(self.name_entry)
        entry_list.append(self.url_entry)
        entry_list.append(self.username_entry)
        entry_list.append(self.password_entry)

        self.add_button = Gtk.Button(label=_("Add"), halign=Gtk.Align.END)
        self.add_button.add_css_class("suggested-action")
        self.add_button.connect("clicked", self._on_add)

        self.spinner = Gtk.Spinner()

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.set_halign(Gtk.Align.END)
        action_box.append(self.spinner)
        action_box.append(self.add_button)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.append(entry_list)
        box.append(action_box)

        self.set_child(box)

    def _on_add(self, *_args):
        name = self.name_entry.get_text().strip()
        url = self.url_entry.get_text().strip()

        if not name or not url:
            self._toast.show(_("Name and URL are required"))
            return

        username = self.username_entry.get_text().strip() or None
        password = self.password_entry.get_text() or None

        self.add_button.set_sensitive(False)
        self.spinner.start()
        Thread(
            target=self._add_background,
            args=(name, url, username, password),
            name="AbsAddServerThread",
        ).start()

    def _add_background(self, name: str, url: str, username, password) -> None:
        error = self._view_model.add_server(name, url, username, password)
        GLib.idle_add(self._on_add_done, error)

    def _on_add_done(self, error) -> None:
        self.spinner.stop()

        if error:
            self.add_button.set_sensitive(True)
            self._toast.show(_("Could not add server: ") + error)
            return

        self.close()

        servers = self._view_model.servers
        if servers:
            self._view_model.sync(servers[-1])
