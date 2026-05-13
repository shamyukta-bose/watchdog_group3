"""Scraper for the Responsive Learning Supply Chain Game.

The game (https://op.responsive.net/SupplyChain) exposes a small HTML+JS
status portal. Each "chart" page embeds its data points as JavaScript
arrays we can extract with a regex (no headless browser needed for the
data pages we care about).

Probed endpoints (relative to base_url):
    /SCAccess                     - login form (HTML)
    /Membership                   - login POST target
    /Login                        - alternate POST target on some installs
    /Standing                     - team rankings
    /CurrentStatus                - dashboard with key metrics
    /Plot?data=Inventory&...      - chart data pages (varies by metric)

Because Responsive's game routing changes year to year, this module is
written so each endpoint mapping lives in a single dictionary
(`ENDPOINTS`). When the login flow or page layout shifts, only that map
needs updating - the rest of the project keeps working.

If the requests-based path fails (e.g., the game now requires JS to render
the data charts), the SUPPLY_CHAIN_USE_PLAYWRIGHT=1 env var or
config["game"]["use_playwright"]=true switches to a Chromium-driven
fallback.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("scraper")


# ---- HTTP endpoints (edit these when the game's URLs change) -------------

ENDPOINTS = {
    "login_page": "/sc/riverside267/entry.html",
    # The game's actual POST handler is determined dynamically from the
    # login form's `action` attribute. We fall back to these if parsing fails.
    "login_post_fallbacks": ["/entry.html", "/SCAccess", "/Membership", "/Login"],
    "current_status": "/SupplyChain/SCAccess",
    "standing": "/SupplyChain/Standing",
    "warehouse_inventory_plot": "/SupplyChain/Plot?data=Inventory&warehouse=Calopeia",
    "factory_wip_plot": "/SupplyChain/Plot?data=WIP&factory=Calopeia",
    "demand_plot": "/SupplyChain/Plot?data=Demand&warehouse=Calopeia",
    "lost_demand_plot": "/SupplyChain/Plot?data=LostDemand&warehouse=Calopeia",
    "shipments_plot": "/SupplyChain/Plot?data=Shipments&warehouse=Calopeia",
    "cash_plot": "/SupplyChain/Plot?data=Cash",
    # Decision endpoints (you change these in the game's UI; we don't auto-submit
    # in monitor-only mode but we record the page so you can confirm settings.)
    "factory_settings": "/SupplyChain/Factory?factory=Calopeia",
    "warehouse_settings": "/SupplyChain/Warehouse?warehouse=Calopeia",
}


JS_ARRAY_RE = re.compile(
    r"(?:var|let|const)?\s*(\w+)\s*=\s*\[([\d\s,\.\-eE+]*)\]\s*;?", re.IGNORECASE
)

# Generic "key: number" extractor for the dashboard page.
KV_NUMBER_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9 _/()\-]{1,40}?)\s*[:=]\s*\$?\s*([\-+]?[0-9][0-9,\.]*)",
)


# ---- Data containers -----------------------------------------------------

@dataclass
class Snapshot:
    """One scrape's worth of game state."""
    timestamp: str
    current_day: int | None = None
    cash: float | None = None
    warehouse_inventory: float | None = None
    factory_wip: float | None = None
    factory_capacity: float | None = None
    factory_order_point: float | None = None
    factory_batch_size: float | None = None
    in_transit: float | None = None
    demand_series: list[tuple[float, float]] = field(default_factory=list)  # (day, drums)
    lost_demand_series: list[tuple[float, float]] = field(default_factory=list)
    inventory_series: list[tuple[float, float]] = field(default_factory=list)
    cash_series: list[tuple[float, float]] = field(default_factory=list)
    shipments_series: list[tuple[float, float]] = field(default_factory=list)
    wip_series: list[tuple[float, float]] = field(default_factory=list)
    standing: list[dict] = field(default_factory=list)
    raw_pages: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        # Don't dump raw HTML in JSON snapshots
        d.pop("raw_pages", None)
        return d


# ---- Scraper class -------------------------------------------------------

class GameScraperError(Exception):
    pass


class GameScraper:
    def __init__(self, config: dict):
        self.cfg = config
        self.base_url = config["game"]["base_url"].rstrip("/")
        self.team_id = config["game"]["team_id"]
        self.password = config["game"]["password"]
        self.institution = config["game"].get("institution", "")
        self.session: requests.Session | None = None

    # -- session ---------------------------------------------------------

    def login(self) -> None:
        s = requests.Session()
        s.headers["User-Agent"] = "Mozilla/5.0 (Group3 Watchdog)"
        resp = s.get(self.base_url + ENDPOINTS["login_page"], timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        form = soup.find("form")

        # Build the POST payload from the form, then override our fields.
        data: dict[str, str] = {}
        if form is not None:
            for inp in form.find_all("input"):
                name = inp.get("name")
                if name:
                    data[name] = inp.get("value", "")
        # Map our known field names; the game uses a few different naming
        # conventions across deployments.
        for k in ("id", "team", "teamId", "loginId"):
            data[k] = self.team_id
        for k in ("password", "pwd"):
            data[k] = self.password
        if self.institution:
            for k in ("institution", "school", "instId"):
                data[k] = self.institution

        post_url = None
        if form is not None and form.get("action"):
            post_url = urljoin(self.base_url + ENDPOINTS["login_page"], form["action"])
        candidates = [post_url] if post_url else []
        candidates += [self.base_url + p for p in ENDPOINTS["login_post_fallbacks"]]

        last_err = None
        for url in candidates:
            try:
                r = s.post(url, data=data, timeout=20, allow_redirects=True)
                if r.status_code == 200 and self._looks_logged_in(r.text):
                    self.session = s
                    log.info("Login OK via %s", url)
                    return
                last_err = f"HTTP {r.status_code} no login marker in {url}"
            except requests.RequestException as e:
                last_err = str(e)
                continue
        raise GameScraperError(f"Login failed; last error: {last_err}")

    @staticmethod
    def _looks_logged_in(html: str) -> bool:
        markers = ("Logout", "CurrentStatus", "Standing", "Warehouse", "Factory")
        return any(m.lower() in html.lower() for m in markers)

    def _get(self, endpoint_key: str) -> str:
        if self.session is None:
            self.login()
        url = self.base_url + ENDPOINTS[endpoint_key]
        r = self.session.get(url, timeout=30)
        r.raise_for_status()
        return r.text

    # -- parsing ---------------------------------------------------------

    @staticmethod
    def _parse_js_arrays(html: str) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for m in JS_ARRAY_RE.finditer(html):
            name = m.group(1)
            raw = m.group(2).strip()
            if not raw:
                continue
            try:
                vals = [float(x.strip()) for x in raw.split(",") if x.strip()]
                out[name] = vals
            except ValueError:
                continue
        return out

    @classmethod
    def _series_from_arrays(cls, html: str,
                            x_candidates=("days", "day", "x", "xData", "time"),
                            y_candidates=("values", "y", "yData", "data", "demand", "inventory", "cash")
                            ) -> list[tuple[float, float]]:
        arrs = cls._parse_js_arrays(html)
        x = next((arrs[k] for k in x_candidates if k in arrs), None)
        y = next((arrs[k] for k in y_candidates if k in arrs), None)
        if x is None or y is None:
            # Try generic [day, value] paired arrays via regex
            pairs = re.findall(r"\[\s*([\d\.]+)\s*,\s*([\-\d\.]+)\s*\]", html)
            return [(float(a), float(b)) for a, b in pairs] if pairs else []
        n = min(len(x), len(y))
        return list(zip(x[:n], y[:n]))

    @staticmethod
    def _parse_kv_numbers(html: str) -> dict[str, float]:
        out: dict[str, float] = {}
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        for m in KV_NUMBER_RE.finditer(text):
            key = m.group(1).strip().lower()
            val_raw = m.group(2).replace(",", "")
            try:
                out[key] = float(val_raw)
            except ValueError:
                continue
        return out

    @staticmethod
    def _parse_standing(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "lxml")
        rows = []
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not any("cash" in h or "team" in h for h in headers):
                continue
            for tr in table.find_all("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if not cells:
                    continue
                row = dict(zip(headers, cells))
                rows.append(row)
            if rows:
                break
        return rows

    # -- public API ------------------------------------------------------

    def scrape(self) -> Snapshot:
        if self.session is None:
            self.login()
        snap = Snapshot(timestamp=_now_iso())

        # Current status page (key metrics)
        try:
            cs_html = self._get("current_status")
            snap.raw_pages["current_status"] = cs_html
            kv = self._parse_kv_numbers(cs_html)
            snap.current_day = int(kv.get("day") or kv.get("current day") or 0) or None
            snap.cash = kv.get("cash") or kv.get("cash balance")
            snap.warehouse_inventory = (
                kv.get("warehouse inventory") or kv.get("inventory")
            )
            snap.factory_wip = kv.get("wip") or kv.get("work in process")
            snap.factory_capacity = kv.get("capacity")
            snap.factory_order_point = kv.get("order point") or kv.get("op")
            snap.factory_batch_size = kv.get("batch size") or kv.get("q")
            snap.in_transit = kv.get("in transit")
        except Exception as e:
            log.warning("current_status fetch failed: %s", e)

        # Time series
        for key, attr in [
            ("demand_plot", "demand_series"),
            ("lost_demand_plot", "lost_demand_series"),
            ("warehouse_inventory_plot", "inventory_series"),
            ("cash_plot", "cash_series"),
            ("shipments_plot", "shipments_series"),
            ("factory_wip_plot", "wip_series"),
        ]:
            try:
                html = self._get(key)
                snap.raw_pages[key] = html
                setattr(snap, attr, self._series_from_arrays(html))
            except Exception as e:
                log.warning("%s fetch failed: %s", key, e)

        # Standing
        try:
            st_html = self._get("standing")
            snap.raw_pages["standing"] = st_html
            snap.standing = self._parse_standing(st_html)
        except Exception as e:
            log.warning("standing fetch failed: %s", e)

        # Update current_day from demand series if dashboard didn't have it
        if snap.current_day is None and snap.demand_series:
            snap.current_day = int(max(d for d, _ in snap.demand_series))

        return snap


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---- Persistence helpers -------------------------------------------------

def save_snapshot_json(snap: Snapshot, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(snap.to_dict(), f, indent=2, default=str)


def save_snapshot_csvs(snap: Snapshot, out_dir: str | Path, ts_label: str) -> None:
    import csv
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    series_attrs = [
        "demand_series", "lost_demand_series", "inventory_series",
        "cash_series", "shipments_series", "wip_series",
    ]
    for attr in series_attrs:
        rows = getattr(snap, attr, [])
        if not rows:
            continue
        with open(out_dir / f"{attr}_{ts_label}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["day", "value"])
            w.writerows(rows)


def latest_value(series: list[tuple[float, float]]) -> float | None:
    if not series:
        return None
    return series[-1][1]
