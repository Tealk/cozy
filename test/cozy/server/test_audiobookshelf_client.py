import pytest
import requests

from cozy.server.audiobookshelf_client import (
    AudiobookshelfClient,
    AudiobookshelfError,
    AuthenticationError,
)


class FakeResponse:
    def __init__(
        self, status_code: int, payload=None, content: bytes = b"", text: str = "", headers=None
    ):
        self.status_code = status_code
        self._payload = payload
        self.content = content
        self.headers = headers or {}
        self.text = text or ("" if payload is None else str(payload))

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return self.responses.pop(0)


class NonJsonResponse(FakeResponse):
    def __init__(self, status_code: int, text: str):
        super().__init__(status_code, None, text=text)

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def make_client(session):
    return AudiobookshelfClient("http://abs.local:13378", token="token123", session=session)


def test_authorize_with_token_returns_token():
    session = FakeSession([])
    client = AudiobookshelfClient(
        "http://abs.local:13378",
        token="token123",
        username="alice",
        password="secret",
        session=session,
    )

    token = client.authorize()

    assert token == "token123"
    assert session.requests == []


def test_authorize_with_credentials_logs_in():
    login_payload = {"user": {"token": "login-token", "username": "alice"}}
    session = FakeSession([FakeResponse(200, login_payload)])
    client = AudiobookshelfClient(
        "http://abs.local:13378", username="alice", password="secret", session=session
    )

    token = client.authorize()

    assert token == "login-token"
    method, url, kwargs = session.requests[0]
    assert method == "POST"
    assert url.endswith("/login")
    assert kwargs["json"] == {"username": "alice", "password": "secret"}


def test_get_authorized_user_falls_back_to_login_user_on_missing_route():
    login_payload = {"user": {"token": "login-token", "username": "alice"}}
    session = FakeSession(
        [FakeResponse(200, login_payload), FakeResponse(404, None, text="<html>Cannot GET</html>")]
    )
    client = AudiobookshelfClient(
        "http://abs.local:13378", username="alice", password="secret", session=session
    )
    client.authorize()

    user = client.get_authorized_user()

    assert user == {"token": "login-token", "username": "alice"}


def test_authorize_raises_authentication_error():
    session = FakeSession([FakeResponse(401, {"error": "unauthorized"})])
    client = AudiobookshelfClient("http://abs.local:13378", token="bad", session=session)

    with pytest.raises(AuthenticationError):
        client.get_authorized_user()


def test_get_libraries_returns_libraries():
    payload = {"libraries": [{"id": "lib_1", "name": "Books"}]}
    client = make_client(FakeSession([FakeResponse(200, payload)]))

    libraries = client.get_libraries()

    assert libraries == [{"id": "lib_1", "name": "Books"}]


def test_get_library_items_requests_limit_zero():
    payload = {"results": [{"id": "li_1"}]}
    session = FakeSession([FakeResponse(200, payload)])
    client = make_client(session)

    items = client.get_library_items("lib_1")

    assert items == [{"id": "li_1"}]
    params = session.requests[0][2]["params"]
    assert params["limit"] == 0


def test_get_item_requests_expanded():
    payload = {"id": "li_1", "media": {}}
    session = FakeSession([FakeResponse(200, payload)])
    client = make_client(session)

    client.get_item("li_1")

    params = session.requests[0][2]["params"]
    assert params["expanded"] == 1
    assert params["include"] == "progress"


def test_get_cover_returns_content():
    session = FakeSession([FakeResponse(200, None, content=b"\x89PNG")])
    client = make_client(session)

    cover = client.get_cover("li_1")

    assert cover == b"\x89PNG"


def test_get_cover_returns_none_on_error():
    session = FakeSession([FakeResponse(404, {"error": "not found"})])
    client = make_client(session)

    cover = client.get_cover("li_1")

    assert cover is None


def test_post_progress():
    session = FakeSession([FakeResponse(200, {})])
    client = make_client(session)

    client.post_progress("li_1", current_time=100.0, duration=600.0)

    method, url, kwargs = session.requests[0]
    assert method == "PATCH"
    assert url.endswith("/api/me/progress/li_1")
    assert kwargs["json"] == {"currentTime": 100.0, "duration": 600.0}


def test_post_progress_finished():
    session = FakeSession([FakeResponse(200, {})])
    client = make_client(session)

    client.post_progress("li_1", current_time=600.0, duration=600.0, is_finished=True)

    method, url, kwargs = session.requests[0]
    assert method == "PATCH"
    assert url.endswith("/api/me/progress/li_1")
    assert kwargs["json"] == {"currentTime": 600.0, "duration": 600.0, "isFinished": True}


def test_post_progress_empty_response_body():
    session = FakeSession([FakeResponse(200, None, text="")])
    client = make_client(session)

    client.post_progress("li_1", current_time=100.0, duration=600.0)


def test_post_progress_non_json_response_body():
    session = FakeSession([NonJsonResponse(200, "OK")])
    client = make_client(session)

    client.post_progress("li_1", current_time=100.0, duration=600.0)


def test_raises_on_server_error(no_sleep):
    session = FakeSession([FakeResponse(500, {"error": "boom"}) for _ in range(3)])
    client = make_client(session)

    with pytest.raises(AudiobookshelfError):
        client.get_libraries()


@pytest.fixture
def no_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr(
        "cozy.server.audiobookshelf_client.time.sleep", lambda seconds: slept.append(seconds)
    )
    return slept


def test_retries_on_server_error(no_sleep):
    session = FakeSession([FakeResponse(503, {}), FakeResponse(200, {"libraries": []})])
    client = make_client(session)

    assert client.get_libraries() == []
    assert len(session.requests) == 2
    assert len(no_sleep) == 1


def test_retries_on_rate_limit_and_honors_retry_after(no_sleep):
    session = FakeSession(
        [FakeResponse(429, {}, headers={"Retry-After": "7"}), FakeResponse(200, {"libraries": []})]
    )
    client = make_client(session)

    assert client.get_libraries() == []
    assert no_sleep == [7.0]


def test_raises_after_max_attempts(no_sleep):
    session = FakeSession([FakeResponse(500, {"error": "boom"}) for _ in range(3)])
    client = make_client(session)

    with pytest.raises(AudiobookshelfError):
        client.get_libraries()

    assert len(session.requests) == 3
    assert len(no_sleep) == 2


def test_does_not_retry_on_client_error(no_sleep):
    session = FakeSession([FakeResponse(404, {"error": "nope"})])
    client = make_client(session)

    with pytest.raises(AudiobookshelfError):
        client.get_item("li_1")

    assert len(session.requests) == 1
    assert no_sleep == []


def test_retries_on_request_exception(no_sleep):
    class FlakySession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise requests.ConnectionError("boom")

            return FakeResponse(200, {"libraries": []})

    session = FlakySession()
    client = make_client(session)

    assert client.get_libraries() == []
    assert session.calls == 2
    assert len(no_sleep) == 1


def test_raises_when_all_attempts_fail_with_request_exception(no_sleep):
    class BrokenSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            raise requests.ConnectionError("boom")

    session = BrokenSession()
    client = make_client(session)

    with pytest.raises(AudiobookshelfError):
        client.get_libraries()

    assert session.calls == 3
    assert len(no_sleep) == 2


def test_get_progress_maps_library_items():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "mediaProgress": [
                        {"libraryItemId": "li_1", "currentTime": 10, "duration": 100},
                        {"libraryItemId": "li_2", "currentTime": 50, "duration": 100},
                    ]
                },
            )
        ]
    )
    client = make_client(session)

    progress = client.get_progress()

    assert set(progress) == {"li_1", "li_2"}
    assert progress["li_2"]["currentTime"] == 50
    assert session.requests[0][1].endswith("/api/me/progress")


def test_get_progress_prefers_book_entries_over_episodes():
    session = FakeSession(
        [
            FakeResponse(
                200,
                {
                    "mediaProgress": [
                        {"libraryItemId": "li_1", "episodeId": "ep_1", "currentTime": 5},
                        {"libraryItemId": "li_1", "currentTime": 42},
                    ]
                },
            )
        ]
    )
    client = make_client(session)

    assert client.get_progress()["li_1"]["currentTime"] == 42


def test_get_progress_handles_alternative_payload():
    session = FakeSession(
        [FakeResponse(200, {"libraryItemsInProgress": [{"itemId": "li_3", "currentTime": 7}]})]
    )
    client = make_client(session)

    assert client.get_progress()["li_3"]["currentTime"] == 7


def test_get_progress_handles_empty_payload():
    session = FakeSession([FakeResponse(200, {})])
    client = make_client(session)

    assert client.get_progress() == {}
