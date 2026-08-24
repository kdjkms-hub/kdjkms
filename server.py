import http.server
import json
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PORT = 8899
BASE_DIR = Path(__file__).resolve().parent
PUBLIC_DIR = BASE_DIR / "public"
PHOTOS_DIR = BASE_DIR / "상품사진선별"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PUBLISH_QUEUE_PATH = BASE_DIR / "발행_대기.json"
PUBLISH_STATUS_PATH = BASE_DIR / "발행_진행상황.json"
PUBLISHED_IDS_PATH = BASE_DIR / "발행완료_상품.json"
_publish_lock = threading.Lock()


def load_published_ids():
    if not PUBLISHED_IDS_PATH.exists():
        return set()
    try:
        return set(json.loads(PUBLISHED_IDS_PATH.read_text(encoding="utf-8")))
    except Exception:
        return set()


def add_published_ids(keys):
    with _publish_lock:
        current = load_published_ids()
        current.update(keys)
        PUBLISHED_IDS_PATH.write_text(
            json.dumps(sorted(current), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def annotate_published(items, site):
    published = load_published_ids()
    out = []
    for it in items:
        it = dict(it)
        it["isPublished"] = (site + ":" + str(it.get("id"))) in published
        out.append(it)
    return out


def build_response(data, site, extra):
    out = dict(data)
    out["items"] = annotate_published(out.get("items", []), site)
    out.update(extra)
    return out

UPSTREAM = "https://client.musinsa.com/api/home/web/v5/pans/ranking/sections/200"
UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.musinsa.com/main/musinsa/ranking",
    "Accept": "application/json",
}

SEARCH_UPSTREAM = "https://api.musinsa.com/api2/dp/v1/plp/goods"
SEARCH_HEADERS = {
    "User-Agent": UPSTREAM_HEADERS["User-Agent"],
    "Referer": "https://www.musinsa.com/search/goods",
    "Accept": "application/json",
}
SEARCH_PAGE_SIZE = 100

ALLOWED_GF = {"A", "M", "W"}
ALLOWED_CATEGORY = {
    "000", "104", "103", "001", "002", "003", "100",
    "004", "120", "101", "026", "017", "102", "106",
}

# a-bly requires a session token minted by a real browser (Cloudflare-gated
# HTML pages). If requests start failing with 401, this token has expired
# and needs to be re-captured from a live browser session and pasted in here.
ABLY_UPSTREAM = "https://api.a-bly.com/api/v2/goods/"
ABLY_HEADERS = {
    "X-Anonymous-Token": (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJhbm9ueW1vdXNfaWQiOiIxMjc4NDc3MDIwIiwiaWF0IjoxNzg3MDMyNDMxfQ."
        "IwN3zqYYDKgFsLk6nAzZMc-E6VqM17NGk7jIelH3OP8"
    ),
    "X-Device-Type": "PCWeb",
    "X-App-Version": "0.1.0",
    "X-Web-Type": "Web",
    "X-Device-Id": "49c535d4-d78a-4bb1-8b87-ce653be3e335",
    "Accept": "application/json, text/plain, */*",
}
ABLY_CATEGORIES = {
    "000": None,
    "001": 1,
    "002": 2,
    "003": 3,
    "004": 4,
    "659": 659,
    "027": 27,
}

CACHE_TTL_SECONDS = 45
UPSTREAM_TIMEOUT_SECONDS = 12

_cache = {}
_cache_lock = threading.Lock()
_fetch_locks = {}
_fetch_locks_guard = threading.Lock()

SUPABASE_CONFIG_PATH = BASE_DIR / "supabase_config.json"
try:
    _supabase = json.loads(SUPABASE_CONFIG_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:
    _supabase = None


def log_snapshot_to_supabase(gf, category_code, data):
    if not _supabase:
        return
    fetched_at = time.time()
    source_updated_at = data.get("sourceUpdatedAt")
    rows = [
        {
            "gf": gf,
            "category_code": category_code,
            "rank": it["rank"],
            "product_id": it["id"],
            "brand": it["brand"],
            "name": it["name"],
            "price": it["price"],
            "original_price": it["originalPrice"],
            "discount": it["discount"],
            "sold_out": it["soldOut"],
            "source_updated_at": (
                time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.localtime(source_updated_at / 1000))
                if source_updated_at else None
            ),
        }
        for it in data.get("items", [])
    ]
    if not rows:
        return

    def _send():
        try:
            body = json.dumps(rows).encode("utf-8")
            req = urllib.request.Request(
                _supabase["url"].rstrip("/") + "/rest/v1/" + _supabase.get("table", "ranking_snapshots"),
                data=body,
                method="POST",
                headers={
                    "apikey": _supabase["key"],
                    "Authorization": "Bearer " + _supabase["key"],
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            print("supabase log failed:", exc)

    threading.Thread(target=_send, daemon=True).start()


def log_ably_snapshot_to_supabase(category_code, data):
    if not _supabase:
        return
    source_updated_at = data.get("sourceUpdatedAt")
    rows = [
        {
            "category_code": category_code,
            "rank": it["rank"],
            "product_id": it["id"],
            "brand": it["brand"],
            "name": it["name"],
            "price": it["price"],
            "original_price": it["originalPrice"],
            "discount": it["discount"],
            "sold_out": it["soldOut"],
            "source_updated_at": (
                time.strftime("%Y-%m-%dT%H:%M:%S+09:00", time.localtime(source_updated_at / 1000))
                if source_updated_at else None
            ),
        }
        for it in data.get("items", [])
    ]
    if not rows:
        return

    def _send():
        try:
            body = json.dumps(rows).encode("utf-8")
            req = urllib.request.Request(
                _supabase["url"].rstrip("/") + "/rest/v1/ably_ranking_snapshots",
                data=body,
                method="POST",
                headers={
                    "apikey": _supabase["key"],
                    "Authorization": "Bearer " + _supabase["key"],
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as exc:
            print("supabase ably log failed:", exc)

    threading.Thread(target=_send, daemon=True).start()


def get_fetch_lock(key):
    with _fetch_locks_guard:
        if key not in _fetch_locks:
            _fetch_locks[key] = threading.Lock()
        return _fetch_locks[key]


def fetch_search(keyword, gf):
    params = {
        "keyword": keyword,
        "gf": gf,
        "sortCode": "POPULAR",
        "isUsed": "false",
        "size": str(SEARCH_PAGE_SIZE),
        "page": "1",
        "caller": "SEARCH",
    }
    url = SEARCH_UPSTREAM + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=SEARCH_HEADERS)
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    rows = raw.get("data", {}).get("list", [])
    items = []
    for it in rows:
        if it.get("isAd"):
            continue
        final_price = it.get("finalPrice") or it.get("price") or 0
        discount = it.get("finalDiscount") or 0
        original_price = it.get("normalPrice") or final_price
        items.append({
            "id": it.get("goodsNo"),
            "brand": it.get("brandName", ""),
            "name": it.get("goodsName", ""),
            "price": final_price,
            "originalPrice": original_price,
            "discount": discount,
            "soldOut": bool(it.get("isSoldOut")),
            "image": it.get("thumbnail", ""),
            "url": it.get("goodsLinkUrl", ""),
            "labels": [],
            "note": "",
        })

    for idx, it in enumerate(items, start=1):
        it["rank"] = idx

    total_count = raw.get("data", {}).get("pagination", {}).get("totalCount", len(items))
    return {"items": items, "totalCount": total_count}


def fetch_ranking(gf, category_code):
    params = {
        "storeCode": "musinsa",
        "gf": gf,
        "ageBand": "AGE_BAND_ALL",
        "period": "REALTIME",
        "eventPeriod": "BASIC_REALTIME",
        "categoryCode": category_code,
        "contentsId": "",
        "variantValue": "",
        "page": "1",
        "startRank": "1",
        "offset": "0",
    }
    url = UPSTREAM + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UPSTREAM_HEADERS)
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    modules = raw.get("data", {}).get("modules", [])
    source_updated_at = None
    items = []

    for module in modules:
        module_type = module.get("type")
        if module_type == "QUERY_UPDATEDAT":
            source_updated_at = module.get("information", {}).get("updatedAt")
        elif module_type == "MULTICOLUMN":
            for it in module.get("items", []):
                if it.get("type") != "PRODUCT_COLUMN":
                    continue
                info = it.get("info", {})
                image = it.get("image", {})
                on_click = it.get("onClick", {})
                ga4_payload = on_click.get("eventLog", {}).get("ga4", {}).get("payload", {})

                discount = info.get("discountRatio") or 0
                final_price = info.get("finalPrice") or 0
                original_price = ga4_payload.get("original_price")
                if not original_price:
                    original_price = round(final_price / (1 - discount / 100)) if discount else final_price

                labels = [l.get("text") for l in (image.get("labels") or []) if l.get("text")]
                notes = [a.get("text") for a in (info.get("additionalInformation") or []) if a.get("text")]

                rank = image.get("rank")
                if rank is None:
                    continue

                items.append({
                    "rank": rank,
                    "id": it.get("id"),
                    "brand": info.get("brandName", ""),
                    "name": info.get("productName", ""),
                    "price": final_price,
                    "originalPrice": int(original_price) if original_price else final_price,
                    "discount": discount,
                    "soldOut": bool(info.get("isSoldOut")),
                    "image": image.get("url", ""),
                    "url": on_click.get("url", ""),
                    "labels": labels,
                    "note": notes[0] if notes else "",
                })

    items.sort(key=lambda x: x["rank"])
    return {"items": items, "sourceUpdatedAt": source_updated_at}


def fetch_ably_ranking(category_code):
    market_type_sno = ABLY_CATEGORIES[category_code]
    params = {"filter": "best"}
    if market_type_sno is not None:
        params["market_type_sno"] = market_type_sno
    url = ABLY_UPSTREAM + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=ABLY_HEADERS)
    with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT_SECONDS) as resp:
        raw = json.loads(resp.read().decode("utf-8"))

    minutes_ago = raw.get("last_updated_before")
    source_updated_at = (time.time() - minutes_ago * 60) * 1000 if minutes_ago is not None else None

    items = []
    for it in raw.get("goods", []):
        rank = it.get("ranking")
        if rank is None:
            continue
        price = it.get("price") or 0
        discount = it.get("discount_rate") or 0
        original_price = round(price / (1 - discount / 100)) if discount else price
        market = it.get("market") or {}

        items.append({
            "rank": rank,
            "id": it.get("sno"),
            "brand": market.get("name", ""),
            "name": it.get("name", ""),
            "price": price,
            "originalPrice": original_price,
            "discount": discount,
            "soldOut": bool(it.get("is_soldout")),
            "image": it.get("image", ""),
            "url": "https://m.a-bly.com/goods/" + str(it.get("sno", "")),
            "labels": [],
            "note": (str(it.get("sell_count")) + "개 구매중") if it.get("sell_count") else "",
        })

    items.sort(key=lambda x: x["rank"])
    return {"items": items, "sourceUpdatedAt": source_updated_at}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_ranking(self, query):
        qs = urllib.parse.parse_qs(query)
        gf = (qs.get("gf", ["A"])[0] or "A").upper()
        category_code = qs.get("categoryCode", ["000"])[0] or "000"

        if gf not in ALLOWED_GF or category_code not in ALLOWED_CATEGORY:
            self._send_json({"error": "invalid parameters"}, 400)
            return

        key = (gf, category_code)
        now = time.time()

        with _cache_lock:
            cached = _cache.get(key)
        if cached and now - cached["ts"] < CACHE_TTL_SECONDS:
            self._send_json(build_response(cached["data"], "musinsa", {"fetchedAt": cached["ts"], "cached": True}))
            return

        lock = get_fetch_lock(key)
        with lock:
            with _cache_lock:
                cached = _cache.get(key)
            if cached and time.time() - cached["ts"] < CACHE_TTL_SECONDS:
                self._send_json(build_response(cached["data"], "musinsa", {"fetchedAt": cached["ts"], "cached": True}))
                return
            try:
                data = fetch_ranking(gf, category_code)
                ts = time.time()
                with _cache_lock:
                    _cache[key] = {"ts": ts, "data": data}
                log_snapshot_to_supabase(gf, category_code, data)
                self._send_json(build_response(data, "musinsa", {"fetchedAt": ts, "cached": False}))
            except Exception as exc:
                if cached:
                    self._send_json(build_response(cached["data"], "musinsa", {
                        "fetchedAt": cached["ts"],
                        "cached": True,
                        "stale": True,
                        "error": str(exc),
                    }))
                else:
                    self._send_json(
                        {"error": "무신사 서버에서 데이터를 가져오지 못했습니다: " + str(exc)}, 502
                    )

    def _handle_ably_ranking(self, query):
        qs = urllib.parse.parse_qs(query)
        category_code = qs.get("categoryCode", ["000"])[0] or "000"

        if category_code not in ABLY_CATEGORIES:
            self._send_json({"error": "invalid parameters"}, 400)
            return

        key = ("ably", category_code)
        now = time.time()

        with _cache_lock:
            cached = _cache.get(key)
        if cached and now - cached["ts"] < CACHE_TTL_SECONDS:
            self._send_json(build_response(cached["data"], "ably", {"fetchedAt": cached["ts"], "cached": True}))
            return

        lock = get_fetch_lock(key)
        with lock:
            with _cache_lock:
                cached = _cache.get(key)
            if cached and time.time() - cached["ts"] < CACHE_TTL_SECONDS:
                self._send_json(build_response(cached["data"], "ably", {"fetchedAt": cached["ts"], "cached": True}))
                return
            try:
                data = fetch_ably_ranking(category_code)
                ts = time.time()
                with _cache_lock:
                    _cache[key] = {"ts": ts, "data": data}
                log_ably_snapshot_to_supabase(category_code, data)
                self._send_json(build_response(data, "ably", {"fetchedAt": ts, "cached": False}))
            except Exception as exc:
                if cached:
                    self._send_json(build_response(cached["data"], "ably", {
                        "fetchedAt": cached["ts"],
                        "cached": True,
                        "stale": True,
                        "error": str(exc),
                    }))
                else:
                    self._send_json(
                        {"error": "에이블리 서버에서 데이터를 가져오지 못했습니다: " + str(exc)}, 502
                    )

    def _handle_search(self, query):
        qs = urllib.parse.parse_qs(query)
        keyword = (qs.get("keyword", [""])[0] or "").strip()
        gf = (qs.get("gf", ["A"])[0] or "A").upper()
        if gf not in ALLOWED_GF:
            gf = "A"
        if not keyword:
            self._send_json({"items": [], "totalCount": 0})
            return
        try:
            data = fetch_search(keyword, gf)
            data["items"] = annotate_published(data["items"], "musinsa")
            self._send_json(data)
        except Exception as exc:
            self._send_json({"error": "무신사 검색에 실패했습니다: " + str(exc)}, 502)

    def _handle_publish_status(self):
        if PUBLISH_STATUS_PATH.exists():
            try:
                status = json.loads(PUBLISH_STATUS_PATH.read_text(encoding="utf-8"))
            except Exception:
                status = {"status": "idle"}
        else:
            status = {"status": "idle"}
        self._send_json(status)

    def _handle_photos_api(self):
        categories = []
        if PHOTOS_DIR.exists():
            for cat_dir in sorted(PHOTOS_DIR.iterdir()):
                if not cat_dir.is_dir():
                    continue
                images = []
                for f in sorted(cat_dir.rglob("*")):
                    if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                        rel = str(f.relative_to(PHOTOS_DIR)).replace("\\", "/")
                        images.append({
                            "name": f.stem,
                            "path": rel,
                            "url": "/photos/" + urllib.parse.quote(rel),
                        })
                if images:
                    categories.append({"name": cat_dir.name, "images": images})
        self._send_json({"categories": categories})

    def _handle_photo_file(self, path):
        rel = urllib.parse.unquote(path[len("/photos/"):])
        candidate = (PHOTOS_DIR / rel).resolve()
        try:
            candidate.relative_to(PHOTOS_DIR.resolve())
        except ValueError:
            self.send_error(403)
            return
        if not candidate.exists() or not candidate.is_file() or candidate.suffix.lower() not in IMAGE_EXTENSIONS:
            self.send_error(404)
            return

        content_type = "application/octet-stream"
        ext = candidate.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            content_type = "image/jpeg"
        elif ext == ".png":
            content_type = "image/png"
        elif ext == ".webp":
            content_type = "image/webp"
        elif ext == ".gif":
            content_type = "image/gif"

        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_static(self, path):
        rel = path.lstrip("/") or "index.html"
        candidate = (PUBLIC_DIR / rel).resolve()
        try:
            candidate.relative_to(PUBLIC_DIR)
        except ValueError:
            self.send_error(403)
            return
        if not candidate.exists() or candidate.is_dir():
            candidate = PUBLIC_DIR / "index.html"

        content_type = "application/octet-stream"
        if candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif candidate.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"

        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/ranking":
            self._handle_ranking(parsed.query)
        elif parsed.path == "/api/ably-ranking":
            self._handle_ably_ranking(parsed.query)
        elif parsed.path == "/api/search":
            self._handle_search(parsed.query)
        elif parsed.path == "/api/photos":
            self._handle_photos_api()
        elif parsed.path == "/api/publish-status":
            self._handle_publish_status()
        elif parsed.path.startswith("/photos/"):
            self._handle_photo_file(parsed.path)
        else:
            self._handle_static(parsed.path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/publish":
            self._handle_publish_post()
        elif parsed.path == "/api/photos/delete":
            self._handle_photos_delete()
        else:
            self.send_error(404)

    def _handle_photos_delete(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            rel_paths = body.get("paths", [])

            deleted = []
            errors = []
            for rel in rel_paths:
                try:
                    candidate = (PHOTOS_DIR / rel).resolve()
                    candidate.relative_to(PHOTOS_DIR.resolve())
                except ValueError:
                    errors.append({"path": rel, "error": "invalid path"})
                    continue
                if not candidate.exists() or not candidate.is_file():
                    errors.append({"path": rel, "error": "not found"})
                    continue
                try:
                    candidate.unlink()
                    deleted.append(rel)
                    parent = candidate.parent
                    while parent != PHOTOS_DIR.resolve() and parent.exists() and not any(parent.iterdir()):
                        empty_dir = parent
                        parent = parent.parent
                        empty_dir.rmdir()
                except Exception as exc:
                    errors.append({"path": rel, "error": str(exc)})

            self._send_json({"deleted": deleted, "errors": errors})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)

    def _handle_publish_post(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            items = body.get("items", [])
            if not items:
                self._send_json({"error": "no items"}, 400)
                return

            entry = {"publishedAt": time.strftime("%Y-%m-%dT%H:%M:%S+09:00"), "items": items}
            with _publish_lock:
                queue = []
                if PUBLISH_QUEUE_PATH.exists():
                    try:
                        queue = json.loads(PUBLISH_QUEUE_PATH.read_text(encoding="utf-8"))
                    except Exception:
                        queue = []
                queue.append(entry)
                PUBLISH_QUEUE_PATH.write_text(
                    json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                status = {
                    "status": "queued",
                    "message": "발행됨 · 처리 대기 중 (대화창에서 확인 요청 필요)",
                    "queuedAt": entry["publishedAt"],
                    "total": len(items),
                    "completed": 0,
                    "currentItem": None,
                    "log": [],
                }
                PUBLISH_STATUS_PATH.write_text(
                    json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            published_keys = [
                it["site"] + ":" + str(it["id"])
                for it in items
                if it.get("site") and it.get("id") is not None
            ]
            if published_keys:
                add_published_ids(published_keys)

            self._send_json({"ok": True, "queued": len(items)})
        except Exception as exc:
            self._send_json({"error": str(exc)}, 500)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


BACKGROUND_REQUEST_GAP_SECONDS = 3
BACKGROUND_CYCLE_REST_SECONDS = 20
BACKGROUND_TARGETS = [
    (gf, category_code) for gf in sorted(ALLOWED_GF) for category_code in sorted(ALLOWED_CATEGORY)
]


def background_collector():
    while True:
        for gf, category_code in BACKGROUND_TARGETS:
            try:
                data = fetch_ranking(gf, category_code)
                with _cache_lock:
                    _cache[(gf, category_code)] = {"ts": time.time(), "data": data}
                log_snapshot_to_supabase(gf, category_code, data)
            except Exception as exc:
                print("background collector error:", gf, category_code, exc)
            time.sleep(BACKGROUND_REQUEST_GAP_SECONDS)
        for category_code in sorted(ABLY_CATEGORIES):
            try:
                data = fetch_ably_ranking(category_code)
                with _cache_lock:
                    _cache[("ably", category_code)] = {"ts": time.time(), "data": data}
                log_ably_snapshot_to_supabase(category_code, data)
            except Exception as exc:
                print("background collector error: ably", category_code, exc)
            time.sleep(BACKGROUND_REQUEST_GAP_SECONDS)
        time.sleep(BACKGROUND_CYCLE_REST_SECONDS)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=background_collector, daemon=True).start()
    print(f"DropRank Live -> http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
