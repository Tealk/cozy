from types import SimpleNamespace

import pytest

pytest.importorskip("cairo")

from cozy.model.book import Book as ModelBook
from cozy.view_model.library_view_model import NO_SERIES, LibraryViewMode, LibraryViewModel


def _model_book(**fields) -> ModelBook:
    from cozy.db.book import Book

    defaults = {"name": "Book", "author": "Author", "reader": "Reader", "position": 0, "rating": -1}
    defaults.update(fields)
    db_book = Book.create(**defaults)

    book = ModelBook.__new__(ModelBook)
    book._db_object = db_book
    book._settings = None
    return book


def _view_model(books) -> LibraryViewModel:
    view_model = LibraryViewModel.__new__(LibraryViewModel)
    view_model._listeners = []
    view_model._observers = {}
    view_model._application_settings = SimpleNamespace(hide_offline=False)
    view_model._fs_monitor = SimpleNamespace(get_book_online=lambda book: True)
    view_model._model = SimpleNamespace(books=books)
    view_model._selected_filter = "All"
    view_model._library_view_mode = LibraryViewMode.SERIES
    return view_model


def _card(book):
    return SimpleNamespace(book=book)


def test_series_lists_sorted_series_and_books_without_series(peewee_database):
    books = [_model_book(series="Zwei"), _model_book(series="Alpha"), _model_book(series=None)]
    view_model = _view_model(books)

    assert view_model.series == ["Alpha", "Zwei", NO_SERIES]


def test_series_omits_hidden_books(peewee_database):
    books = [_model_book(series="Alpha"), _model_book(series="Beta", hidden=True)]
    view_model = _view_model(books)

    assert view_model.series == ["Alpha"]


def test_display_book_filter_matches_exact_series(peewee_database):
    view_model = _view_model([])
    book = _model_book(series="Die Kronen Chroniken")

    view_model.selected_filter = "Kronen"
    assert not view_model.display_book_filter(_card(book))

    view_model.selected_filter = "Die Kronen Chroniken"
    assert view_model.display_book_filter(_card(book))


def test_display_book_filter_for_books_without_series(peewee_database):
    view_model = _view_model([])
    with_series = _model_book(series="Alpha")
    without_series = _model_book(series=None)

    view_model.selected_filter = NO_SERIES

    assert view_model.display_book_filter(_card(without_series))
    assert not view_model.display_book_filter(_card(with_series))


def test_display_book_sort_orders_by_series_and_part(peewee_database):
    from functools import cmp_to_key

    view_model = _view_model([])
    first = _model_book(series="Alpha", series_part=1.0, name="B")
    second = _model_book(series="Alpha", series_part=2.0, name="A")
    other = _model_book(series="Beta", series_part=1.0, name="C")

    elements = [_card(first), _card(second), _card(other)]
    ordered = sorted(elements, key=cmp_to_key(view_model.display_book_sort))

    assert [element.book.name for element in ordered] == ["B", "A", "C"]


def test_display_book_filter_matches_any_series_of_a_book(peewee_database):
    view_model = _view_model([])
    book = _model_book(series="Alpha, Beta", series_part=1.0)

    view_model.selected_filter = "Beta"
    assert view_model.display_book_filter(_card(book))


def test_series_lists_every_series_of_a_book(peewee_database):
    view_model = _view_model([_model_book(series="Alpha, Beta")])

    assert view_model.series == ["Alpha", "Beta"]


def test_display_book_sort_uses_part_of_selected_series(peewee_database):
    from functools import cmp_to_key

    view_model = _view_model([])
    view_model.selected_filter = "Beta"
    first = _model_book(series="Alpha, Beta", name="A")
    second = _model_book(series="Beta", series_part=2.0, name="B")

    ordered = sorted([_card(second), _card(first)], key=cmp_to_key(view_model.display_book_sort))

    assert [element.book.name for element in ordered] == ["A", "B"]


def test_series_label_for_selected_series(peewee_database):
    view_model = _view_model([])
    book = _model_book(series="Alpha, Beta", series_part=1.0)

    view_model.selected_filter = "Beta"
    assert view_model.series_label_for(book) == "Beta"

    view_model.selected_filter = "Alpha"
    assert view_model.series_label_for(book) == "Alpha #1"


def test_series_label_is_empty_outside_series_mode(peewee_database):
    view_model = _view_model([])
    view_model._library_view_mode = LibraryViewMode.AUTHOR
    book = _model_book(series="Alpha", series_part=1.0)

    assert view_model.series_label_for(book) == ""


def test_series_list_merges_legacy_entries(peewee_database):
    view_model = _view_model(
        [_model_book(series="Die Zwerge #6"), _model_book(series="Die Zwerge #8")]
    )

    assert view_model.series == ["Die Zwerge"]
