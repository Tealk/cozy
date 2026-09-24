#!/usr/bin/env bash
read -rsp "Passwort fuer Tealk: " P
echo
python3 - "$P" <<'PY'
import json
import sys
import urllib.error
import urllib.request

server = "https://audiobookshelf.rollenspiel.monster"
password = sys.argv[1]
login_url = server + "/audiobookshelf/login"

def request(method, url, headers=None, body=None, cut=True):
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers=headers or {},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            data = response.read().decode(errors="replace")
            return response.status, (data[:300] if cut else data)
    except urllib.error.HTTPError as error:
        data = error.read().decode(errors="replace")
        return error.code, (data[:300] if cut else data)
    except Exception as error:
        return "ERR", str(error)[:200]

status, body = request("POST", login_url, {"Content-Type": "application/json"}, {"username": "Tealk", "password": password}, cut=False)
print(f"POST {login_url} -> {status}")
if status != 200:
    sys.exit(1)
token = json.loads(body)["user"]["token"]
print(f"token: {token[:24]}...")

for path in ["/api/libraries", "/audiobookshelf/api/libraries"]:
    s, b = request("GET", server + path, {"Authorization": "Bearer " + token})
    print(f"GET {path} (mit token) -> {s} {b}")

status, body = request("GET", server + "/api/libraries", {"Authorization": "Bearer " + token}, cut=False)
if status == 200:
    libraries = json.loads(body)["libraries"]
    for library in libraries:
        lib_id = library["id"]
        s, b = request("GET", server + f"/api/libraries/{lib_id}/items", {"Authorization": "Bearer " + token}, cut=False)
        if s == 200:
            data = json.loads(b)
            items = data.get("results", [])
            print(
                f"library {library['name']} ({library['mediaType']}): {data.get('total')} items"
            )
            for item in items[:5]:
                media = item.get("media") or {}
                meta = media.get("metadata") or {}
                print(
                    f"  - {item.get('id')} | {meta.get('title')} | tracks={len(media.get('audioTracks') or [])} | updatedAt={item.get('updatedAt')}"
                )
        else:
            print(f"items for {library['name']}: HTTP {s} {b}")
        s2, b2 = request("GET", server + f"/api/libraries/{lib_id}/items?limit=0&include=progress", {"Authorization": "Bearer " + token}, cut=False)
        if s2 == 200:
            data2 = json.loads(b2)
            print(f"  importer-query (limit=0&include=progress): {len(data2.get('results', []))} results")
        else:
            print(f"  importer-query: HTTP {s2} {b2[:200]}")

        if library["mediaType"] == "book" and data.get("results"):
            first_id = data["results"][0]["id"]
            for label, query in [
                ("?expanded=1&include=progress", f"?expanded=1&include=progress"),
                ("?expanded=1", f"?expanded=1"),
            ]:
                s3, b3 = request("GET", server + f"/api/items/{first_id}{query}", {"Authorization": "Bearer " + token}, cut=False)
                print(f"  detail {label} -> {s3}")
                if s3 == 200:
                    detail = json.loads(b3)
                    media = detail.get("media") or {}
                    print("   media keys:", sorted(media.keys()))
                    print(
                        "   audioTracks:",
                        len(media.get("audioTracks") or []),
                        "| tracks:",
                        len(media.get("tracks") or []),
                        "| chapters:",
                        len(media.get("chapters") or []),
                    )
                    for key in ("audioTracks", "tracks"):
                        track_list = media.get(key) or []
                        if track_list:
                            print(f"   first {key} keys:", sorted(track_list[0].keys()))
                else:
                    print(f"   body: {b3[:200]}")
            break

progress_payload = {"currentTime": 30, "duration": 600, "isFinished": False}
id_path = f"/api/me/progress/{first_id}"
for method, body in [
    ("GET", None),
    ("PATCH", progress_payload),
    ("PUT", progress_payload),
]:
    s, b = request(method, server + id_path, {"Authorization": "Bearer " + token, "Content-Type": "application/json"}, body)
    print(f"{method} {id_path} -> {s} {b}")
PY