import argparse
import logging
import sys

from cozy.control import db as db_control
from cozy.control import secrets
from cozy.db.abs_server import AudiobookshelfServer
from cozy.server.abs_importer import AbsImporter
from cozy.server.audiobookshelf_client import AudiobookshelfClient, AudiobookshelfError

log = logging.getLogger("abs_cli")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="cozy-abs", description="Manage Audiobookshelf servers for Cozy"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add-server", help="Add or update an Audiobookshelf server")
    add.add_argument("--name", required=True)
    add.add_argument("--url", required=True)
    add.add_argument("--library-id", default=None)
    add.add_argument("--token", default=None)
    add.add_argument("--username", default=None)
    add.add_argument("--password", default=None)
    add.add_argument(
        "--id",
        type=int,
        default=None,
        help="Update an existing server instead of creating a new one",
    )

    sync = subparsers.add_parser(
        "sync", help="Synchronize a server's selected library into the Cozy library"
    )
    sync.add_argument("--id", type=int, required=True)

    test = subparsers.add_parser("test", help="Test the connection to a server")
    test.add_argument("--id", type=int, required=True)

    subparsers.add_parser("list-servers", help="List all configured servers")

    args = parser.parse_args(argv)
    db_control.init_db()

    if args.command == "add-server":
        return _add_server(args)
    elif args.command == "sync":
        return _sync(args.id)
    elif args.command == "test":
        return _test(args.id)
    elif args.command == "list-servers":
        return _list_servers()
    return 1


def _add_server(args) -> int:
    server = AudiobookshelfServer.get_or_none(args.id) if args.id else None
    if server is None:
        server = AudiobookshelfServer(name=args.name, url=args.url)

    server.name = args.name
    server.url = args.url

    client = AudiobookshelfClient(
        args.url, token=args.token, username=args.username, password=args.password
    )
    try:
        token = client.authorize()
    except (AudiobookshelfError, KeyError) as e:
        print(f"Could not authenticate against {args.url}: {e}", file=sys.stderr)
        return 1

    server.save()

    secrets.store_server_token(server.id, token)
    server.username = _username(args.username, client)
    if args.library_id:
        server.library_id = args.library_id
    else:
        server.library_id = _default_book_library(client)
    server.save()

    print(f"Server {server.id} ({server.name}) configured with library {server.library_id}")
    return 0


def _sync(server_id: int) -> int:
    server = AudiobookshelfServer.get_or_none(server_id)
    if server is None:
        print(f"Unknown server id {server_id}", file=sys.stderr)
        return 1

    if not server.library_id:
        print("Server has no library selected", file=sys.stderr)
        return 1

    token = secrets.get_server_token(server.id)
    client = AudiobookshelfClient(server.url, token=token)
    importer = AbsImporter(client, server)
    try:
        result = importer.sync()
    except AudiobookshelfError as e:
        print(f"Synchronization failed: {e}", file=sys.stderr)
        return 1

    print(
        f"created={result.created} updated={result.updated} skipped={result.skipped} removed={result.removed}"
    )
    return 0


def _test(server_id: int) -> int:
    server = AudiobookshelfServer.get_or_none(server_id)
    if server is None:
        print(f"Unknown server id {server_id}", file=sys.stderr)
        return 1

    token = secrets.get_server_token(server.id)
    client = AudiobookshelfClient(server.url, token=token)
    try:
        user = client.get_authorized_user()
        libraries = client.get_libraries()
        items = client.get_library_items(server.library_id) if server.library_id else []
    except AudiobookshelfError as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        return 1

    print(f"Connected as {user.get('username', 'unknown')}")
    print(f"Libraries: {len(libraries)}")
    if server.library_id:
        print(f"Selected library contains {len(items)} items")
    return 0


def _list_servers() -> int:
    servers = AudiobookshelfServer.select()
    for server in servers:
        print(
            f"{server.id}\t{server.name}\t{server.url}\tlibrary={server.library_id}\tuser={server.username}"
        )
    return 0


def _username(username, client) -> str | None:
    if username:
        return username
    return client.get_authorized_user().get("username")


def _default_book_library(client) -> str:
    libraries = client.get_libraries()
    book_libraries = [library for library in libraries if library.get("mediaType") == "book"]
    default = book_libraries or libraries
    return default[0]["id"] if default else ""


if __name__ == "__main__":
    sys.exit(main())
