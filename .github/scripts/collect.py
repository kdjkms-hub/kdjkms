import json
import time
import urllib.parse
import urllib.request

UPSTREAM_TEMPLATE = "https://client.musinsa.com/api/home/web/v5/pans/ranking/sections/{}"
MUSINSA_SECTIONS = {
    "200": "NEW",
    "199": "전체",
    "201": "급상승",
    "2075": "오프라인",
    "1770": "부티크",
    "1827": "USED",
    "2210": "아울렛",
    "2211": "키즈",
    "203": "스트리트",
    "209": "미니멀",
    "205": "프레피",
    "206": "로맨틱",
    "207": "걸코어",
    "202": "캐주얼",
    "204": "워크웨어",
    "301": "레트로",
    "210": "시크",
}
UPSTREAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://www.musinsa.com/main/musinsa/ranking",
    "Accept": "application/json",
}

SUPABASE_URL = "https://blykenseazunyrxkslkm.supabase.co"
SUPABASE_KEY = "sb_publishable_YzZ7-HuyTWfwR9SQ_A6n7Q_imyR0Jjh"
SUPABASE_TABLE = "ranking_snapshots"

UPSTREAM_TIMEOUT_SECONDS = 12
REQUEST_GAP_SECONDS = 3

ALL_GF = ["A", "M", "W"]
ALL_CATEGORY = [
    "000", "104", "103", "001", "002", "003", "100",
    "004", "120", "101", "026", "017", "102", "106",
]

# a-bly requires a session token minted by a real browser (Cloudflare-gated
# HTML pages). If this starts failing with 401, the token has expired and
# needs to be re-captured from a live browser session and pasted in here.
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
ABLY_SUPABASE_TABLE = "ably_ranking_snapshots"
ABLY_CATEGORIES = {
    "000": None,
    "001": 1,
    "002": 2,
    "003": 3,
    "004": 4,
    "659": 659,
    "027": 27,
}


def fetch_ranking(gf, category_code, section_id="200"):
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
    url = UPSTREAM_TEMPLATE.format(section_id) + "?" + urllib.parse.urlencode(params)
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
                })

    items.sort(key=lambda x: x["rank"])
    return {"items": items, "sourceUpdatedAt": source_updated_at}


def log_snapshot(gf, category_code, data, section_id="200"):
    source_updated_at = data.get("sourceUpdatedAt")
    rows = [
        {
            "gf": gf,
            "category_code": category_code,
            "section_id": section_id,
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
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        SUPABASE_URL.rstrip("/") + "/rest/v1/" + SUPABASE_TABLE,
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


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
        })

    items.sort(key=lambda x: x["rank"])
    return {"items": items, "sourceUpdatedAt": source_updated_at}


def log_ably_snapshot(category_code, data):
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
    body = json.dumps(rows).encode("utf-8")
    req = urllib.request.Request(
        SUPABASE_URL.rstrip("/") + "/rest/v1/" + ABLY_SUPABASE_TABLE,
        data=body,
        method="POST",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": "Bearer " + SUPABASE_KEY,
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        resp.read()


def main():
    # NEW (200) gets full category depth. The other 16 themes are collected
    # at category=000 (전체) only, to keep request volume reasonable.
    for gf in ALL_GF:
        for category_code in ALL_CATEGORY:
            try:
                data = fetch_ranking(gf, category_code, "200")
                log_snapshot(gf, category_code, data, "200")
                print(f"ok {gf} {category_code} 200 items={len(data['items'])}")
            except Exception as exc:
                print(f"error {gf} {category_code} 200: {exc}")
            time.sleep(REQUEST_GAP_SECONDS)

    for gf in ALL_GF:
        for section_id in MUSINSA_SECTIONS:
            if section_id == "200":
                continue
            try:
                data = fetch_ranking(gf, "000", section_id)
                log_snapshot(gf, "000", data, section_id)
                print(f"ok {gf} 000 {section_id} items={len(data['items'])}")
            except Exception as exc:
                print(f"error {gf} 000 {section_id}: {exc}")
            time.sleep(REQUEST_GAP_SECONDS)

    for category_code in ABLY_CATEGORIES:
        try:
            data = fetch_ably_ranking(category_code)
            log_ably_snapshot(category_code, data)
            print(f"ok ably {category_code} items={len(data['items'])}")
        except Exception as exc:
            print(f"error ably {category_code}: {exc}")
        time.sleep(REQUEST_GAP_SECONDS)


if __name__ == "__main__":
    main()
