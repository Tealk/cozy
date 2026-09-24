import pytest

from cozy.db.abs_server import AudiobookshelfBook, AudiobookshelfServer
from cozy.db.book import Book
from cozy.db.file import File
from cozy.db.offline_cache import OfflineCache as OfflineCacheModel
from cozy.db.track import Track
from cozy.db.track_to_file import TrackToFile
from cozy.server.abs_importer import AbsImporter
from cozy.server.audiobookshelf_client import AudiobookshelfError

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


def test_sync_skips_items_that_fail(server):
    class FailingClient(FakeClient):
        def get_item(self, item_id):
            if item_id == "li_1":
                raise AudiobookshelfError("status 503")

            return super().get_item(item_id)

    other_item = {
        **ITEM,
        "id": "li_2",
        "updatedAt": 700,
        "media": {**ITEM["media"], "metadata": {**ITEM["media"]["metadata"], "title": "Second"}},
    }
    client = FailingClient([ITEM, other_item], details={"li_2": other_item})

    result = AbsImporter(client, server).sync()

    assert result.failed_items == ["The Book"]
    assert result.created == 1
    assert AudiobookshelfBook.select().count() == 1
    assert AudiobookshelfBook.get().book.name == "Second"


def test_sync_uses_item_id_when_title_is_missing(server):
    class FailingClient(FakeClient):
        def get_item(self, item_id):
            raise AudiobookshelfError("status 500")

    item_without_title = {**ITEM, "media": {"metadata": {}, "audioTracks": []}}
    client = FailingClient([item_without_title], details={})

    result = AbsImporter(client, server).sync()

    assert result.failed_items == ["li_1"]


def test_sync_imports_book_metadata(server):
    metadata_item = {
        **ITEM,
        "id": "li_meta",
        "updatedAt": 800,
        "media": {
            **ITEM["media"],
            "metadata": {
                "title": "Die Krone",
                "authorName": "Author",
                "narratorName": "Narrator",
                "series": "Die Kronen Chroniken",
                "seriesPart": "2.5",
                "description": "Ein Hörbuch.",
                "publisher": "Verlag",
                "publishedYear": "2019-05-04",
                "language": "German",
                "asin": "B000000000",
            },
        },
    }
    client = FakeClient([metadata_item], details={"li_meta": metadata_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Die Kronen Chroniken #2.5"
    assert book.series_part == 2.5
    assert book.description == "Ein Hörbuch."
    assert book.publisher == "Verlag"
    assert book.published_year == 2019
    assert book.language == "German"
    assert book.asin == "B000000000"


def test_sync_clears_metadata_when_abs_has_none(server):
    item = {**ITEM, "id": "li_plain", "updatedAt": 900}
    client = FakeClient([item], details={"li_plain": item})

    AbsImporter(client, server).sync()
    first_book = AudiobookshelfBook.get().book

    rich_item = {
        **ITEM,
        "id": "li_plain",
        "updatedAt": 1000,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "series": "Alpha", "publisher": "Verlag"},
        },
    }
    client.items = [rich_item]
    client.details["li_plain"] = rich_item
    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.series == "Alpha"

    client.items = [item]
    client.details["li_plain"] = item
    AbsImporter(client, server).sync()

    refreshed = AudiobookshelfBook.get().book
    assert refreshed.id == first_book.id
    assert refreshed.series is None
    assert refreshed.publisher is None


def test_sync_applies_metadata_for_unchanged_items(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    enriched_item = {
        **ITEM,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "series": "Alpha", "publisher": "Verlag"},
        },
    }
    client.items = [enriched_item]

    result = AbsImporter(client, server).sync()

    assert result.skipped == 1
    assert result.updated == 0
    book = AudiobookshelfBook.get().book
    assert book.series == "Alpha"
    assert book.publisher == "Verlag"


def test_sync_reports_unchanged_books(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    result = AbsImporter(client, server).sync()

    assert result.created == 0
    assert result.updated == 0
    assert result.skipped == 1
    assert result.total == 1


def test_sync_imports_series_from_list_payload(server):
    series_item = {
        **ITEM,
        "id": "li_series",
        "updatedAt": 1100,
        "media": {
            **ITEM["media"],
            "metadata": {
                **ITEM["media"]["metadata"],
                "series": [{"name": "Die Kronen Chroniken", "sequence": "3"}],
            },
        },
    }
    client = FakeClient([series_item], details={"li_series": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Die Kronen Chroniken #3"
    assert book.series_part == 3.0


def test_sync_uses_first_series_of_multiple_entries(server):
    series_item = {
        **ITEM,
        "id": "li_multi",
        "updatedAt": 1200,
        "media": {
            **ITEM["media"],
            "metadata": {
                **ITEM["media"]["metadata"],
                "series": [
                    {"name": "Alpha", "sequence": "1"},
                    {"name": "Beta", "sequence": "2"},
                ],
            },
        },
    }
    client = FakeClient([series_item], details={"li_multi": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Alpha #1, Beta #2"
    assert book.series_part == 1.0


def test_sync_handles_plain_string_series(server):
    series_item = {
        **ITEM,
        "id": "li_str",
        "updatedAt": 1300,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "series": "Alpha", "seriesPart": "2"},
        },
    }
    client = FakeClient([series_item], details={"li_str": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Alpha #2"
    assert book.series_part == 2.0


def test_sync_backfills_missing_metadata_from_detail(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    detailed_item = {
        **ITEM,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "series": "Alpha", "asin": "B000000000"},
        },
    }
    client.items = [ITEM]
    client.details["li_1"] = detailed_item

    result = AbsImporter(client, server).sync()

    assert result.skipped == 1
    assert result.metadata_backfilled == 1
    book = AudiobookshelfBook.get().book
    assert book.series == "Alpha"
    assert book.asin == "B000000000"


def test_sync_does_not_backfill_when_metadata_is_present(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    detailed_item = {
        **ITEM,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "series": "Alpha"},
        },
    }
    client.items = [detailed_item]
    client.details["li_1"] = detailed_item
    AbsImporter(client, server).sync()

    class CountingClient(FakeClient):
        def get_item(self, item_id):
            raise AssertionError("detail request should not happen")

    counting_client = CountingClient([detailed_item], details={"li_1": detailed_item})
    result = AbsImporter(counting_client, server).sync()

    assert result.metadata_backfilled == 0


def test_sync_reads_series_from_media(server):
    series_item = {
        **ITEM,
        "id": "li_media_series",
        "updatedAt": 1400,
        "media": {
            **ITEM["media"],
            "series": [{"name": "Die Zwerge", "sequence": "2"}],
        },
    }
    client = FakeClient([series_item], details={"li_media_series": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Die Zwerge #2"
    assert book.series_part == 2.0


def test_sync_prefers_media_series_over_metadata_series(server):
    series_item = {
        **ITEM,
        "id": "li_both",
        "updatedAt": 1500,
        "media": {
            **ITEM["media"],
            "series": [{"name": "Aus Media", "sequence": "1"}],
            "metadata": {
                **ITEM["media"]["metadata"],
                "series": "Aus Metadaten",
                "seriesPart": "9",
            },
        },
    }
    client = FakeClient([series_item], details={"li_both": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Aus Media #1"
    assert book.series_part == 1.0


def test_sync_splits_series_name_with_part(server):
    series_item = {
        **ITEM,
        "id": "li_series_name",
        "updatedAt": 1600,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "seriesName": "Tiffany #5"},
        },
    }
    client = FakeClient([series_item], details={"li_series_name": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Tiffany #5"
    assert book.series_part == 5.0


def test_sync_handles_series_name_without_part(server):
    series_item = {
        **ITEM,
        "id": "li_series_plain",
        "updatedAt": 1700,
        "media": {
            **ITEM["media"],
            "metadata": {**ITEM["media"]["metadata"], "seriesName": "Die Zwerge"},
        },
    }
    client = FakeClient([series_item], details={"li_series_plain": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Die Zwerge"
    assert book.series_part is None


def test_sync_keeps_parts_of_all_series(server):
    series_item = {
        **ITEM,
        "id": "li_multi_parts",
        "updatedAt": 1800,
        "media": {
            **ITEM["media"],
            "metadata": {
                **ITEM["media"]["metadata"],
                "seriesName": "Scheibenwelt #4, Tod #1",
            },
        },
    }
    client = FakeClient([series_item], details={"li_multi_parts": series_item})

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    assert book.series == "Scheibenwelt #4, Tod #1"
    assert book.series_part == 4.0


def test_sync_keeps_position_when_server_has_no_progress(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()
    book = AudiobookshelfBook.get().book
    track = Track.get(Track.book == book)
    book.position = track.id
    book.save(only=[Book.position])

    changed_item = {**ITEM, "updatedAt": 1900}
    client.items = [changed_item]
    client.details["li_1"] = changed_item

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == track.id


def test_sync_keeps_finished_position_when_files_are_unchanged(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()
    book = AudiobookshelfBook.get().book
    book.position = -1
    book.save(only=[Book.position])

    changed_item = {**ITEM, "updatedAt": 2000}
    client.items = [changed_item]
    client.details["li_1"] = changed_item

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == -1


def test_sync_resets_position_when_files_changed_without_progress(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()
    book = AudiobookshelfBook.get().book
    book.position = -1
    book.save(only=[Book.position])

    shorter_item = {
        **ITEM,
        "updatedAt": 2100,
        "media": {
            **ITEM["media"],
            "audioTracks": [ITEM["media"]["audioTracks"][0]],
        },
    }
    client.items = [shorter_item]
    client.details["li_1"] = shorter_item

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == 0


def test_force_sync_reimports_unchanged_items(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    client.details["li_1"] = {
        **ITEM,
        "media": {**ITEM["media"], "metadata": {**ITEM["media"]["metadata"], "series": "Alpha"}},
    }

    result = AbsImporter(client, server).sync(force=True)

    assert result.skipped == 0
    assert result.updated == 1
    assert AudiobookshelfBook.get().book.series == "Alpha"


def test_force_sync_resets_progress_without_server_data(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()
    book = AudiobookshelfBook.get().book
    book.position = -1
    book.save(only=[Book.position])

    AbsImporter(client, server).sync(force=True)

    assert AudiobookshelfBook.get().book.position == 0


def test_normal_sync_keeps_progress_without_server_data(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()
    book = AudiobookshelfBook.get().book
    book.position = -1
    book.save(only=[Book.position])

    changed_item = {**ITEM, "updatedAt": 2200}
    client.items = [changed_item]
    client.details["li_1"] = changed_item

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == -1


class ProgressClient(FakeClient):
    def __init__(self, items, details=None, cover=b"cover-bytes", progress=None):
        super().__init__(items, details, cover)
        self.progress = progress or {}

    def get_progress(self):
        return self.progress


def test_sync_applies_progress_from_bulk_endpoint(server):
    client = ProgressClient(
        [ITEM],
        details={"li_1": ITEM},
        progress={"li_1": {"currentTime": 300, "duration": 1200}},
    )

    AbsImporter(client, server).sync()

    book = AudiobookshelfBook.get().book
    track = Track.get(Track.book == book)
    assert book.position == track.id
    assert track.position == 300 * 1_000_000_000


def test_sync_marks_book_finished_from_bulk_endpoint(server):
    client = ProgressClient(
        [ITEM],
        details={"li_1": ITEM},
        progress={"li_1": {"currentTime": 1200, "duration": 1200}},
    )

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == -1


def test_sync_survives_missing_bulk_endpoint(server):
    class NoProgressClient(FakeClient):
        def get_progress(self):
            raise AudiobookshelfError("status 404")

    client = NoProgressClient([ITEM], details={"li_1": ITEM})

    result = AbsImporter(client, server).sync()

    assert result.created == 1
    assert AudiobookshelfBook.get().book.position == 0


def test_sync_marks_book_finished_from_is_finished_flag(server):
    client = ProgressClient(
        [ITEM],
        details={"li_1": ITEM},
        progress={"li_1": {"isFinished": True, "currentTime": 0, "duration": 0}},
    )

    AbsImporter(client, server).sync()

    assert AudiobookshelfBook.get().book.position == -1


def test_sync_marks_book_finished_without_progress_map(server):
    client = FakeClient([ITEM], details={"li_1": ITEM})
    AbsImporter(client, server).sync()

    finished_client = ProgressClient(
        [ITEM],
        details={"li_1": ITEM},
        progress={"li_1": {"isFinished": True}},
    )

    AbsImporter(finished_client, server).sync()

    assert AudiobookshelfBook.get().book.position == -1
