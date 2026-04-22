#!/usr/bin/env python3
"""
insider_signals.py — SEC EDGAR Form 4 insider buy detection.

Round-11 expansion (item 7). Cluster insider buying (3+ insiders
buying within 30 days) is a well-documented alpha factor. This
module fetches EDGAR's full-text search RSS for Form 4 filings,
counts buys per ticker, and surfaces a +0..+15 score bonus to
the screener.

Free, no API key required. Polite to SEC by:
  - Setting User-Agent with contact email (required by EDGAR)
  - 1-second sleep between requests (SEC rate limit guidance)
  - 24-hour cache so we hit EDGAR at most 1× per day per symbol

Public API:

    fetch_insider_buys(symbol, days=30) -> dict
        {ticker, buy_count, buyer_count, total_value_usd,
         most_recent_date, has_cluster_buy: bool, raw_filings: list}

    insider_score_bonus(insider_data) -> int
        Convert to 0..15 score bonus. Cluster (3+ buyers) → +10,
        each additional buyer → +1, recent (<7d) → +3.

    enrich_picks_with_insiders(picks, top_n=20) -> picks
        Adds `insider_data` and `insider_bonus` to each pick.
        Boosts breakout + pead scores by the bonus.

CONFIG:
    EDGAR_USER_AGENT — required header. Defaults to
        "alpaca-bot kevinbell@example.com" but Kevin should set his
        own (SEC bans persistent abusers).
"""
from __future__ import annotations
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
import re

try:
    from et_time import now_et
except ImportError:
    def now_et():
        return datetime.now()


EDGAR_USER_AGENT = os.environ.get(
    "EDGAR_USER_AGENT",
    "alpaca-bot/1.0 contact@example.com"
)


def _cache_dir():
    base = os.environ.get("DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    d = os.path.join(base, "insider_cache")
    os.makedirs(d, exist_ok=True)
    return d


def _cache_path(symbol):
    return os.path.join(_cache_dir(), f"{symbol.upper()}.json")


def _read_cache(symbol, max_age_seconds=86400):
    path = _cache_path(symbol)
    try:
        if not os.path.exists(path):
            return None
        if time.time() - os.path.getmtime(path) > max_age_seconds:
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        # Matches llm_sentiment._read_cache — narrow catch lets genuine
        # code bugs surface while still treating corrupt-file / missing-
        # file / permission as a cache miss.
        return None


def _write_cache(symbol, data):
    path = _cache_path(symbol)
    tmp = None
    try:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, default=str)
        os.rename(tmp, path)
    except (OSError, TypeError, ValueError) as e:
        # Silent cache-write failure meant the same EDGAR Form 4 scan
        # would re-run on every refresh, hitting SEC rate limits. Route
        # through observability so we see systematic breakage.
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass
        try:
            from observability import capture_exception
            capture_exception(e, component="insider_signals_cache_write")
        except ImportError:
            pass


_LAST_REQUEST_TIME = 0.0


def _polite_sleep():
    """SEC EDGAR asks for ≤10 req/sec — we go even slower (1/sec)."""
    global _LAST_REQUEST_TIME
    elapsed = time.time() - _LAST_REQUEST_TIME
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
    _LAST_REQUEST_TIME = time.time()


def _fetch_edgar_form4(symbol, days=30):
    """Hit EDGAR's full-text search for Form 4 filings on this ticker
    in the last `days`. Returns parsed list of {filed_date, filer,
    transaction_type}. Empty on error (caller falls back to neutral)."""
    _polite_sleep()
    end = now_et().date()
    start = end - timedelta(days=days)
    # EDGAR full-text search endpoint
    url = (
        f"https://efts.sec.gov/LATEST/search-index?"
        f"q=%22{urllib.parse.quote(symbol.upper())}%22"
        f"&dateRange=custom"
        f"&startdt={start.isoformat()}"
        f"&enddt={end.isoformat()}"
        f"&forms=4"
    )
    req = urllib.request.Request(url, headers={
        "User-Agent": EDGAR_USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return [], str(e)
    hits = (data.get("hits") or {}).get("hits") or []
    parsed = []
    for h in hits:
        src = h.get("_source", {})
        # filer: first display_names entry; date: from `file_date`
        filer = (src.get("display_names") or ["?"])[0]
        filed = src.get("file_date") or src.get("filing_date") or ""
        parsed.append({
            "filer": filer,
            "filed_date": filed,
            "form": src.get("form_type", "4"),
            "accession": h.get("_id", ""),
        })
    return parsed, None


# Round-59: Form 4 XML parser.
#
# Form 4 is the SEC filing every insider must submit when they trade
# their company's stock. The full-text search (above) only returns
# filing metadata — the actual transaction details (purchase vs sale,
# share count, price per share) live in the primary doc XML. Parsing
# it lets us compute a real `total_value_usd` for "open-market
# purchases" (transaction code P), the only insider buy that actually
# signals conviction.
#
# Filing accession format we receive: "0001628280-26-023978:wk-form4_1775526679.xml"
# (accession_id : primary_doc_filename). The accession's first 10 digits
# are the filer's CIK (with leading zeros). EDGAR archive URL:
#   https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}
#
# Budget: max 5 XML fetches per fetch_insider_buys() call (~5 sec at
# 1 req/sec rate limit). Cached per accession indefinitely — Form 4s
# don't change after filing — so the cost is amortised across all
# subsequent screener runs.

import xml.etree.ElementTree as _ET


def _form4_xml_cache_path(accession_id):
    """Cache parsed dollar value per accession (one-time fetch ever)."""
    safe = accession_id.replace("/", "_").replace(":", "_").replace(" ", "_")
    return os.path.join(_cache_dir(), f"_xml_{safe}.json")


def _read_form4_xml_cache(accession_id):
    path = _form4_xml_cache_path(accession_id)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _write_form4_xml_cache(accession_id, data):
    path = _form4_xml_cache_path(accession_id)
    tmp = None
    try:
        d = os.path.dirname(path)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.rename(tmp, path)
    except (OSError, TypeError, ValueError):
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass


def _form4_archive_url(accession_with_doc):
    """Build the SEC archive URL for a Form 4 primary doc.
    Input: "0001628280-26-023978:wk-form4_1775526679.xml"
    Output: full https URL or None on parse failure."""
    if not accession_with_doc or ":" not in accession_with_doc:
        return None
    accession, primary_doc = accession_with_doc.split(":", 1)
    # accession format: NNNNNNNNNN-YY-NNNNNN  (CIK-YY-Seq)
    parts = accession.split("-")
    if len(parts) != 3 or not parts[0].isdigit():
        return None
    filer_cik = parts[0].lstrip("0") or "0"  # strip leading zeros for URL
    accession_no_dashes = "".join(parts)
    return (f"https://www.sec.gov/Archives/edgar/data/"
            f"{filer_cik}/{accession_no_dashes}/{primary_doc}")


def parse_form4_purchase_value(accession_with_doc):
    """Fetch + parse a single Form 4 XML, summing the dollar value of
    open-market PURCHASE transactions only (transaction code "P").

    Returns dict {usd: float | None, transactions: int, status: str}
    where status ∈ {"parsed", "no_purchase", "fetch_error", "parse_error",
    "bad_accession"}. None usd means "no purchase found" or "fetch failed".

    Cached per accession indefinitely — Form 4 filings don't change
    after submission."""
    if not accession_with_doc:
        return {"usd": None, "transactions": 0, "status": "bad_accession"}
    cached = _read_form4_xml_cache(accession_with_doc)
    if cached is not None:
        return cached
    url = _form4_archive_url(accession_with_doc)
    if not url:
        out = {"usd": None, "transactions": 0, "status": "bad_accession"}
        _write_form4_xml_cache(accession_with_doc, out)
        return out
    _polite_sleep()
    req = urllib.request.Request(url, headers={
        "User-Agent": EDGAR_USER_AGENT,
        "Accept": "application/xml",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_bytes = resp.read()
    except Exception:
        # Don't cache fetch errors — transient — let the next screener
        # run retry. (Bad accession + parse errors ARE cached because
        # they won't fix themselves.)
        return {"usd": None, "transactions": 0, "status": "fetch_error"}
    try:
        root = _ET.fromstring(xml_bytes)
    except _ET.ParseError:
        out = {"usd": None, "transactions": 0, "status": "parse_error"}
        _write_form4_xml_cache(accession_with_doc, out)
        return out
    total_usd = 0.0
    purchase_count = 0
    # nonDerivativeTable holds direct equity transactions; derivative-
    # Table holds options/warrants. We only care about direct purchases
    # (D = direct, but also count I = indirect — both are real ownership
    # changes). Filtering by transaction code "P" = open-market purchase
    # excludes A (grant), M (option exercise), G (gift), F (tax
    # withholding), S (sale), etc. Awards and exercises aren't bullish
    # signals — they're compensation, not conviction.
    for tx in root.iter():
        # Tag namespaces vary across SEC schema versions. Match on local
        # name only.
        if not tx.tag.endswith("nonDerivativeTransaction"):
            continue
        code = None
        shares = None
        price = None
        for child in tx.iter():
            local = child.tag.split("}")[-1]  # strip namespace
            if local == "transactionCode" and child.text:
                code = child.text.strip()
            elif local == "transactionShares":
                # value is a child element <value>
                v = child.find("./{*}value")
                if v is None:
                    # Try without namespace
                    for sub in child.iter():
                        if sub.tag.split("}")[-1] == "value" and sub.text:
                            shares = sub.text.strip()
                            break
                elif v.text:
                    shares = v.text.strip()
            elif local == "transactionPricePerShare":
                v = child.find("./{*}value")
                if v is None:
                    for sub in child.iter():
                        if sub.tag.split("}")[-1] == "value" and sub.text:
                            price = sub.text.strip()
                            break
                elif v.text:
                    price = v.text.strip()
        if code != "P":
            continue
        try:
            sh_f = float(shares) if shares is not None else 0.0
            px_f = float(price) if price is not None else 0.0
        except (ValueError, TypeError):
            continue
        if sh_f > 0 and px_f > 0:
            total_usd += sh_f * px_f
            purchase_count += 1
    if purchase_count == 0:
        out = {"usd": None, "transactions": 0, "status": "no_purchase"}
    else:
        out = {"usd": round(total_usd, 2), "transactions": purchase_count,
                "status": "parsed"}
    _write_form4_xml_cache(accession_with_doc, out)
    return out


# Per-call XML fetch budget. Form 4 XMLs come from sec.gov — rate-
# limited to 1 req/sec by SEC. With 10 filings per ticker × 50 picks
# enriched, that's 500 sec = 8 min added per screener run if we
# fetched everything. Cap at 5 fetches per fetch_insider_buys call —
# subsequent calls hit the cache for free since Form 4s don't change.
_FORM4_XML_BUDGET_PER_CALL = 5


def fetch_insider_buys(symbol, days=30):
    """Returns dict with insider activity summary. 24h cache.

    Round-59: `total_value_usd` is now the real summed dollar value of
    open-market PURCHASE transactions (Form 4 transaction code "P")
    parsed from each filing's primary XML. Budget-capped at 5 XML
    fetches per call (~5 sec at SEC 1 req/sec rate limit); per-
    accession results are cached indefinitely so subsequent screener
    runs hit the cache for free.

    `value_parse_status` ∈ {parsed, partial, no_purchase, not_parsed}
    where partial means we hit the budget before processing all
    filings. The cluster-buy boolean still drives `insider_bonus`
    independently — dollar value enriches the dashboard surface but
    doesn't gate the score."""
    cached = _read_cache(symbol)
    if cached is not None:
        return cached
    filings, err = _fetch_edgar_form4(symbol, days)
    if err:
        result = {
            "ticker": symbol.upper(),
            "buy_count": 0,
            "buyer_count": 0,
            "total_value_usd": None,
            "value_parse_status": "not_parsed",
            "most_recent_date": None,
            "has_cluster_buy": False,
            "error": err,
            "raw_filings": [],
        }
    else:
        unique_filers = set(f["filer"] for f in filings if f["filer"] != "?")
        most_recent = max((f["filed_date"] for f in filings if f["filed_date"]),
                           default=None)

        # Round-59: parse Form 4 XMLs to compute real purchase dollar value.
        # Budget-capped + per-accession cached. Walk the most recent
        # filings first since old ones are less actionable.
        sorted_filings = sorted(
            filings,
            key=lambda f: f.get("filed_date") or "",
            reverse=True,
        )
        total_usd = 0.0
        parsed_count = 0
        purchases_found = 0
        budget = _FORM4_XML_BUDGET_PER_CALL
        for filing in sorted_filings:
            if budget <= 0:
                break
            # Only count uncached fetches against the budget — cached
            # accessions are free.
            cached_xml = _read_form4_xml_cache(filing.get("accession", ""))
            if cached_xml is None:
                budget -= 1
            xml_result = parse_form4_purchase_value(filing.get("accession", ""))
            parsed_count += 1
            if xml_result.get("usd") is not None:
                total_usd += xml_result["usd"]
                purchases_found += xml_result.get("transactions", 0)
            # Annotate the filing with its parse status for downstream
            # transparency on the dashboard.
            filing["purchase_usd"] = xml_result.get("usd")
            filing["purchase_status"] = xml_result.get("status")

        # Status enum:
        #   parsed       — all filings parsed AND at least one purchase
        #   no_purchase  — all filings parsed but none were open-market buys
        #   partial      — budget exhausted before processing every filing
        #   not_parsed   — no filings parsed (none in the search hits)
        if parsed_count == 0:
            value_status = "not_parsed"
            value = None
        elif parsed_count < len(sorted_filings):
            value_status = "partial"
            value = round(total_usd, 2) if purchases_found > 0 else None
        elif purchases_found == 0:
            value_status = "no_purchase"
            value = None
        else:
            value_status = "parsed"
            value = round(total_usd, 2)

        result = {
            "ticker": symbol.upper(),
            "buy_count": len(filings),
            "buyer_count": len(unique_filers),
            "total_value_usd": value,
            "value_parse_status": value_status,
            "most_recent_date": most_recent,
            "has_cluster_buy": len(unique_filers) >= 3,
            "raw_filings": sorted_filings[:10],  # cap stored to 10
            "computed_at": now_et().isoformat(),
        }
    _write_cache(symbol, result)
    return result


def insider_score_bonus(insider_data):
    """Convert insider activity to 0..15 score bonus.
      cluster (3+ filers in 30d): +10
      additional filer beyond 3:  +1 each (cap +5 → max 15)
      most recent within 7 days:  +3
    Returns int."""
    if not insider_data or insider_data.get("error"):
        return 0
    buyer_count = int(insider_data.get("buyer_count", 0) or 0)
    bonus = 0
    if buyer_count >= 3:
        bonus += 10
    if buyer_count > 3:
        bonus += min(5, buyer_count - 3)
    most_recent = insider_data.get("most_recent_date")
    if most_recent:
        try:
            d = datetime.strptime(most_recent[:10], "%Y-%m-%d").date()
            days_ago = (now_et().date() - d).days
            if days_ago <= 7:
                bonus += 3
        except (ValueError, TypeError):
            pass
    return min(15, bonus)


def enrich_picks_with_insiders(picks, top_n=20):
    """Mutates top-N picks: adds insider_data + insider_bonus + applies
    bonus to breakout_score and pead_score. Skips picks beyond top_n
    to respect EDGAR rate limits."""
    if not picks:
        return picks
    for i, p in enumerate(picks):
        if i >= top_n:
            p["insider_bonus"] = 0
            continue
        sym = p.get("symbol", "").upper()
        if not sym:
            continue
        try:
            data = fetch_insider_buys(sym, days=30)
            p["insider_data"] = data
            bonus = insider_score_bonus(data)
            p["insider_bonus"] = bonus
            for k in ("breakout_score", "pead_score"):
                if k in p and isinstance(p[k], (int, float)):
                    p[k] = round(p[k] + bonus, 2)
        except Exception as e:
            p["insider_bonus"] = 0
            p["insider_error"] = str(e)
    return picks


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    print(f"Fetching insider buys for {sym}...")
    r = fetch_insider_buys(sym)
    print(json.dumps(r, indent=2, default=str))
    print(f"Score bonus: {insider_score_bonus(r)}")
