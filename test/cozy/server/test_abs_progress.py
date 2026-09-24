from types import SimpleNamespace

from cozy.server.abs_progress import progress_in_seconds


def test_progress_in_seconds_converts_nanoseconds():
    book = SimpleNamespace(progress=250_000_000_000, duration=600_000_000_000)

    current_time, duration = progress_in_seconds(book)

    assert current_time == 250.0
    assert duration == 600.0


def test_progress_in_seconds_clamps_to_duration():
    book = SimpleNamespace(progress=700_000_000_000, duration=600_000_000_000)

    current_time, duration = progress_in_seconds(book)

    assert current_time == 600.0
    assert duration == 600.0
