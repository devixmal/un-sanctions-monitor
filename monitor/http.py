"""Shared HTTP session with retries and a polite User-Agent."""
from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (compatible; UNSanctionsMonitor/1.0; "
    "+https://github.com/) python-requests"
)


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})
    return s


SESSION = make_session()


def get(url: str, *, params=None, timeout: int = 60, rate_limit_waits=(30, 60, 120), **kw):
    """GET with extra back-off on HTTP 429 (rate limiting)."""
    resp = SESSION.get(url, params=params, timeout=timeout, **kw)
    for wait in rate_limit_waits:
        if resp.status_code != 429:
            break
        time.sleep(wait)
        resp = SESSION.get(url, params=params, timeout=timeout, **kw)
    return resp
