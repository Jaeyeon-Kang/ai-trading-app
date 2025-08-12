"""
EDGAR Scanner

Lightweight polling of SEC submissions API for a small ticker set.
- Maps TICKERS → CIK via https://www.sec.gov/files/company_tickers.json (cached in-memory)
- Fetches recent submissions for each CIK: https://data.sec.gov/submissions/CIK##########.json
- Emits simplified filings records suitable for Redis Streams/news.edgar and SignalMixer consumption.

Environment variables:
- EDGAR_ENABLED: if not true-ish, returns []
- SEC_USER_AGENT: required by SEC (e.g., "email@example.com; org; purpose")
- TICKERS: comma-separated symbols (e.g., "AAPL,MSFT")
- EDGAR_POLL_SEC: not used directly (beat controls schedule)

Output schema per filing (list of dicts):
{
  "ticker": str,
  "form_type": "8-K" | "4" | str,
  "items": [str],           # e.g., ["2.02"] when available
  "summary": str,           # short text if available
  "url": str,               # primary document or filings page
  "snippet_text": str,      # alias of summary
  "snippet_hash": str       # to be added later by pipeline
}
"""

from __future__ import annotations

import os
import time
import json
from typing import Dict, List, Optional

import httpx


class EDGARScanner:
    def __init__(self,
                 user_agent: Optional[str] = None,
                 tickers_csv: Optional[str] = None):
        self.user_agent = user_agent or os.getenv("SEC_USER_AGENT", "")
        self.tickers = [t.strip().upper() for t in (tickers_csv or os.getenv("TICKERS", "AAPL,MSFT")).split(",") if t.strip()]
        self._cik_cache: Dict[str, str] = {}

    def _headers(self) -> Dict[str, str]:
        ua = self.user_agent or "contact@example.com; research"
        return {
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate"
        }

    def _ensure_cik_map(self) -> None:
        if self._cik_cache:
            return
        url = "https://www.sec.gov/files/company_tickers.json"
        with httpx.Client(headers=self._headers(), timeout=10.0) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
        # data is dict with numeric keys → {"ticker": "AAPL", "cik_str": 320193, ...}
        for _, entry in data.items():
            ticker = str(entry.get("ticker", "")).upper()
            cik_str = str(entry.get("cik_str", "")).rjust(10, "0")
            if ticker:
                self._cik_cache[ticker] = cik_str

    def _fetch_submissions(self, cik_padded: str) -> Optional[Dict]:
        url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
        with httpx.Client(headers=self._headers(), timeout=10.0) as client:
            r = client.get(url)
            if r.status_code >= 400:
                return None
            return r.json()

    def _build_url(self, cik: str, accession_no: str) -> str:
        # SEC filings page URL
        acc_no = accession_no.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc_no}/{accession_no}-index.html"

    def run_scan(self) -> List[Dict]:
        enabled = os.getenv("EDGAR_ENABLED", "false").lower() in ("1", "true", "yes", "on")
        if not enabled:
            return []
        try:
            self._ensure_cik_map()
        except Exception:
            return []

        filings: List[Dict] = []
        now_epoch = int(time.time())
        # consider filings within ~24h
        try:
            for ticker in self.tickers:
                cik = self._cik_cache.get(ticker)
                if not cik:
                    continue
                data = self._fetch_submissions(cik)
                if not data:
                    continue
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                primary_docs = recent.get("primaryDocument", [])
                try:
                    items = recent.get("items", [])  # list of item arrays (for 8-K), sometimes empty
                except Exception:
                    items = []

                for i, form in enumerate(forms):
                    if form not in ("8-K", "4"):
                        continue
                    # rough freshness filter: only today or yesterday
                    fdate = dates[i] if i < len(dates) else ""
                    # Construct URL
                    accession = accessions[i] if i < len(accessions) else ""
                    url = self._build_url(cik, accession) if accession else ""
                    prim = primary_docs[i] if i < len(primary_docs) else ""
                    # items extraction (8-K)
                    item_list: List[str] = []
                    if form == "8-K" and i < len(items) and isinstance(items[i], list):
                        item_list = [str(x) for x in items[i] if x]

                    summary = f"{ticker} {form} filed {fdate} {prim}".strip()
                    filings.append({
                        "ticker": ticker,
                        "form_type": form,
                        "items": item_list,
                        "summary": summary,
                        "url": url,
                        "snippet_text": summary,
                    })
        except Exception:
            return []

        return filings


