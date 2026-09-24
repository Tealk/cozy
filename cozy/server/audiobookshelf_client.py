import logging
import random
import time
from typing import Optional

import requests

log = logging.getLogger("audiobookshelf")

LOGIN_PATH = "/audiobookshelf/login"
LIBRARIES_PATH = "/api/libraries"
AUTHORIZE_PATH = "/api/authorize"
LIBRARY_ITEMS_PATH = "/api/libraries/{library_id}/items"
ITEM_PATH = "/api/items/{item_id}"
ITEM_COVER_PATH = "/api/items/{item_id}/cover"
ME_PROGRESS_PATH = "/api/me/progress/{item_id}"

MAX_ATTEMPTS = 3
RETRY_BACKOFF = (1.0, 2.0, 4.0)
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class AudiobookshelfError(Exception):
    pass


class AuthenticationError(AudiobookshelfError):
    pass


class AudiobookshelfClient:
    def __init__(
        self,
        base_url: str,
        token: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._username = username
        self._password = password
        self._user = None
        self._session = session or requests.Session()
        self._session.headers["User-Agent"] = "Cozy"

    def authorize(self) -> str:
        if not self.token and self._username is not None:
            self.token = self._login()
        return self.token

    def _login(self) -> str:
        data = self._post(LOGIN_PATH, json={"username": self._username, "password": self._password})
        self._user = data.get("user")
        token = (data.get("user") or {}).get("token")
        if not token:
            raise AuthenticationError("Login response did not contain a user token")
        return token

    def get_authorized_user(self) -> dict:
        try:
            return self._get_json(AUTHORIZE_PATH)
        except AuthenticationError:
            raise
        except AudiobookshelfError:
            if self._user is not None:
                return self._user
            raise

    def get_libraries(self) -> list[dict]:
        data = self._get_json(LIBRARIES_PATH)
        return data.get("libraries", [])

    def get_library_items(self, library_id: str) -> list[dict]:
        data = self._get_json(
            LIBRARY_ITEMS_PATH.format(library_id=library_id), limit=0, include="progress"
        )
        return data.get("results", [])

    def get_item(self, item_id: str) -> dict:
        return self._get_json(ITEM_PATH.format(item_id=item_id), expanded=1, include="progress")

    def get_cover(self, item_id: str) -> Optional[bytes]:
        try:
            return self._get_bytes(ITEM_COVER_PATH.format(item_id=item_id), raw=1)
        except AudiobookshelfError as e:
            log.info("Could not fetch cover for item %s: %s", item_id, e)
            return None

    def post_progress(
        self, library_item_id: str, current_time: float, duration: float, is_finished: bool = False
    ) -> None:
        body = {"currentTime": current_time, "duration": duration}
        if is_finished:
            body["isFinished"] = True
        self._patch(ME_PROGRESS_PATH.format(item_id=library_item_id), json=body)

    def _get_json(self, path: str, **params) -> dict:
        response = self._request("GET", path, params=params)
        return response.json()

    def _get_bytes(self, path: str, **params) -> bytes:
        response = self._request("GET", path, params=params)
        return response.content

    def _post(self, path: str, **kwargs) -> dict:
        response = self._request("POST", path, **kwargs)
        return response.json()

    def _patch(self, path: str, **kwargs) -> Optional[dict]:
        response = self._request("PATCH", path, **kwargs)
        try:
            return response.json()
        except ValueError:
            return None

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)

        headers = kwargs.pop("headers", {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        kwargs["headers"] = headers

        url = self.base_url + path
        response = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self._session.request(method, url, **kwargs)
            except requests.RequestException as e:
                if attempt == MAX_ATTEMPTS:
                    raise AudiobookshelfError(
                        f"{method} {url}: request failed after {attempt} attempts: {e}"
                    ) from e

                self._wait_before_retry(method, url, attempt, str(e))
                continue

            if response.status_code not in RETRY_STATUS_CODES or attempt == MAX_ATTEMPTS:
                break

            log.warning(
                "%s %s: status %s, retry %d of %d",
                method,
                url,
                response.status_code,
                attempt,
                MAX_ATTEMPTS,
            )
            self._wait_before_retry(method, url, attempt, response.headers.get("Retry-After"))

        error = f"{method} {url}: "
        if response.status_code in (401, 403):
            raise AuthenticationError(
                error + f"Authentication failed with status {response.status_code}: "
                f"{self._error_body(response)}"
            )
        if response.status_code >= 400:
            raise AudiobookshelfError(
                error + f"Audiobookshelf request failed with status {response.status_code}: "
                f"{self._error_body(response)}"
            )

        return response

    def _wait_before_retry(self, method: str, url: str, attempt: int, retry_after) -> None:
        delay = self._retry_delay(attempt, retry_after)
        log.info("%s %s: retrying in %.1f seconds", method, url, delay)
        time.sleep(delay)

    @staticmethod
    def _retry_delay(attempt: int, retry_after=None) -> float:
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except (TypeError, ValueError):
                pass

        backoff = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF)) - 1]
        return backoff + random.uniform(0, 0.5)

    @staticmethod
    def _error_body(response: requests.Response) -> str:
        body = response.text.strip().replace("\n", " ")[:300]
        return body if body else "<no response body>"
