from urllib.parse import urlencode

from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfServer

HTTP_SCHEMES = ("http://", "https://")


def is_remote_file(path: str) -> bool:
    return isinstance(path, str) and path.startswith(HTTP_SCHEMES)


def resolve_playback_uri(path: str) -> str:
    if not is_remote_file(path):
        return path

    server = _find_server_for_path(path)
    if server is None:
        return path

    token = secrets.get_server_token(server.id)
    if not token:
        return path

    separator = "&" if "?" in path else "?"
    return path + separator + urlencode({"token": token})


def _find_server_for_path(path: str) -> AudiobookshelfServer | None:
    for server in AudiobookshelfServer.select():
        base_url = server.url.rstrip("/")
        if path.startswith(base_url + "/") or path == base_url:
            return server
    return None
