import os
from os import path

from gi.repository import Adw, GObject, Gtk

from cozy.control.time_format import ns_to_time
from cozy.model.chapter import Chapter
from cozy.server.playback import is_remote_file


@Gtk.Template.from_resource("/com/github/geigi/cozy/ui/chapter_element.ui")
class ChapterElement(Adw.ActionRow):
    __gtype_name__ = "ChapterElement"

    icon_stack: Gtk.Stack = Gtk.Template.Child()
    play_icon: Gtk.Image = Gtk.Template.Child()
    number_label: Gtk.Label = Gtk.Template.Child()
    duration_label: Gtk.Label = Gtk.Template.Child()

    def __init__(self, chapter: Chapter):
        super().__init__()

        self.chapter = chapter

        self.connect("activated", self._on_button_press)

        self.set_title(self.chapter.name)
        self.number_label.set_text(str(self.chapter.number))

        if not os.path.exists(chapter.file) and not is_remote_file(chapter.file):
            self.duration_label.set_text(_("File not Found"))
        else:
            self.duration_label.set_text(ns_to_time(self.chapter.length))
            self.set_tooltip_text(path.basename(chapter.file))

    @GObject.Signal(arg_types=(object,))
    def play_pause_clicked(self, *_): ...

    def _on_button_press(self, *_):
        self.emit("play-pause-clicked", self.chapter)

    def select(self):
        self.icon_stack.set_visible_child_name("icon")

    def deselect(self):
        self.icon_stack.set_visible_child_name("number")

    def set_playing(self, playing):
        if playing:
            self.play_icon.set_from_icon_name("media-playback-pause-symbolic")
        else:
            self.play_icon.set_from_icon_name("media-playback-start-symbolic")
