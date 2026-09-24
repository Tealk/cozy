import pytest

from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.db.book import Book
from cozy.db.file import File
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.db.track import Track
from cozy.db.track_to_file import TrackToFile
from cozy.server.abs_importer import AbsImporter

BASE_URL = "http://abs.local:13378"

ITEM = {
    "id": "li_1",
    "libraryId": "lib_1",
    "updatedAt": 100,
    "media": {
        "metadata": {
            "title": "The Book",
            "authorName": "Author One",
            "narratorName": "Narrator One",
        },
        "chapters": [
            {"id": 0, "title": "Chapter 1", "start": 0, "end": 600},
            {"id": 1, "title": "Chapter 2", "start": 600, "end": 1200},
        ],
        "audioTracks": [
            {
                "index": 1,
                "startOffset": 0,
                "duration": 600,
                "title": "01-book.mp3",
                "contentUrl": "/s/item/li_1/01-book.mp3",
                "metadata": {"filename": "01-book.mp3"},
            },
            {
                "index": 2,
                "startOffset": 600,
                "duration": 600,
                "title": "02-book.mp3",
                "contentUrl": "/s/item/li_1/02-book.mp3",
                "metadata": {"filename": "02-book.mp3"},
            },
        ],
    },
}


class FakeClient:
    def __init__(self, items, details=None, cover=b"cover-bytes"):
        self.base_url = BASE_URL
        self.items = items
        self.details = details or {}
        self.cover = cover

    def get_library_items(self, library_id):
        return self.items

    def get_item(self, item_id):
        return self.details.get(item_id, {})

    def get_cover(self, item_id):
        return self.cover


@pytest.fixture
def server(peewee_database):
    return AudiobookshelfServer.create(name="abs", url=BASE_URL, library_id="lib_1")


def test_sync_imports_books_tracks_and_files(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    importer = AbsImporter(client, server)

    result = importer.sync()

    assert result.created == 1
    assert result.skipped == 0

    book = AudiobookshelfBook.get().book
    assert book.name == "The Book"
    assert book.author == "Author One"
    assert book.reader == "Narrator One"
    assert book.cover == b"cover-bytes"
    assert book.hidden is False

    tracks = Track.select().where(Track.book == book).order_by(Track.number)
    assert [TrackToFile.get(TrackToFile.track == track).file.path for track in tracks] == [
        f"{BASE_URL}/s/item/li_1/01-book.mp3",
        f"{BASE_URL}/s/item/li_1/02-book.mp3",
    ]
    assert tracks[0].name == "Chapter 1"
    assert tracks[0].number == 1
    assert tracks[0].length == 600
    assert TrackToFile.get(TrackToFile.track == tracks[0]).start_at == 0
    assert tracks[1].name == "Chapter 2"
    assert tracks[1].number == 2
    assert tracks[1].length == 600
    assert TrackToFile.get(TrackToFile.track == tracks[1]).start_at == 0


def test_sync_applies_progress_from_server(server):
    progressed_item = {
        **ITEM,
        "updatedAt": 500,
        "media": {
            **ITEM["media"],
            "progress": {"currentTime": 250, "duration": 1200},
        },
    }
    client = FakeClient([progressed_item], details={"li_1": progressed_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    tracks = list(Track.select().where(Track.book == book).order_by(Track.number))
    assert book.position == tracks[0].id
    assert tracks[0].position == 250_000_000_000


def test_sync_marks_book_finished_when_progress_at_end(server):
    finished_item = {
        **ITEM,
        "updatedAt": 600,
        "media": {
            **ITEM["media"],
            "progress": {"currentTime": 1200, "duration": 1200},
        },
    }
    client = FakeClient([finished_item], details={"li_1": finished_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.position == -1


def test_sync_skips_unchanged_books(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    importer = AbsImporter(client, server)

    first = importer.sync()
    second = importer.sync()

    assert first.created == 1
    assert second.skipped == 1
    assert AudiobookshelfBook.select().count() == 1


def test_sync_updates_changed_books(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    importer = AbsImporter(client, server)
    importer.sync()

    changed_item = {
        **ITEM,
        "updatedAt": 200,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "title": "The Book v2"},
        },
    }
    client.details["li_1"] = changed_item
    client.items = [changed_item]

    result = importer.sync()

    assert result.updated == 1
    book = AudiobookshelfBook.get().book
    assert book.name == "The Book v2"


def test_sync_hides_books_removed_on_server(server):
    second_item = {
        **ITEM,
        "id": "li_2",
        "updatedAt": 200,
        "media": {**ITEM["media"], "metadata": {**ITEM["media"]["metadata"], "title": "Other"}},
    }
    first_items = [ITEM, second_item]
    client = FakeClient(first_items, details={"li_1": ITEM, "li_2": second_item})
    AbsImporter(client, server).sync()

    client.items = [second_item]
    client.details.pop("li_1")

    result = AbsImporter(client, server).sync()

    assert result.removed == 1
    assert Book.get(Book.name == "The Book").hidden is True
    assert Book.get(Book.name == "Other").hidden is False


def test_sync_uses_filenames_when_no_chapters(server):
    no_chapter_item = {
        "id": "li_2",
        "updatedAt": 300,
        "media": {
            "metadata": {
                "title": "No Chapters",
                "authorName": "Author",
                "narratorName": "Narrator",
            },
            "chapters": [],
            "audioTracks": [
                {
                    "index": 1,
                    "startOffset": 0,
                    "duration": 300,
                    "title": "01.mp3",
                    "contentUrl": "/s/item/li_2/01.mp3",
                    "metadata": {"filename": "01.mp3"},
                }
            ],
        },
    }
    client = FakeClient([no_chapter_item], details={"li_2": no_chapter_item})

    AbsImporter(client, server).sync()

    mapping = AudiobookshelfBook.get()
    track = Track.get(Track.book == mapping.book)
    assert track.name == "01.mp3"
    assert track.number == 1
    assert track.length == 300
    assert TrackToFile.get(TrackToFile.track == track).start_at == 0


def test_sync_imports_tracks_field_with_per_track_chapters(server):
    tracks_item = {
        **ITEM,
        "id": "li_3",
        "updatedAt": 400,
        "media": {
            "metadata": {
                "title": "Tracks Field",
                "authorName": "Author",
                "narratorName": "Narrator",
            },
            "chapters": [],
            "tracks": [
                {
                    "index": 1,
                    "startOffset": 0,
                    "duration": 600,
                    "title": "01.mp3",
                    "contentUrl": "/s/item/li_3/01.mp3",
                    "metadata": {"filename": "01.mp3"},
                    "chapters": [
                        {"title": "Track Chapter", "start": 0, "end": 600},
                    ],
                }
            ],
        },
    }
    client = FakeClient([tracks_item], details={"li_3": tracks_item})

    AbsImporter(client, server).sync()

    mapping = AudiobookshelfBook.get()
    track = Track.get(Track.book == mapping.book)
    assert track.name == "Track Chapter"
    assert track.number == 1
    assert track.length == 600
    assert TrackToFile.get(TrackToFile.track == track).start_at == 0


def _changed_tracks_item(extra_track: bool):
    tracks = []
    for track in ITEM["media"]["audioTracks"]:
        tracks.append(
            {
                **track,
                "chapters": [
                    {"title": track["title"], "start": track["startOffset"],
                     "end": track["startOffset"] + track["duration"]}
                ],
            }
        )

    if extra_track:
        tracks.append(
            {
                "index": 3,
                "startOffset": 0,
                "duration": 300,
                "title": "03-book.mp3",
                "contentUrl": "/s/item/li_1/03-book.mp3",
                "metadata": {"filename": "03-book.mp3"},
                "chapters": [{"title": "03-book.mp3", "start": 0, "end": 300}],
            }
        )

    return {
        **ITEM,
        "updatedAt": 500,
        "media": {
            **ITEM["media"],
            "chapters": [],
            "audioTracks": tracks,
        },
    }


def test_sync_reports_changed_file_set(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    changed_item = _changed_tracks_item(extra_track=True)
    client.items = [changed_item]
    client.details["li_1"] = changed_item

    result = AbsImporter(client, server).sync()

    assert result.updated == 1
    assert result.changed_books == [AudiobookshelfBook.get().book.id]


def test_sync_does_not_report_changed_file_set_when_files_stay_equal(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    changed_item = _changed_tracks_item(extra_track=False)
    client.items = [changed_item]
    client.details["li_1"] = changed_item

    result = AbsImporter(client, server).sync()

    assert result.updated == 1
    assert result.changed_books == []


def test_sync_reports_removed_cached_files(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    file = File.get(File.path == BASE_URL + "/s/item/li_1/02-book.mp3")
    OfflineCacheModel.create(original_file=file, cached_file="cached-2", copied=True)

    client.items = [{**ITEM, "updatedAt": 500}]
    client.details["li_1"] = {
        **ITEM,
        "updatedAt": 500,
        "media": {
            **ITEM["media"],
            "audioTracks": [ITEM["media"]["audioTracks"][0]],
        },
    }

    result = AbsImporter(client, server).sync()

    assert result.removed_cached_files == ["cached-2"]
    assert not OfflineCacheModel.select().where(
        OfflineCacheModel.cached_file == "cached-2"
    ).exists()
    assert not File.select().where(File.path == BASE_URL + "/s/item/li_1/02-book.mp3").exists()
