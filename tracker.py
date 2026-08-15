"""Fetch and log Parfumdreams product price."""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "data" / "prices.db"
CSV_PATH = ROOT / "data" / "prices.csv"
LOCAL_TZ = ZoneInfo("Europe/Copenhagen")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass
class PriceSnapshot:
    checked_at: datetime
    price_dkk: float | None
    list_price_dkk: float | None
    in_stock: bool | None
    raw_availability: str | None
    error: str | None = None


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                checked_at TEXT NOT NULL,
                product_url TEXT NOT NULL,
                price_dkk REAL,
                list_price_dkk REAL,
                in_stock INTEGER,
                availability TEXT,
                error TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_price_checks_checked_at ON price_checks(checked_at)"
        )


def _parse_dkk(value: str | float | int | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = value.replace(".", "").replace(",", ".").replace("kr", "").strip()
    if not cleaned:
        return None
    return float(cleaned)


def _extract_from_json_ld(html: str, variation_id: str) -> tuple[float | None, float | None, bool | None, str | None]:
    for match in re.finditer(
        r'<script type="application/ld\+json">(\[.*?\])</script>',
        html,
        flags=re.DOTALL,
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        for item in payload:
            if item.get("@type") != "ProductGroup":
                continue
            for variant in item.get("hasVariant", []):
                offers = variant.get("offers", {})
                offer_url = offers.get("url", "")
                if variation_id not in offer_url:
                    continue

                availability = offers.get("availability", "")
                in_stock = "InStock" in availability if availability else None
                price_spec = offers.get("priceSpecification", {})
                price = _parse_dkk(price_spec.get("price"))
                list_price = _parse_dkk(price_spec.get("referencePrice"))
                return price, list_price, in_stock, availability

    return None, None, None, None


def _extract_from_props(html: str, variation_id: str) -> tuple[float | None, float | None, bool | None, str | None]:
    pattern = (
        rf'data-variation-id="{variation_id}"[^>]*data-props="(\{{.*?\}})"'
    )
    match = re.search(pattern, html)
    if not match:
        return None, None, None, None

    props_raw = match.group(1).replace("&quot;", '"')
    try:
        props = json.loads(props_raw)
    except json.JSONDecodeError:
        return None, None, None, None

    variation = next(
        (v for v in props.get("variations", []) if str(v.get("id")) == variation_id),
        None,
    )
    if not variation:
        return None, None, None, None

    price = variation.get("pptc") or variation.get("retailPriceTC")
    list_price = variation.get("retailPriceTC")
    available = variation.get("available")
    availability = "InStock" if available else "OutOfStock"
    return _parse_dkk(price), _parse_dkk(list_price), bool(available), availability


def fetch_price(product_url: str, variation_id: str) -> PriceSnapshot:
    checked_at = datetime.now(LOCAL_TZ)
    try:
        response = requests.get(
            product_url,
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()
        html = response.text

        price, list_price, in_stock, availability = _extract_from_json_ld(html, variation_id)
        if price is None:
            props_price, props_list, props_stock, props_avail = _extract_from_props(
                html, variation_id
            )
            price = price or props_price
            list_price = list_price or props_list
            in_stock = in_stock if in_stock is not None else props_stock
            availability = availability or props_avail

        if price is None:
            item_match = re.search(r'"itemNumber":"(\d+)"', html)
            if item_match:
                retail_match = re.search(
                    rf'id="price_retail_{item_match.group(1)}"[^>]*>([^<]+)',
                    html,
                )
                if retail_match:
                    price = _parse_dkk(retail_match.group(1))

        if price is None and in_stock is None:
            return PriceSnapshot(
                checked_at=checked_at,
                price_dkk=None,
                list_price_dkk=None,
                in_stock=None,
                raw_availability=None,
                error="Could not parse price from page",
            )

        return PriceSnapshot(
            checked_at=checked_at,
            price_dkk=price,
            list_price_dkk=list_price,
            in_stock=in_stock,
            raw_availability=availability,
        )
    except requests.RequestException as exc:
        return PriceSnapshot(
            checked_at=checked_at,
            price_dkk=None,
            list_price_dkk=None,
            in_stock=None,
            raw_availability=None,
            error=str(exc),
        )


def log_snapshot(product_url: str, snapshot: PriceSnapshot) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO price_checks (
                checked_at, product_url, price_dkk, list_price_dkk,
                in_stock, availability, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.checked_at.isoformat(),
                product_url,
                snapshot.price_dkk,
                snapshot.list_price_dkk,
                None if snapshot.in_stock is None else int(snapshot.in_stock),
                snapshot.raw_availability,
                snapshot.error,
            ),
        )


def export_prices_csv() -> Path | None:
    if not DB_PATH.exists():
        return None

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT checked_at, price_dkk, list_price_dkk, in_stock, availability, error
            FROM price_checks
            ORDER BY checked_at ASC
            """
        ).fetchall()

    if not rows:
        return None

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["checked_at", "price_dkk", "list_price_dkk", "in_stock", "availability", "error"]
        )
        for checked_at, price_dkk, list_price_dkk, in_stock, availability, error in rows:
            stock = "" if in_stock is None else ("yes" if in_stock else "no")
            when = datetime.fromisoformat(checked_at).astimezone(LOCAL_TZ).strftime(
                "%d/%m/%Y %H:%M"
            )
            writer.writerow(
                [when, price_dkk, list_price_dkk, stock, availability or "", error or ""]
            )

    return CSV_PATH


def check_once() -> PriceSnapshot:
    config = load_config()
    init_db()
    snapshot = fetch_price(config["product_url"], str(config["variation_id"]))
    log_snapshot(config["product_url"], snapshot)
    export_prices_csv()
    return snapshot
