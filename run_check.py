"""Hourly entry point: fetch price, log it, refresh chart."""

from __future__ import annotations

from chart import generate_chart
from tracker import check_once


def main() -> int:
    snapshot = check_once()

    if snapshot.error:
        print(f"ERROR: {snapshot.error}")
    else:
        stock = "på lager" if snapshot.in_stock else "udsolgt"
        price = f"{snapshot.price_dkk:.2f} kr" if snapshot.price_dkk is not None else "—"
        print(f"{snapshot.checked_at:%Y-%m-%d %H:%M} | {price} | {stock}")

    chart = generate_chart()
    if chart:
        print(f"Chart saved: {chart}")
    else:
        print("No chart yet (need at least one logged price)")

    return 1 if snapshot.error else 0


if __name__ == "__main__":
    raise SystemExit(main())
