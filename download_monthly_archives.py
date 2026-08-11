"""
Auto-download Delta Exchange monthly options/futures trade archives.

What it does:
  1. Opens a browser, you log in once (2FA / captcha as needed).
  2. Pulls the auth token from localStorage into memory — never writes it
     to disk. Closing the script destroys it.
  3. Asks Delta's API which monthly archives exist (per underlying + year).
  4. Downloads any missing file into options_data/ (resume-capable, real
     Range-based resume on partial files).

The signed S3 URLs are short-lived (5 min) so each attempt gets a fresh URL.
You log in each run — the token does not persist.
"""
import argparse
import asyncio
import json
import random
import sys
import time
import warnings
from pathlib import Path

import nodriver
import requests

warnings.filterwarnings("ignore", category=ResourceWarning)

BASE = "https://cdn.india.deltaex.org"
OUT_DIR = Path("options_data")
LOGIN = "https://www.delta.exchange/app/login"

HEADERS_BASE = {
    "referer": "https://www.delta.exchange/",
    "origin": "https://www.delta.exchange",
    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "accept": "application/json",
}

MAX_ATTEMPTS = 5


def backoff_delay(attempt):
    """HANDBOOK R5: exp backoff with jitter, capped at 30s."""
    return min(30.0, (2 ** attempt) + random.uniform(0, 1.0))


def login_and_get_token() -> str:
    """HANDBOOK R1, R6: open a real Chrome via nodriver, let the user log in,
    read the token. Token lives only in the returned Python string — nothing
    is written to disk.
    """
    return asyncio.run(_login_and_get_token_async())


async def _login_and_get_token_async() -> str:
    """HANDBOOK R6: nodriver (not Playwright) + 10-min login deadline."""
    print(f"[LOGIN] Opening {LOGIN}")
    print("[LOGIN] Log in, handle 2FA/captcha. Token is detected automatically.")
    browser = await nodriver.start(headless=False)
    try:
        page = await browser.get(LOGIN)
        last_url = None
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                url = page.url
                if url != last_url:
                    print(f"[LOGIN] now at: {url}")
                    last_url = url
                raw = await page.evaluate(
                    "window.localStorage.getItem('persist:root')"
                )
                if raw:
                    try:
                        token = json.loads(json.loads(raw)["user"])["token"]
                        if token:
                            print(f"[LOGIN] token acquired from {url}")
                            return token
                    except Exception:
                        pass
            except Exception:
                pass
            await asyncio.sleep(1)
        raise RuntimeError(
            "Login timeout — no token in localStorage after 10 min. "
            "Did login succeed?"
        )
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        await asyncio.sleep(0.5)


def list_files(session, token, contract_type, symbol, year):
    """HANDBOOK R5: list monthly archive files for one (contract_type, symbol, year).
    Retries on network/5xx; 401 is terminal (token expired). Token is never
    printed (R1)."""
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = session.get(
                f"{BASE}/v2/trades_history/files/monthly",
                params={"contract_type": contract_type, "year": str(year), "symbol": symbol},
                headers={**HEADERS_BASE, "authorization": token},
                timeout=20,
            )
            if r.status_code == 401:
                raise RuntimeError("Token expired — re-run to log in again.")
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", backoff_delay(attempt)))
                print(f"  [list] HTTP {r.status_code}, retry in {delay:.1f}s")
                time.sleep(delay); continue
            r.raise_for_status()
            return r.json().get("result", []) or []
        except (requests.RequestException, OSError) as e:
            last_err = e
            delay = backoff_delay(attempt)
            print(f"  [list] error: {e}, retry in {delay:.1f}s ({attempt+1}/{MAX_ATTEMPTS})")
            time.sleep(delay)
    raise RuntimeError(f"list_files failed: {last_err}")


def get_signed_url(session, token, file_name):
    """HANDBOOK R5, R7: ask Delta for a fresh pre-signed S3 URL for one file.
    Called once per download attempt so S3's 5-min URL expiry can't kill a
    large file mid-stream."""
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            r = session.get(
                f"{BASE}/v2/trades_history",
                params={"key": file_name},
                headers={**HEADERS_BASE, "authorization": token},
                timeout=20,
            )
            if r.status_code == 401:
                raise RuntimeError("Token expired — re-run to log in again.")
            if r.status_code == 429 or r.status_code >= 500:
                delay = float(r.headers.get("Retry-After", backoff_delay(attempt)))
                time.sleep(delay); continue
            r.raise_for_status()
            result = r.json().get("result") or []
            if not result:
                raise RuntimeError(f"empty result for {file_name}")
            return result[0].get("url")
        except (requests.RequestException, OSError) as e:
            last_err = e
            delay = backoff_delay(attempt)
            time.sleep(delay)
    raise RuntimeError(f"get_signed_url failed for {file_name}: {last_err}")


def download_with_resume(session, token, file_name, dest_final: Path):
    """HANDBOOK R3, R7, R8: download with real Range-resume. Re-signs the URL
    each attempt (R7) so a 5-min expiry mid-stream doesn't kill a large file.
    Returns total bytes. Final file is written to <dest_final>.part then
    renamed to dest_final (R8)."""
    part = dest_final.with_suffix(dest_final.suffix + ".part")
    last_err = None
    for attempt in range(MAX_ATTEMPTS):
        signed = get_signed_url(session, token, file_name)
        existing = part.stat().st_size if part.exists() else 0
        req_headers = {"Range": f"bytes={existing}-"} if existing > 0 else {}
        bytes_so_far = existing
        total_expected = 0
        try:
            with session.get(signed, stream=True, timeout=120, headers=req_headers) as r:
                if existing > 0 and r.status_code == 206:
                    mode = "ab"
                    total_expected = existing + int(r.headers.get("Content-Length", 0))
                elif r.status_code == 200:
                    mode = "wb"
                    bytes_so_far = 0
                    total_expected = int(r.headers.get("Content-Length", 0))
                elif r.status_code == 416:
                    # Range not satisfiable: .part is already the full object
                    if existing > 0:
                        print(f"\n      [range-satisfied] {existing/1e6:.1f} MB already on disk")
                        return existing
                    r.raise_for_status()
                else:
                    r.raise_for_status()
                with open(part, mode) as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        if chunk:
                            f.write(chunk)
                            bytes_so_far += len(chunk)
                            if total_expected:
                                pct = bytes_so_far * 100 // total_expected
                                sys.stdout.write(f"\r      {bytes_so_far/1e6:6.1f} MB / {total_expected/1e6:6.1f} MB  ({pct:3d}%)")
                                sys.stdout.flush()
            sys.stdout.write("\n")
            if total_expected and bytes_so_far < total_expected:
                print(f"      [!] short read ({bytes_so_far} < {total_expected}); re-signing")
                continue
            return bytes_so_far
        except (requests.RequestException, OSError) as e:
            last_err = e
            delay = backoff_delay(attempt)
            print(f"\n      [!] download error: {e}; retry in {delay:.1f}s ({attempt+1}/{MAX_ATTEMPTS})")
            time.sleep(delay)
    raise RuntimeError(f"download failed for {file_name}: {last_err}")


def verify_zip(path: Path):
    """HANDBOOK R4: integrity check via zip central directory + CRC.
    Catches both BadZipFile and OSError — a malformed central directory can
    raise either depending on platform."""
    import zipfile
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                raise RuntimeError(f"zip corrupt: bad member {bad}")
    except (zipfile.BadZipFile, OSError) as e:
        raise RuntimeError(f"bad zip: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--underlying", default="BTC,ETH",
                    help="Comma-separated underlyings (options: BTC,ETH | futures: BTCUSD,ETHUSD)")
    ap.add_argument("--contract-type", default="options",
                    choices=["options", "futures"],
                    help="options uses symbol like BTC; futures uses BTCUSD")
    ap.add_argument("--years", default="",
                    help="Comma-separated years (default: auto-discover)")
    ap.add_argument("--out-dir", default=str(OUT_DIR),
                    help=f"Output directory (default: {OUT_DIR})")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be downloaded, then exit")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if file already exists")
    ap.add_argument("--no-verify", action="store_true",
                    help="Skip zip CRC verification after download")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = login_and_get_token()
    print(f"Authenticated (token {len(token)} chars, in-memory only)")

    underlyings = [u.strip() for u in args.underlying.split(",") if u.strip()]
    if args.years:
        years = [int(y) for y in args.years.split(",") if y.strip()]
    else:
        years = [2026, 2025, 2024]

    session = requests.Session()
    total_downloaded = 0
    total_skipped = 0
    total_bytes = 0
    total_failed = 0

    for sym in underlyings:
        for yr in years:
            print(f"\n=== {args.contract_type} | {sym} | {yr} ===")
            try:
                files = list_files(session, token, args.contract_type, sym, yr)
            except Exception as e:
                print(f"  [!] list error: {e}")
                continue
            if not files:
                print("  (no files)")
                continue
            print(f"  {len(files)} files listed:")
            for f in files:
                print(f"    - {f['file_name']}  ({f.get('month','')})")

            if args.dry_run:
                continue

            for f in files:
                fname = f["file_name"]
                dest = out_dir / fname
                base = fname.rsplit(".csv.zip", 1)[0]
                # HANDBOOK R2: idempotent skip — canonical name OR browser rename
                existing = list(out_dir.glob(f"{base}.csv.zip")) + \
                           list(out_dir.glob(f"{base}.csv (*.zip"))
                if existing and not args.force:
                    e = existing[0]
                    print(f"\n  [skip] {fname} already present as {e.name} ({e.stat().st_size/1e6:.1f} MB)")
                    total_skipped += 1
                    continue

                print(f"\n  [get ] {fname}")
                t0 = time.time()
                try:
                    n = download_with_resume(session, token, fname, dest)
                except Exception as e:
                    print(f"      [!] FAILED: {e}")
                    total_failed += 1
                    continue
                # HANDBOOK R8: atomic promote .part -> final
                part = dest.with_suffix(dest.suffix + ".part")
                try:
                    part.replace(dest)
                except OSError as e:
                    print(f"      [!] rename failed: {e}")
                    total_failed += 1
                    continue
                total_downloaded += 1
                total_bytes += n
                print(f"      done in {time.time()-t0:.1f}s  ({n/1e6:.1f} MB)")
                # HANDBOOK R4: zip CRC verification after promote
                if not args.no_verify:
                    try:
                        verify_zip(dest)
                        print(f"      [zip OK]")
                    except Exception as e:
                        print(f"      [!] [zip-verify-failed]: {e}; removing")
                        dest.unlink(missing_ok=True)
                        total_failed += 1
                        total_downloaded -= 1
                        total_bytes -= n

    # HANDBOOK R9: deterministic summary with exactly four counts
    print()
    print("=" * 50)
    print(f"Downloaded: {total_downloaded}   Skipped: {total_skipped}   Failed: {total_failed}   Bytes: {total_bytes/1e6:.1f} MB")
    if args.dry_run:
        print("(dry-run: nothing was actually downloaded)")

    # HANDBOOK R10: exit non-zero if anything failed; dry-run always exits 0
    if not args.dry_run and total_failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
