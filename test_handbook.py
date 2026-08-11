"""
HANDBOOK conformance tests for download_monthly_archives.py.

Each test maps to a rule in HANDBOOK.md (R1..R10). Run directly:

    python test_handbook.py

Stdlib only — no pytest dependency. Mocks HTTP via fakes; never touches the
network or a real browser.
"""
import io
import os
import sys
import tempfile
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch, MagicMock

# Stub nodriver so the import never tries to launch Chrome during tests.
if "nodriver" not in sys.modules or not hasattr(sys.modules.get("nodriver", None), "start"):
    sys.modules["nodriver"] = MagicMock(name="nodriver_stub")

import download_monthly_archives as mod


# ---------- helpers ----------

@contextmanager
def temp_outdir():
    d = Path(tempfile.mkdtemp(prefix="handbook_test_"))
    yield d
    import shutil
    shutil.rmtree(d, ignore_errors=True)


class FakeResponse:
    def __init__(self, status_code=200, headers=None, content=b"", json_data=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content
        self._json = json_data
    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
    def json(self):
        return self._json or {}
    def __enter__(self): return self
    def __exit__(self, *a): return False


# ---------- R1: no token on disk ----------

def test_no_token_write_in_source():
    """R1: scan the script's own source for any open(...,'w') that writes a
    token-shaped value. Token should only flow through HTTP headers."""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src) if False else None  # not parsing — substring scan is enough
    # Forbidden patterns: writing to a path whose name suggests auth/session/token
    forbidden_substrings = [
        'open("session.json"',
        "open('session.json'",
        "session.json",
        "open(\"token",
        "open('token",
    ]
    found = [s for s in forbidden_substrings if s in src]
    assert not found, f"R1 violation — source writes a token-shaped file: {found}"
    # Sanity: token does flow through headers (positive evidence the rule is meaningful)
    assert "authorization" in src.lower() or "Authorization" in src, "R1: token isn't used at all?"



# ---------- R5: backoff ----------

def test_backoff_is_capped_at_30s():
    max_seen = max(mod.backoff_delay(a) for a in range(20))
    assert max_seen <= 31.0, f"R5 cap violated: {max_seen} > 30s"


def test_backoff_has_jitter():
    vals = {mod.backoff_delay(3) for _ in range(20)}
    assert len(vals) > 1, "R5 jitter missing — backoff is deterministic"


def test_backoff_grows_exponentially():
    avg_low = sum(mod.backoff_delay(0) for _ in range(50)) / 50
    avg_high = sum(mod.backoff_delay(5) for _ in range(50)) / 50
    assert avg_high > avg_low * 10, f"R5 growth not exponential: low={avg_low:.2f} high={avg_high:.2f}"


# ---------- R4: zip CRC ----------

def test_verify_zip_accepts_good():
    with temp_outdir() as d:
        p = d / "good.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", "hello")
        mod.verify_zip(p)  # must not raise


def test_verify_zip_rejects_corrupt():
    with temp_outdir() as d:
        p = d / "bad.zip"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("a.txt", "hello")
        # flip a byte in the central directory region
        raw = bytearray(p.read_bytes())
        raw[-5] ^= 0xFF
        p.write_bytes(raw)
        try:
            mod.verify_zip(p)
        except RuntimeError:
            return
        raise AssertionError("R4: corrupt zip was accepted")


# ---------- R2: idempotent skip ----------

def _fake_downloader(byte_count):
    """Returns a callable matching download_with_resume's signature that writes
    a valid zip to dest.part and returns byte_count. Needed because main()
    promotes .part -> final via Path.replace, which requires .part to exist."""
    def _impl(session, token, file_name, dest_final):
        part = dest_final.with_suffix(dest_final.suffix + ".part")
        with zipfile.ZipFile(part, "w") as zf:
            zf.writestr("x.txt", "x")
        return byte_count
    return _impl


def _raising_downloader(exc):
    def _impl(*a, **k):
        raise exc
    return _impl


def _run_main_with_stubs(out_dir, list_files_ret, download_ret, verify_ret=None,
                          extra_argv=None):
    """Invoke main() with network and browser stubbed. Returns (stdout, exit_code_or_None).
    download_ret: an exception/class to raise, or an int (success byte count)."""
    argv = ["prog", "--out-dir", str(out_dir), "--underlying", "BTC", "--years", "2025"]
    if extra_argv:
        argv += extra_argv
    captured = io.StringIO()
    code = None

    is_exc = isinstance(download_ret, BaseException) or (
        isinstance(download_ret, type) and issubclass(download_ret, BaseException)
    )
    dl_side = _raising_downloader(download_ret) if is_exc else _fake_downloader(download_ret)

    with patch.object(mod, "login_and_get_token", return_value="fake-token"), \
         patch.object(mod, "list_files", return_value=list_files_ret), \
         patch.object(mod, "download_with_resume", side_effect=dl_side), \
         patch.object(mod, "verify_zip", side_effect=verify_ret if verify_ret else (lambda p: None)), \
         patch.object(sys, "argv", argv), \
         redirect_stdout(captured):
        try:
            mod.main()
        except SystemExit as e:
            code = e.code
    return captured.getvalue(), code


def test_skip_existing_canonical():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        (d / "BTC-2025-09.csv.zip").write_bytes(b"x")
        out, code = _run_main_with_stubs(d, files, download_ret=RuntimeError("should not be called"))
    assert "[skip]" in out, "R2: canonical existing file was not skipped"
    assert "should not be called" not in out


def test_skip_existing_browser_renamed():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        (d / "BTC-2025-09.csv (1).zip").write_bytes(b"x")
        out, code = _run_main_with_stubs(d, files, download_ret=RuntimeError("should not be called"))
    assert "[skip]" in out, "R2: browser-renamed duplicate was not skipped"


# ---------- R3: Range resume ----------

def test_resume_sends_range_header():
    captured = {}
    def fake_get(url, stream=False, timeout=None, headers=None):
        captured["headers"] = headers or {}
        return FakeResponse(status_code=206, headers={"Content-Length": "10"}, content=b"x" * 10)
    fake_session = MagicMock()
    fake_session.get = fake_get
    with temp_outdir() as d:
        dest = d / "foo.csv.zip"
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(b"0123456789")  # 10 bytes already on disk
        with patch.object(mod, "get_signed_url", return_value="http://fake/url"):
            n = mod.download_with_resume(fake_session, "fake-token", "foo.csv.zip", dest)
    assert captured["headers"].get("Range") == "bytes=10-", f"R3: Range header wrong: {captured['headers']}"
    assert n == 20, f"R3: expected total=20 (10 existing + 10 new), got {n}"


def test_416_treats_part_as_complete():
    fake_session = MagicMock()
    fake_session.get = lambda *a, **k: FakeResponse(status_code=416)
    with temp_outdir() as d:
        dest = d / "foo.csv.zip"
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(b"0" * 1234)
        with patch.object(mod, "get_signed_url", return_value="http://fake/url"):
            n = mod.download_with_resume(fake_session, "fake-token", "foo.csv.zip", dest)
    assert n == 1234, f"R3: 416 should treat 1234-byte part as complete, got {n}"


# ---------- R7: re-sign per attempt ----------

def test_resigns_per_attempt():
    sign_calls = {"n": 0}
    def fake_sign(*a, **k):
        sign_calls["n"] += 1
        return "http://fake/url"
    attempts = {"n": 0}
    def fake_get(url, stream=False, timeout=None, headers=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise OSError("simulated mid-stream drop")
        return FakeResponse(status_code=200, headers={"Content-Length": "5"}, content=b"abcde")
    fake_session = MagicMock()
    fake_session.get = fake_get
    with temp_outdir() as d:
        dest = d / "foo.csv.zip"
        with patch.object(mod, "get_signed_url", side_effect=fake_sign):
            mod.download_with_resume(fake_session, "fake-token", "foo.csv.zip", dest)
    assert sign_calls["n"] >= 2, f"R7: get_signed_url must be called per attempt, got {sign_calls['n']}"


# ---------- R8: atomic promote, no half-written final file ----------

def test_failed_download_leaves_no_final_file():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        out, code = _run_main_with_stubs(d, files, download_ret=RuntimeError("network dead"))
        # the final filename must NOT exist; only possibly a .part
        assert not (d / "BTC-2025-09.csv.zip").exists(), "R8: final file present after a failed download"


# ---------- R9: output discipline ----------

def test_summary_has_four_counts():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        out, _ = _run_main_with_stubs(d, files, download_ret=12345)
    for tag in ("Downloaded", "Skipped", "Failed", "Bytes"):
        assert tag in out, f"R9: summary missing '{tag}'"


def test_per_file_log_tags():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        out, _ = _run_main_with_stubs(d, files, download_ret=12345)
    assert "[get ]" in out and "[zip OK]" in out, "R9: per-file tags missing"


# ---------- R10: exit code ----------

def test_nonzero_exit_when_failures():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        out, code = _run_main_with_stubs(d, files, download_ret=RuntimeError("boom"))
    assert code == 1, f"R10: expected exit 1 on failure, got {code}"


def test_zero_exit_on_success():
    files = [{"file_name": "BTC-2025-09.csv.zip", "month": "09"}]
    with temp_outdir() as d:
        out, code = _run_main_with_stubs(d, files, download_ret=12345)
    assert code is None or code == 0, f"R10: expected clean exit on success, got {code}"


def test_zero_exit_on_dry_run_even_with_no_files():
    # dry-run must always exit 0
    with temp_outdir() as d:
        out, code = _run_main_with_stubs(d, [], download_ret=RuntimeError("nope"),
                                          extra_argv=["--dry-run"])
    assert code is None or code == 0, f"R10: dry-run must exit clean, got {code}"


# ---------- runner ----------

def run_all():
    tests = sorted(name for name, v in globals().items() if name.startswith("test_") and callable(v))
    passed = 0
    failed = []
    for name in tests:
        try:
            globals()[name]()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {name}: {type(e).__name__}: {e}")
            failed.append((name, str(e)))
    print()
    print(f"Total: {len(tests)}   passed={passed}   failed={len(failed)}")
    for name, err in failed:
        print(f"  FAILED: {name} — {err}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(run_all())
