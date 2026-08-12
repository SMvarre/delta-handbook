# delta-handbook

Auto-download and ingest Delta Exchange monthly options/futures trade archives into local DuckDB databases.

## What's here

| File | Purpose |
|---|---|
| `HANDBOOK.md` | Binding spec — 10 rules for the monthly downloader, each with rationale, code pointer, and test |
| `download_monthly_archives.py` | Implementation of HANDBOOK. Opens Chrome via nodriver, you log in once, pulls auth token from localStorage, downloads missing monthly archives with HTTP Range-resume, zip-CRC verify, atomic `.part` rename |
| `test_handbook.py` | Conformance tests for the handbook (17/17 pass). Stdlib only — no pytest |
| `download_trades.py` | Live trade-history downloader for Delta's REST API |
| `load_options_db.py` | Ingests `options_data/*.csv.zip` archives into per-asset DuckDB |
| `month_counts.py` | Quick utility — prints row counts per month |

## Quick start

```bash
# install dependencies (creates .venv)
uv sync

# 1. Log in once (Chrome opens, token stays in memory only)
uv run python download_monthly_archives.py --underlying BTC,ETH --contract-type options

# 2. Load the archives into DuckDB
uv run python load_options_db.py

# 3. Verify the handbook conformance suite
uv run python test_handbook.py
```

Requires [uv](https://docs.astral.sh/uv/) (`pip install uv` or `brew install uv`).

## Requirements

- Python 3.11+
- `nodriver`, `requests`, `duckdb`, `playwright` (pinned in `pyproject.toml`)
- Chrome installed (for the login flow)

## Why a handbook?

The downloader grew organically and had bugs: resume didn't actually resume, zip files weren't verified, failed downloads left half-written final files, S3 URLs expired mid-stream. The handbook locks in the rules that fix those issues; the test suite enforces them. See [`HANDBOOK.md`](HANDBOOK.md) for the 10 rules.
