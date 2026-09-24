#!/usr/bin/env bash
read -rsp "Passwort fuer Tealk: " P
echo
python3 - "$P" <<'PY'
import json
import os
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
            results = data2.get("results", [])
            print(f"  importer-query (limit=0&include=progress): {len(results)} results")
            if results:
                first_item = results[0]
                first_media = first_item.get("media") or {}
                print("   list item media keys :", sorted(first_media.keys()))
                print("   list item metadata   :", json.dumps(first_media.get("metadata") or {})[:600])
                print("   media.series         :", json.dumps(first_media.get("series"))[:400])
                print("   metadata keys        :", sorted((first_media.get("metadata") or {}).keys()))

                def find_series_keys(node, path=""):
                    found = []
                    if isinstance(node, dict):
                        for key, value in node.items():
                            if "series" in key.lower():
                                found.append((f"{path}.{key}", json.dumps(value)[:200]))
                            found.extend(find_series_keys(value, f"{path}.{key}"))
                    elif isinstance(node, list):
                        for index, value in enumerate(node[:1]):
                            found.extend(find_series_keys(value, f"{path}[{index}]"))
                    return found

                print("   series keys in item  :", find_series_keys(first_item)[:5])
                with_series = [item for item in results if find_series_keys(item)]
                print(f"   items with series    : {len(with_series)} of {len(results)}")
                if with_series:
                    print("   example              :", find_series_keys(with_series[0])[:3])

                def find_progress_keys(node, path=""):
                    found = []
                    if isinstance(node, dict):
                        for key, value in node.items():
                            if "progress" in key.lower():
                                found.append((f"{path}.{key}", json.dumps(value)[:200]))
                            found.extend(find_progress_keys(value, f"{path}.{key}"))
                    elif isinstance(node, list):
                        for index, value in enumerate(node[:1]):
                            found.extend(find_progress_keys(value, f"{path}[{index}]"))
                    return found

                for item in results[:3]:
                    media = item.get("media") or {}
                    print("   progress keys      :", find_progress_keys(item)[:4])
                    print("   media.progress     :", json.dumps(media.get("progress"))[:200])
                    print("   item keys          :", sorted(item.keys()))

                for path in ("/api/me/progress", "/api/me/items-in-progress", "/api/me"):
                    status, body = request(
                        "GET", server + path, {"Authorization": "Bearer " + token}
                    )
                    print(f"   GET {path} -> {status} {body[:220]}")

                query = os.environ.get("TITLE_QUERY", "Rückkehr der Zwerge")
                matches = [
                    item for item in results
                    if query.lower() in ((item.get("media") or {}).get("metadata") or {}).get("title", "").lower()
                ]
                print(f"   query {query!r}: {len(matches)} matches")
                for match in matches:
                    print("     item id:", match.get("id"))
                    for path, value in find_series_keys(match):
                        print("     ", path, "=", value)
                    detail_media = {}
                    status, detail_body = request("GET", server + f"/api/items/{match['id']}?expanded=1", headers)
                    if status == 200:
                        detail_media = json.loads(detail_body).get("media") or {}
                        print("     detail media keys:", sorted(detail_media.keys()))
                        print("     detail series    :", json.dumps(detail_media.get("series"))[:600])
                        detail_meta = detail_media.get("metadata") or {}
                        print("     detail metadata  :", json.dumps({
                            k: v for k, v in detail_meta.items() if "series" in k.lower()
                        })[:600])
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