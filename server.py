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

UPSTREAM = "https://client.musinsa.com/api/home/web/v5/pans/ranking/sections/200"
UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.musinsa.com/main/musinsa/ranking",
    "Accept": "application/json",
}

ALLOWED_GF = {"A", "M", "W"}
ALLOWED_CATEGORY = {
    "000", "104", "103", "001", "002", "003", "100",
    "004", "120", "101", "026", "017", "102", "106",
}

CACHE_TTL_SECONDS = 45
UPSTREAM_TIMEOUT_SECONDS = 12
MIN_PRICE = 50000

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


def get_fetch_lock(key):
    with _fetch_locks_guard:
        if key not in _fetch_locks:
            _fetch_locks[key] = threading.Lock()
        return _fetch_locks[key]


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
    items = [it for it in items if it["price"] >= MIN_PRICE]
    for idx, it in enumerate(items, start=1):
        it["rank"] = idx
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
            self._send_json({**cached["data"], "fetchedAt": cached["ts"], "cached": True})
            return

        lock = get_fetch_lock(key)
        with lock:
            with _cache_lock:
                cached = _cache.get(key)
            if cached and time.time() - cached["ts"] < CACHE_TTL_SECONDS:
                self._send_json({**cached["data"], "fetchedAt": cached["ts"], "cached": True})
                return
            try:
                data = fetch_ranking(gf, category_code)
                ts = time.time()
                with _cache_lock:
                    _cache[key] = {"ts": ts, "data": data}
                log_snapshot_to_supabase(gf, category_code, data)
                self._send_json({**data, "fetchedAt": ts, "cached": False})
            except Exception as exc:
                if cached:
                    self._send_json({
                        **cached["data"],
                        "fetchedAt": cached["ts"],
                        "cached": True,
                        "stale": True,
                        "error": str(exc),
                    })
                else:
                    self._send_json(
                        {"error": "무신사 서버에서 데이터를 가져오지 못했습니다: " + str(exc)}, 502
                    )

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
        else:
            self._handle_static(parsed.path)


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
        time.sleep(BACKGROUND_CYCLE_REST_SECONDS)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=background_collector, daemon=True).start()
    print(f"DropRank Live -> http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
