# HANDBOOK — Monthly Archive Downloader (`download_monthly_archives.py`)

This document is the binding policy for `download_monthly_archives.py`. Every
rule below has a rule ID (`Rn`), a one-line statement, the reason it exists,
and a pointer to where the code enforces it. The companion test file
`test_handbook.py` asserts each rule.

Scope: this handbook governs only the monthly archive downloader. It is *not*
derived from any external paper; it is the operational spec for this script.

---

## R1 — Auth tokens live only in memory

The Delta auth token acquired during login must never be written to disk, to
any file, in any form (raw, base64, JSON, log line, or exception message).

**Why:** this working directory is OneDrive-synced. A persisted token is a
credential leak.

**Enforced in:** `login_and_get_token()` returns a Python `str`. No `open()`
call in the script writes a token-shaped value. Error messages from
`list_files` / `get_signed_url` say "Token expired" — they do not include the
token.

## R2 — Idempotent re-runs

Running the script twice must not re-download a file already present in
`--out-dir`. A file is "present" if either its canonical name
(`<base>.csv.zip`) OR a browser-renamed duplicate (`<base>.csv (1).zip`,
`<base>.csv (2).zip`, ...) exists.

**Why:** the API re-issues signed URLs freely; the cost of an unnecessary
re-download is bandwidth + S3 PUT cost on Delta's side.

**Enforced in:** `main()` checks `out_dir.glob(...)` for both patterns before
fetching. `--force` overrides.

## R3 — Resumable downloads via HTTP Range

If a previous run left a `<dest>.part` file, the next run must continue from
that byte offset using `Range: bytes=N-`. If the server returns 200 (ignored
Range), truncate and restart. If it returns 416 (Range unsatisfiable) and the
`.part` is non-empty, treat the `.part` as complete.

**Why:** monthly archives are 100s of MB; restarting from zero on every
failure wastes the work already done.

**Enforced in:** `download_with_resume()`.

## R4 — Every completed download must pass a zip CRC check

After the `.part` is promoted to its final name, the script must run
`zipfile.testzip()` over it. A failed check must remove the file and count it
as failed. `--no-verify` is the only opt-out.

**Why:** a short read or interrupted re-sign can produce a byte-complete
HTTP 200 stream that is internally corrupt.

**Enforced in:** `verify_zip()` + the post-promote check in `main()`.

## R5 — Retry with exponential backoff + `Retry-After`

Every network operation (`list_files`, `get_signed_url`, the stream GET inside
`download_with_resume`) retries up to `MAX_ATTEMPTS = 5` times. On 429 or 5xx,
the retry delay is the server's `Retry-After` header if present, otherwise
`min(30, 2**attempt + uniform(0,1))` seconds. On 4xx other than 429, fail
immediately.

**Why:** Delta's CDN rate-limits aggressively; ignoring `Retry-After` gets you
banned.

**Enforced in:** `backoff_delay()` + the per-call retry loops.

## R6 — Login via nodriver with a hard 10-minute deadline

Login must use `nodriver` (not Playwright), because reCAPTCHA fingerprints
automation browser sessions. The script must poll `localStorage` for the
`persist:root.user.token` key on each navigation. If no token appears within
600 seconds, the run aborts with a clear error.

**Why:** Playwright triggers bot-detection; missing the deadline means the
user walked away and the run should not hang indefinitely.

**Enforced in:** `_login_and_get_token_async()`.

## R7 — Re-sign the S3 URL on every download attempt

S3 pre-signed URLs expire in ~5 minutes. Each retry inside
`download_with_resume` must call `get_signed_url` afresh rather than reusing
a stale URL.

**Why:** a large file started near expiry would otherwise fail mid-stream with
no recovery.

**Enforced in:** `download_with_resume()` — `signed = get_signed_url(...)`
inside the attempt loop.

## R8 — Atomic promotion from `.part` to final name

The final filename must never appear on disk in a half-written state. Bytes
are written to `<dest>.part`; only after the stream completes (and the zip
verifies, if `--no-verify` is not set) is the file atomically renamed to its
final name via `Path.replace`. A failed rename must count as a failure.

**Why:** downstream consumers (the DuckDB loaders) glob for the final name;
a partial file there corrupts the load.

**Enforced in:** `main()` — `part.replace(dest)` after download (and after
verify, since verify-fail removes the file outright).

## R9 — Deterministic output discipline

Per-file log lines must use the tags `[skip]`, `[get]`, `[FAILED]`, or
`[zip OK]` so a downstream parser can grep them. The final summary must
include exactly four counts: `Downloaded`, `Skipped`, `Failed`, `Bytes`.

**Enforced in:** `main()`.

## R10 — Exit non-zero if anything failed

If `total_failed > 0` after all underlyings/years are processed, `main()`
must `sys.exit(1)`. A clean run exits 0. `--dry-run` exits 0 regardless.

**Why:** cron / CI wrappers rely on exit codes to decide whether to alert.

**Enforced in:** `main()` end + the `__main__` block.

---

## Rule → code → test map

| Rule | Code site | Test |
|------|-----------|------|
| R1 | `login_and_get_token` (no disk write) | `test_no_token_write_in_source` |
| R2 | `main()` skip block | `test_skip_existing_canonical`, `test_skip_existing_browser_renamed` |
| R3 | `download_with_resume` | `test_resume_sends_range_header`, `test_416_treats_part_as_complete` |
| R4 | `verify_zip` + post-promote | `test_verify_zip_accepts_good`, `test_verify_zip_rejects_corrupt` |
| R5 | `backoff_delay` + retry loops | `test_backoff_is_capped_at_30s`, `test_backoff_has_jitter`, `test_backoff_grows_exponentially` |
| R6 | `_login_and_get_token_async` | (manual — needs a real browser) |
| R7 | `download_with_resume` re-signs | `test_resigns_per_attempt` |
| R8 | `main()` `part.replace(dest)` | `test_failed_rename_counts_as_failure` |
| R9 | `main()` tags + summary | `test_summary_has_four_counts` |
| R10 | `main()` exit code | `test_nonzero_exit_when_failures` |
