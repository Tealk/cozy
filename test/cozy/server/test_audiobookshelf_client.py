import pytest

from cozy.server.audiobookshelf_client import (
    AudiobookshelfClient,
    AudiobookshelfError,
    AuthenticationError,
)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b"", text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.content = content
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


def test_raises_on_server_error():
    session = FakeSession([FakeResponse(500, {"error": "boom"})])
    client = make_client(session)

    with pytest.raises(AudiobookshelfError):
        client.get_libraries()
