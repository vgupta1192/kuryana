"""Fetch MyDramaList pages, falling back to trawl when Cloudflare blocks us.

MyDramaList sits behind a Cloudflare managed challenge that blocks the VPS IP
(403 + `cf-mitigated: challenge`). Order of attempts:

1. Plain primp request (works whenever Cloudflare is not challenging).
2. Reuse the last trawl clearance: its cookies + user agent, sent through the
   same egress trawl solved from (warp proxy), with a Firefox fingerprint to
   match trawl's browser. ~0.2s instead of a browser solve.
3. Full solve through trawl's FlareSolverr-compatible `/v1` API (5-15s); the
   resulting clearance is cached for step 2.

The clearance is saved to CLEARANCE_FILE so a restart does not force a new
solve, and a background thread renews it once it is REFRESH_AGE_HOURS old (or
stops working), so user requests rarely wait on a browser solve.
"""

import json
import os
import threading
import time
import urllib.request
from typing import Dict, Optional, Tuple

import primp

TRAWL_URL = os.environ.get("TRAWL_URL", "").rstrip("/")
TRAWL_TIMEOUT_MS = int(os.environ.get("TRAWL_TIMEOUT_MS", "60000"))
# egress trawl solves through; the cf_clearance cookie is bound to that IP
TRAWL_EGRESS_PROXY = os.environ.get("TRAWL_EGRESS_PROXY", "")

CLEARANCE_FILE = os.environ.get("CLEARANCE_FILE", "")
REFRESH_AGE_HOURS = float(os.environ.get("CLEARANCE_REFRESH_AGE_HOURS", "20"))
REFRESH_CHECK_HOURS = float(os.environ.get("CLEARANCE_CHECK_HOURS", "3"))
REFRESH_URL = "https://mydramalist.com/"

_lock = threading.Lock()
_clearance: Optional[Dict] = None  # {"cookies": {...}, "ua": str, "time": epoch}


def _load_clearance() -> None:
    global _clearance
    if not CLEARANCE_FILE:
        return
    try:
        with open(CLEARANCE_FILE) as f:
            data = json.load(f)
        if data.get("cookies") and data.get("ua"):
            _clearance = data
    except Exception:
        pass


def _save_clearance(data: Dict) -> None:
    if not CLEARANCE_FILE:
        return
    try:
        tmp = CLEARANCE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CLEARANCE_FILE)
    except Exception:
        pass


def _is_challenge(status: int, text: str) -> bool:
    if status in (403, 503):
        return True
    head = text[:4000]
    return "Just a moment" in head and "cloudflare" in head.lower()


def _get(url: str, **kw) -> Tuple[int, str]:
    resp = primp.Client(**kw).get(url)
    return resp.status_code, resp.text


def _with_clearance(url: str, clearance: Dict) -> Tuple[int, str]:
    kw = {
        "impersonate": "firefox",
        "headers": {"User-Agent": clearance["ua"]},
        "cookies": clearance["cookies"],
    }
    proxies = [TRAWL_EGRESS_PROXY, None] if TRAWL_EGRESS_PROXY else [None]
    status, text = 403, ""
    for proxy in proxies:
        try:
            status, text = _get(url, proxy=proxy, **kw) if proxy else _get(url, **kw)
        except Exception:
            continue
        if not _is_challenge(status, text):
            return status, text
    return status, text


def _via_trawl(url: str) -> Tuple[int, str]:
    global _clearance
    body = json.dumps(
        {"cmd": "request.get", "url": url, "maxTimeout": TRAWL_TIMEOUT_MS}
    ).encode()
    req = urllib.request.Request(
        f"{TRAWL_URL}/v1",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TRAWL_TIMEOUT_MS / 1000 + 15) as r:
        data = json.loads(r.read())
    solution = data.get("solution") or {}
    if data.get("status") != "ok":
        return 502, ""
    text = solution.get("response") or ""
    status = int(solution.get("status") or 200)
    if _is_challenge(status, text):
        return 403, text

    cookies = {c["name"]: c["value"] for c in solution.get("cookies") or []}
    if "cf_clearance" in cookies and solution.get("userAgent"):
        data = {"cookies": cookies, "ua": solution["userAgent"], "time": time.time()}
        with _lock:
            _clearance = data
        _save_clearance(data)
    return status, text


def fetch_html(url: str) -> Tuple[int, str]:
    """Return (status_code, html) for a MyDramaList URL."""
    status, text = 500, ""
    try:
        status, text = _get(url, impersonate="chrome", impersonate_os="linux")
    except Exception:
        pass

    if not TRAWL_URL or not (status == 500 or _is_challenge(status, text)):
        return status, text

    clearance = _clearance
    if clearance:
        c_status, c_text = _with_clearance(url, clearance)
        if not _is_challenge(c_status, c_text):
            return c_status, c_text

    try:
        return _via_trawl(url)
    except Exception:
        return 502, text


def _refresh_loop() -> None:
    while True:
        try:
            clearance = _clearance
            stale = (
                clearance is None
                or time.time() - clearance.get("time", 0) > REFRESH_AGE_HOURS * 3600
            )
            if not stale:
                status, text = _with_clearance(REFRESH_URL, clearance)
                stale = _is_challenge(status, text)
            if stale:
                # a plain request that is not challenged means no clearance is needed
                status, text = 500, ""
                try:
                    status, text = _get(
                        REFRESH_URL, impersonate="chrome", impersonate_os="linux"
                    )
                except Exception:
                    pass
                if _is_challenge(status, text) or status == 500:
                    _via_trawl(REFRESH_URL)
        except Exception:
            pass
        time.sleep(REFRESH_CHECK_HOURS * 3600)


_load_clearance()
if TRAWL_URL:
    threading.Thread(target=_refresh_loop, name="cf-refresh", daemon=True).start()
