from gi.repository import Gst

from cozy.model.book import Book as ModelBook


def _db_book(**fields):
    from cozy.db.book import Book

    defaults = {"name": "Book", "author": "Author", "reader": "Reader", "position": 0, "rating": -1}
    defaults.update(fields)
    return Book.create(**defaults)


def _model_book(**fields) -> ModelBook:
    book = ModelBook.__new__(ModelBook)
    book._db_object = _db_book(**fields)
    book._settings = None
    return book


def test_series_text_formats_part(peewee_database):
    assert _model_book(series="Alpha", series_part=2.0).series_text == "Alpha #2"
    assert _model_book(series="Alpha", series_part=1.5).series_text == "Alpha #1.5"
    assert _model_book(series="Alpha").series_text == "Alpha"
    assert _model_book().series_text == ""


def test_series_text_handles_whole_and_fractional_parts(peewee_database):
    assert _model_book(series="Alpha", series_part=10.0).series_text == "Alpha #10"
    assert _model_book(series="Alpha", series_part=0.5).series_text == "Alpha #0.5"


def test_has_details(peewee_database):
    assert not _model_book().has_details
    assert _model_book(series="Alpha").has_details
    assert _model_book(description="Text").has_details
    assert _model_book(publisher="Publisher").has_details
    assert _model_book(published_year=1999).has_details
    assert _model_book(language="German").has_details
    assert _model_book(asin="B000000000").has_details


def test_metadata_defaults_are_empty_strings(peewee_database):
    book = _model_book()

    assert book.series == ""
    assert book.description == ""
    assert book.publisher == ""
    assert book.language == ""
    assert book.asin == ""
    assert book.series_part is None
    assert book.published_year is None


def test_series_entries_splits_multiple_series(peewee_database):
    book = _model_book(series="Alpha, Beta", series_part=2.0)

    assert book.series_entries == [("Alpha", 2.0), ("Beta", None)]
    assert book.series_text == "Alpha #2, Beta"


def test_series_part_for_returns_part_of_requested_series(peewee_database):
    book = _model_book(series="Alpha, Beta", series_part=2.0)

    assert book.series_part_for("Alpha") == 2.0
    assert book.series_part_for("Beta") is None
    assert book.series_part_for("Gamma") is None


def test_series_entries_strips_part_from_legacy_values(peewee_database):
    book = _model_book(series="Die Zwerge #6, Die Zwerge #8", series_part=None)

    assert book.series_entries == [("Die Zwerge", 6.0)]


def test_series_entries_merges_duplicates_and_keeps_part(peewee_database):
    book = _model_book(series="Alpha, Alpha #2", series_part=None)

    assert book.series_entries == [("Alpha", 2.0)]
    assert book.series_text == "Alpha #2"


def test_series_entries_keeps_non_numeric_suffix(peewee_database):
    book = _model_book(series="Alpha #special")

    assert book.series_entries == [("Alpha", None)]
    assert book.series_text == "Alpha #special"


def test_series_part_for_ignores_legacy_suffix(peewee_database):
    book = _model_book(series="Die Zwerge #6")

    assert book.series_part_for("Die Zwerge") == 6.0


def test_series_text_renders_all_parts(peewee_database):
    book = _model_book(series="Scheibenwelt #4, Tod #1", series_part=4.0)

    assert book.series_text == "Scheibenwelt #4, Tod #1"
    assert book.series_entries == [("Scheibenwelt", 4.0), ("Tod", 1.0)]
    assert book.series_part_for("Tod") == 1.0


def test_metadata_is_parsed_from_json(peewee_database):
    book = _model_book(metadata_json='{"genres": ["Fantasy"], "customField": "Wert"}')

    assert book.metadata["genres"] == ["Fantasy"]
    labels = dict(book.extra_metadata)
    assert labels["Genres"] == "Fantasy"
    assert labels["Customfield"] == "Wert"


def test_metadata_ignores_broken_json(peewee_database):
    book = _model_book(metadata_json="{not json")

    assert book.metadata == {}
    assert book.extra_metadata == []


def test_status_text_for_unread_book(peewee_database):
    book = _model_book(position=0)

    assert book.status_text == "Not started"
    assert book.has_status is False


def test_status_text_for_finished_book(peewee_database):
    book = _model_book(position=-1)

    assert book.status_text == "Finished"
    assert book.has_status is True


def test_status_text_for_book_in_progress(peewee_database):
    from cozy.db.file import File
    from cozy.db.model_base import get_sqlite_database
    from cozy.db.track import Track
    from cozy.db.track_to_file import TrackToFile

    db_book = _db_book(position=1)
    file = File.create(path="/tmp/book.mp3", modified=0)
    Track.create(name="01", number=1, disk=1, position=0, book=db_book, length=3600)
    track = Track.get(Track.book == db_book)
    TrackToFile.create(track=track, file=file, start_at=0)
    track.position = Gst.SECOND * 900
    track.save()
    db_book.position = track.id
    db_book.save(only=[db_book._meta.fields["position"]])

    book = ModelBook(get_sqlite_database(), db_book)
    book._settings = None

    assert book.has_status is True
    assert book.status_text.startswith("25 %")
    assert "15 minutes" in book.status_text


def test_extra_metadata_formats_values(peewee_database):
    book = _model_book(metadata_json='{"abridged": true, "explicit": false, "tags": ["a", "b"]}')

    labels = dict(book.extra_metadata)
    assert labels["Abridged"] == "Yes"
    assert labels["Explicit"] == "No"
    assert labels["Tags"] == "a, b"


def test_has_details_is_true_for_unknown_metadata_only(peewee_database):
    assert not _model_book().has_details
    assert _model_book(metadata_json='{"custom": "x"}').has_details


def test_series_entries_parse_series_range(peewee_database):
    book = _model_book(series="Die Chroniken von Waldsee #1-3")

    assert book.series_entries == [("Die Chroniken von Waldsee", 1.0)]
    assert book.series_text == "Die Chroniken von Waldsee #1-3"
