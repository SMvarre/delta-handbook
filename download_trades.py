"""
Download Delta Exchange options trade history via public REST API.

The browser-capture scripts (capture_options.py, capture_trade_history.py)
came back empty because the web app was geo-blocked / didn't fire its XHRs
on your machine. Delta's public REST API works fine, so this hits it directly.

Endpoints used (all public, no auth):
    GET /v2/products   -> list of BTC/ETH option contracts
    GET /v2/trades/{symbol} -> recent public trades for one contract

Output:
    delta_trade_history.csv   (one row per trade, BTC + ETH options)

Run:
    python download_trades.py

Switch endpoint (default is India; use --global for the global book):
    python download_trades.py --global
    python download_trades.py --base https://api.india.delta.exchange

Tune the fetch width:
    python download_trades.py --workers 20 --underlying BTC,ETH
"""
import argparse
import csv
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

try:
    import requests
except ImportError:
    print("ERROR: requests not installed. Run: pip install requests")
    sys.exit(1)

MAX_ATTEMPTS = 5


def backoff_delay(attempt):
    return min(30.0, (2 ** attempt) + random.uniform(0, 1.0))


def get_with_retry(session, url, params, headers, timeout, label):
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", backoff_delay(attempt)))
                if attempt < 2:
                    print(f"  [{label}] HTTP {r.status_code}, retry in {delay:.1f}s")
                time.sleep(delay); continue
            # 4xx other than 429: don't retry, caller handles (e.g. 404)
            r.raise_for_status()
            return r
        except requests.HTTPError:
            raise
        except (requests.RequestException, OSError) as e:
            last_err = e
            delay = backoff_delay(attempt)
            time.sleep(delay)
    raise RuntimeError(f"[{label}] failed after {MAX_ATTEMPTS} attempts: {last_err}")

DEFAULT_BASE = "https://api.india.delta.exchange"
GLOBAL_BASE = "https://api.delta.exchange"
CSV_OUT = "delta_trade_history.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Output columns - matches the schema already used in duck.py options_trades table
CSV_COLS = [
    "product_symbol",
    "price",
    "size",
    "timestamp_iso",
    "timestamp_raw",
    "buyer_role",
    "seller_role",
    "option_type",
    "strike_price",
    "expiry_raw",
    "underlying",
    "contract_unit_currency",
    "source",
]


def ts_to_iso(raw):
    """Delta timestamps are microseconds since epoch."""
    try:
        return datetime.fromtimestamp(raw / 1_000_000, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def parse_symbol(symbol):
    """C-BTC-65400-080726 -> ('call', 'BTC', 65400, '080726'). Puts -> P."""
    parts = symbol.split("-")
    if len(parts) < 4:
        return "", "", "", ""
    prefix, underlying, strike, expiry = parts[0], parts[1], parts[2], parts[3]
    option_type = "call" if prefix.upper().startswith("C") else "put" if prefix.upper().startswith("P") else ""
    try:
        strike_int = int(strike)
    except ValueError:
        strike_int = None
    return option_type, underlying, strike_int, expiry


def fetch_products(session, base, underlying):
    """Page through /v2/products for one underlying (BTC or ETH)."""
    products = []
    after_cursor = None
    page_size = 500
    pages = 0
    while True:
        params = {
            "contract_types": "call_options,put_options",
            "states": "live",
            "underlying_asset_symbols": underlying,
            "page_size": str(page_size),
        }
        if after_cursor:
            params["after"] = after_cursor
        r = get_with_retry(session, f"{base}/v2/products", params, HEADERS, 30, f"products:{underlying}")
        d = r.json()
        batch = d.get("result", []) or []
        products.extend(batch)
        pages += 1
        meta = d.get("meta") or {}
        next_after = meta.get("after")
        total = meta.get("total_count")
        print(f"  [{underlying}] page {pages}: +{len(batch)} (total seen {len(products)} / listed {total})")
        if not next_after or not batch or next_after == after_cursor:
            break
        after_cursor = next_after
        if pages > 50:
            print("  [!] safety cap on product pagination hit")
            break
    return products


def fetch_trades(session, base, symbol):
    """Fetch recent trades for one product symbol."""
    try:
        r = get_with_retry(session, f"{base}/v2/trades/{symbol}",
                           {"page_size": "100"}, HEADERS, 20, f"trades:{symbol}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return []
        raise
    return r.json().get("result", []) or []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE,
                    help=f"API base URL (default: {DEFAULT_BASE})")
    ap.add_argument("--global", dest="use_global", action="store_true",
                    help=f"Use the global book ({GLOBAL_BASE})")
    ap.add_argument("--underlying", default="BTC,ETH",
                    help="Comma-separated underlyings (default: BTC,ETH)")
    ap.add_argument("--workers", type=int, default=10,
                    help="Parallel HTTP workers for /v2/trades (default: 10)")
    args = ap.parse_args()

    base = GLOBAL_BASE if args.use_global else args.base
    underlyings = [u.strip().upper() for u in args.underlying.split(",") if u.strip()]
    print(f"API base: {base}")
    print(f"Underlyings: {underlyings}")
    print(f"Workers: {args.workers}\n")

    session = requests.Session()

    all_products = []
    for u in underlyings:
        print(f"Fetching product list for {u}...")
        try:
            all_products.extend(fetch_products(session, base, u))
        except Exception as e:
            print(f"  [!] products error for {u}: {e}")

    # De-dup by symbol (the same contract could appear twice across pages)
    seen = set()
    products = []
    for p in all_products:
        sym = p.get("symbol")
        if sym and sym not in seen:
            seen.add(sym)
            products.append(p)
    print(f"\n{len(products)} unique option contracts to scan.\n")

    if not products:
        print("No products found. Check connectivity to the API base.")
        return

    # Parallel trade fetch
    out_rows = []
    errors = 0
    non_empty = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        future_to_sym = {
            pool.submit(fetch_trades, session, base, p["symbol"]): p
            for p in products
        }
        for i, fut in enumerate(as_completed(future_to_sym), 1):
            p = future_to_sym[fut]
            sym = p.get("symbol", "")
            try:
                trades = fut.result()
            except Exception as e:
                errors += 1
                trades = []
                if errors <= 5:
                    print(f"  [!] {sym}: {e}")
            if trades:
                non_empty += 1
                option_type, underlying, strike, expiry_raw = parse_symbol(sym)
                for t in trades:
                    out_rows.append({
                        "product_symbol": sym,
                        "price": t.get("price"),
                        "size": t.get("size"),
                        "timestamp_iso": ts_to_iso(t.get("timestamp", 0)),
                        "timestamp_raw": t.get("timestamp"),
                        "buyer_role": t.get("buyer_role"),
                        "seller_role": t.get("seller_role"),
                        "option_type": option_type or p.get("contract_type", ""),
                        "strike_price": strike if strike is not None else p.get("strike_price"),
                        "expiry_raw": expiry_raw,
                        "underlying": underlying or p.get("contract_unit_currency", ""),
                        "contract_unit_currency": p.get("contract_unit_currency"),
                        "source": base,
                    })
            if i % 100 == 0 or i == len(products):
                print(f"  progress: {i}/{len(products)} scanned, "
                      f"{non_empty} with trades, {len(out_rows)} rows total")

    elapsed = time.time() - t0

    # Sort by timestamp descending (newest first)
    out_rows.sort(key=lambda r: r.get("timestamp_raw") or 0, reverse=True)

    tmp = CSV_OUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(out_rows)
    os.replace(tmp, CSV_OUT)

    print()
    print(f"Done in {elapsed:.1f}s.")
    print(f"  Products scanned : {len(products)}")
    print(f"  With trades      : {non_empty}")
    print(f"  Errors           : {errors}")
    print(f"  Total trade rows : {len(out_rows)}")
    print(f"\nCSV: {CSV_OUT}")
    if out_rows:
        print(f"  Newest: {out_rows[0]['product_symbol']} @ {out_rows[0]['timestamp_iso']}")
        print(f"  Oldest: {out_rows[-1]['product_symbol']} @ {out_rows[-1]['timestamp_iso']}")


if __name__ == "__main__":
    main()
