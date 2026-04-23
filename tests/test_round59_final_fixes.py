"""
Round-59 final pre-live fixes:
  A. Form 4 XML parser computes real total_value_usd from SEC archive
     (was null + value_parse_status="not_parsed" since round-58)
  B. Migrations now run under per-user flock — multi-process Railway
     boots can't race the read-check-apply cycle
  C. Coverage floor bumped 25 → 30 (actual is 33% as of round-59)
"""
from __future__ import annotations

import os
import sys
import tempfile


# ========= Fix A: Form 4 XML parser =========

def test_form4_archive_url_construction():
    """The accession-with-doc string from EDGAR full-text search must
    parse to the canonical SEC archive URL."""
    import insider_signals as _isig
    url = _isig._form4_archive_url(
        "0001628280-26-023978:wk-form4_1775526679.xml")
    assert url is not None
    assert "Archives/edgar/data/1628280/" in url, (
        "URL must use issuer CIK with leading zeros stripped")
    assert "000162828026023978/wk-form4_1775526679.xml" in url, (
        "URL must use accession-with-dashes-stripped + primary doc filename")


def test_form4_archive_url_handles_bad_input():
    import insider_signals as _isig
    assert _isig._form4_archive_url("") is None
    assert _isig._form4_archive_url(None) is None
    # Missing colon (no primary doc filename)
    assert _isig._form4_archive_url("0001628280-26-023978") is None
    # Wrong dash count
    assert _isig._form4_archive_url("badformat:doc.xml") is None


def test_form4_xml_parser_extracts_purchase_value(monkeypatch):
    """Given a synthetic Form 4 XML with one P transaction and one S,
    the parser must sum only the P transaction's shares × price."""
    import insider_signals as _isig

    # Inline fake XML in SEC schema-shape. The real XML uses namespace
    # http://www.sec.gov/edgar/ownership/v1 — our parser strips ns so
    # this works without the prefix.
    fake_xml = b"""<?xml version="1.0"?>
    <ownershipDocument>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <transactionAmounts>
            <transactionShares><value>1000</value></transactionShares>
            <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
          </transactionAmounts>
          <transactionCoding>
            <transactionCode>P</transactionCode>
          </transactionCoding>
        </nonDerivativeTransaction>
        <nonDerivativeTransaction>
          <transactionAmounts>
            <transactionShares><value>500</value></transactionShares>
            <transactionPricePerShare><value>60.00</value></transactionPricePerShare>
          </transactionAmounts>
          <transactionCoding>
            <transactionCode>S</transactionCode>
          </transactionCoding>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""

    # Stub urlopen to return our fake XML
    class _FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def _fake_urlopen(req, timeout=15):
        return _FakeResp(fake_xml)

    # Use a tmpdir so the XML cache doesn't collide with real cache
    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(_isig, "_cache_dir", lambda: tmp)
    monkeypatch.setattr(_isig.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)

    result = _isig.parse_form4_purchase_value(
        "0001628280-26-023978:wk-form4_1775526679.xml")
    assert result["status"] == "parsed"
    # 1000 × $50 = $50,000  (S transaction excluded)
    assert result["usd"] == 50000.00
    assert result["transactions"] == 1


def test_form4_xml_parser_no_purchase_status(monkeypatch):
    """A filing with only sales / grants / exercises returns
    status=no_purchase + usd=None."""
    import insider_signals as _isig
    fake_xml = b"""<?xml version="1.0"?>
    <ownershipDocument>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <transactionAmounts>
            <transactionShares><value>500</value></transactionShares>
            <transactionPricePerShare><value>60.00</value></transactionPricePerShare>
          </transactionAmounts>
          <transactionCoding>
            <transactionCode>S</transactionCode>
          </transactionCoding>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""

    class _FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(_isig, "_cache_dir", lambda: tempfile.mkdtemp())
    monkeypatch.setattr(_isig.urllib.request, "urlopen",
                         lambda r, timeout=15: _FakeResp(fake_xml))
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)

    result = _isig.parse_form4_purchase_value(
        "0001628280-26-023978:wk-form4_1775526679.xml")
    assert result["status"] == "no_purchase"
    assert result["usd"] is None
    assert result["transactions"] == 0


def test_form4_xml_parser_caches_results(monkeypatch):
    """A second call for the same accession must hit the cache and
    NOT call urlopen again — Form 4s never change after submission."""
    import insider_signals as _isig

    fake_xml = b"""<?xml version="1.0"?><ownershipDocument><nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>10</value></transactionPricePerShare>
      </transactionAmounts>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    </nonDerivativeTransaction>
    </nonDerivativeTable></ownershipDocument>"""

    class _FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    call_count = {"n": 0}

    def _fake_urlopen(req, timeout=15):
        call_count["n"] += 1
        return _FakeResp(fake_xml)

    # Stable tmpdir across both _cache_dir calls — without this the
    # lambda mints a new dir per call and the cache file lands in
    # different places for the read vs write.
    stable_tmp = tempfile.mkdtemp()
    monkeypatch.setattr(_isig, "_cache_dir", lambda: stable_tmp)
    monkeypatch.setattr(_isig.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)

    acc = "0001628280-26-023978:wk-form4_1.xml"
    r1 = _isig.parse_form4_purchase_value(acc)
    r2 = _isig.parse_form4_purchase_value(acc)
    assert r1 == r2
    assert call_count["n"] == 1, (
        "Second call must hit the cache — Form 4 cache invalidation bug")


def test_form4_xml_parser_bad_xml_caches_parse_error(monkeypatch):
    """Parse errors are cached (the XML won't fix itself); fetch errors
    are NOT cached (transient)."""
    import insider_signals as _isig

    bad_xml = b"<<not-valid-xml>"

    class _FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(_isig, "_cache_dir", lambda: tempfile.mkdtemp())
    monkeypatch.setattr(_isig.urllib.request, "urlopen",
                         lambda r, timeout=15: _FakeResp(bad_xml))
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)

    result = _isig.parse_form4_purchase_value(
        "0001628280-26-023978:wk-form4.xml")
    assert result["status"] == "parse_error"
    assert result["usd"] is None


def test_form4_xml_parser_fetch_error_not_cached(monkeypatch):
    """Network/timeout errors must NOT be cached so the next screener
    run can retry. Bad-accession + parse errors ARE cached."""
    import insider_signals as _isig

    def _broken_urlopen(req, timeout=15):
        raise OSError("simulated network timeout")

    monkeypatch.setattr(_isig, "_cache_dir", lambda: tempfile.mkdtemp())
    monkeypatch.setattr(_isig.urllib.request, "urlopen", _broken_urlopen)
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)

    result = _isig.parse_form4_purchase_value(
        "0001628280-26-023978:wk-form4.xml")
    assert result["status"] == "fetch_error"
    # Verify nothing was written to cache
    cached = _isig._read_form4_xml_cache(
        "0001628280-26-023978:wk-form4.xml")
    assert cached is None, (
        "Fetch errors must NOT be cached — they're transient and the "
        "next screener run should retry")


def test_fetch_insider_buys_budget_capped(monkeypatch):
    """fetch_insider_buys must respect _FORM4_XML_BUDGET_PER_CALL —
    when the EDGAR search returns more filings than the budget, only
    the first N filings get XML-fetched and value_parse_status reflects
    "partial"."""
    import insider_signals as _isig

    # 10 distinct filings — way over the budget of 5
    fake_filings = [
        {
            "filer": f"Insider {i}",
            "filed_date": f"2026-04-{10 + i:02d}",
            "form": "4",
            "accession": f"00016282{i:02d}-26-02398{i}:wk-form4_{i}.xml",
        }
        for i in range(10)
    ]

    fake_xml = b"""<?xml version="1.0"?><ownershipDocument><nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionAmounts>
        <transactionShares><value>100</value></transactionShares>
        <transactionPricePerShare><value>10</value></transactionPricePerShare>
      </transactionAmounts>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
    </nonDerivativeTransaction>
    </nonDerivativeTable></ownershipDocument>"""

    class _FakeResp:
        def __init__(self, b): self._b = b
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    fetch_count = {"n": 0}

    def _fake_urlopen(req, timeout=15):
        fetch_count["n"] += 1
        return _FakeResp(fake_xml)

    tmp = tempfile.mkdtemp()
    monkeypatch.setattr(_isig, "_cache_dir", lambda: tmp)
    monkeypatch.setattr(_isig.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(_isig, "_polite_sleep", lambda: None)
    monkeypatch.setattr(_isig, "_fetch_edgar_form4",
                         lambda sym, days=30: (fake_filings, None))
    # Ensure no cache hit on fetch_insider_buys
    monkeypatch.setattr(_isig, "_read_cache", lambda sym: None)
    monkeypatch.setattr(_isig, "_write_cache", lambda sym, data: None)

    result = _isig.fetch_insider_buys("TEST")
    # Budget is 5 — so ≤5 fresh fetches
    assert fetch_count["n"] <= _isig._FORM4_XML_BUDGET_PER_CALL
    assert result["value_parse_status"] == "partial"
    # 5 purchases × ($100 × $10 / share) = $5,000
    assert result["total_value_usd"] is not None
    assert result["total_value_usd"] >= 5000.0


# ========= Fix B: Migrations multi-process flock =========

def test_migration_lock_serialises_concurrent_calls():
    """Two _user_migration_lock contexts on the same dir must serialise
    — the second blocks until the first releases."""
    import migrations
    import threading
    import time

    if migrations._fcntl is None:
        # Windows / no-fcntl env — degraded to no-op, can't test
        return

    tmp = tempfile.mkdtemp()
    timeline = []

    def _worker(label, hold_seconds):
        with migrations._user_migration_lock(tmp):
            timeline.append(("acquire", label, time.monotonic()))
            time.sleep(hold_seconds)
            timeline.append(("release", label, time.monotonic()))

    t1 = threading.Thread(target=_worker, args=("a", 0.2))
    t2 = threading.Thread(target=_worker, args=("b", 0.05))
    t1.start()
    time.sleep(0.05)  # let t1 grab the lock first
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    # Filter to acquire/release ordering
    events = [e[0] + ":" + e[1] for e in timeline]
    # Must be: acquire:a, release:a, acquire:b, release:b
    # (b's acquire must come AFTER a's release — that's the lock working)
    assert events.index("release:a") < events.index("acquire:b"), (
        "Lock failed to serialise — b acquired before a released. "
        f"Timeline: {events}")


def test_migration_lock_handles_missing_dir_gracefully():
    """An invalid user_dir must not crash run_all_migrations — the
    lock degrades to a no-op."""
    import migrations
    # None / empty dir → yields without locking
    with migrations._user_migration_lock(None):
        pass
    with migrations._user_migration_lock(""):
        pass


def test_migration_lock_creates_lock_file_in_user_dir():
    """The lock file path must be `<user_dir>/.migrations.lock` and
    the file must exist after entry."""
    import migrations
    if migrations._fcntl is None:
        return
    tmp = tempfile.mkdtemp()
    with migrations._user_migration_lock(tmp):
        assert os.path.exists(os.path.join(tmp, ".migrations.lock"))


def test_run_all_migrations_uses_per_user_lock(monkeypatch):
    """Grep-level: run_all_migrations must wrap each user's migration
    cycle in _user_migration_lock(user_dir)."""
    with open("migrations.py") as f:
        src = f.read()
    # Find run_all_migrations def
    idx = src.find("def run_all_migrations")
    assert idx > 0
    # Bound to next def
    next_def = src.find("\ndef ", idx + 10)
    if next_def < 0:
        next_def = len(src)
    block = src[idx:next_def]
    assert "_user_migration_lock" in block, (
        "run_all_migrations must call _user_migration_lock — round-59")


# ========= Fix C: Coverage floor bumped =========

def test_coverage_floor_at_or_above_30():
    """CI's --cov-fail-under value must be >= 30. Round-59 ratcheted
    25→30; round-61 ratcheted 30→32 after money-path tests landed.
    The ratchet only moves one direction — never lower."""
    import re
    with open(".github/workflows/ci.yml") as f:
        ci = f.read()
    m = re.search(r"--cov-fail-under=(\d+)", ci)
    assert m, "CI workflow must declare --cov-fail-under=<N>"
    floor = int(m.group(1))
    assert floor >= 30, (
        f"CI coverage floor must be >= 30 (ratchet baseline post-R59), "
        f"got {floor} — did someone lower the ratchet?")
    assert "--cov-fail-under=25" not in ci, (
        "Old 25% coverage floor must stay removed — ratchet only moves up")
