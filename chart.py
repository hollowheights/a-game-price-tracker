"""Generate price history chart from SQLite log."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from tracker import DB_PATH, LOCAL_TZ, load_config

ROOT = Path(__file__).resolve().parent
CHART_PATH = ROOT / "data" / "price_chart.png"
HTML_PATH = ROOT / "data" / "price_chart.html"


def load_rows(product_url: str) -> list[tuple[datetime, float | None, bool | None, str | None]]:
    if not DB_PATH.exists():
        return []

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT checked_at, price_dkk, in_stock, error
            FROM price_checks
            WHERE product_url = ?
            ORDER BY checked_at ASC
            """,
            (product_url,),
        ).fetchall()

    parsed: list[tuple[datetime, float | None, bool | None, str | None]] = []
    for checked_at, price_dkk, in_stock, error in rows:
        parsed.append(
            (
                datetime.fromisoformat(checked_at),
                price_dkk,
                None if in_stock is None else bool(in_stock),
                error,
            )
        )
    return parsed


def generate_chart() -> Path | None:
    config = load_config()
    rows = load_rows(config["product_url"])
    if not rows:
        return None

    times = [row[0] for row in rows]
    prices = [row[1] for row in rows]
    stock = [row[2] for row in rows]

    fig, ax = plt.subplots(figsize=(10, 4.5))
    valid_points = [(t, p) for t, p in zip(times, prices) if p is not None]
    if valid_points:
        valid_times, valid_prices = zip(*valid_points)
        ax.plot(valid_times, valid_prices, marker="o", linewidth=2, color="#e91e63")
        ax.fill_between(valid_times, valid_prices, alpha=0.08, color="#e91e63")

    for t, p, s in zip(times, prices, stock):
        if p is None:
            continue
        if s is False:
            ax.scatter([t], [p], color="#999999", s=40, zorder=5)

    ax.set_title(config["product_name"])
    ax.set_ylabel("Pris (DKK)")
    ax.set_xlabel("Tid")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m %H:%M", tz=LOCAL_TZ))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.25)

    if valid_points:
        latest_price = valid_prices[-1]
        ax.annotate(
            f"{latest_price:.2f} kr",
            xy=(valid_times[-1], latest_price),
            xytext=(8, 8),
            textcoords="offset points",
            fontsize=10,
        )

    CHART_PATH.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(CHART_PATH, dpi=150)
    plt.close(fig)

    latest = rows[-1]
    stock_text = (
        "på lager"
        if latest[2] is True
        else "udsolgt"
        if latest[2] is False
        else "ukendt"
    )
    error_text = f"<p>Seneste fejl: {latest[3]}</p>" if latest[3] else ""
    checked = latest[0].astimezone(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")
    price_text = f"{latest[1]:.2f} kr" if latest[1] is not None else "—"

    HTML_PATH.write_text(
        f"""<!DOCTYPE html>
<html lang="da">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="300">
  <title>{config["product_name"]} — pris</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 960px; }}
    img {{ max-width: 100%; height: auto; border: 1px solid #ddd; border-radius: 8px; }}
    .meta {{ color: #555; margin-bottom: 1rem; }}
    a {{ color: #e91e63; }}
  </style>
</head>
<body>
  <h1>{config["product_name"]}</h1>
  <p class="meta">Senest tjekket: {checked} · Pris: <strong>{price_text}</strong> · {stock_text}</p>
  {error_text}
  <p><a href="{config["product_url"]}">Åbn produkt på Parfumdreams</a></p>
  <img src="price_chart.png" alt="Prisgraf">
</body>
</html>
""",
        encoding="utf-8",
    )

    return CHART_PATH
