"""Send price alerts when configured threshold is crossed."""

from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import requests

from tracker import PriceSnapshot, load_config

ROOT = Path(__file__).resolve().parent
ALERT_STATE_PATH = ROOT / "data" / "alert_state.json"


@dataclass
class AlertDecision:
    should_notify: bool
    reason: str


def _load_alert_config() -> dict:
    config = load_config()
    return config.get("alert", {})


def _load_state() -> dict:
    if not ALERT_STATE_PATH.exists():
        return {"armed": True}
    with ALERT_STATE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    ALERT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERT_STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


def evaluate_alert(snapshot: PriceSnapshot) -> AlertDecision:
    alert = _load_alert_config()
    if not alert.get("enabled"):
        return AlertDecision(False, "alerts disabled")

    if snapshot.error:
        return AlertDecision(False, "fetch error")

    threshold = alert.get("price_below_dkk")
    if threshold is None:
        return AlertDecision(False, "no threshold configured")

    if snapshot.price_dkk is None:
        return AlertDecision(False, "no price")

    if alert.get("only_in_stock", True) and snapshot.in_stock is not True:
        return AlertDecision(False, "out of stock")

    state = _load_state()
    if snapshot.price_dkk < threshold:
        if alert.get("notify_on_each_check_while_below"):
            return AlertDecision(True, f"price {snapshot.price_dkk:.2f} below {threshold}")
        if state.get("armed", True):
            return AlertDecision(True, f"price crossed below {threshold}")
        return AlertDecision(False, "already notified for this dip")

    return AlertDecision(False, "price above threshold")


def update_state_after_check(snapshot: PriceSnapshot) -> None:
    alert = _load_alert_config()
    if not alert.get("enabled"):
        return

    threshold = alert.get("price_below_dkk")
    if threshold is None or snapshot.price_dkk is None:
        return

    state = _load_state()
    if snapshot.price_dkk < threshold:
        if not alert.get("notify_on_each_check_while_below"):
            state["armed"] = False
    else:
        state["armed"] = True

    state["last_price_dkk"] = snapshot.price_dkk
    state["last_checked_at"] = snapshot.checked_at.isoformat()
    save_state(state)


def _build_message(snapshot: PriceSnapshot, reason: str) -> tuple[str, str]:
    config = load_config()
    alert = _load_alert_config()
    threshold = alert.get("price_below_dkk")
    price = f"{snapshot.price_dkk:.2f} kr" if snapshot.price_dkk is not None else "ukendt"
    stock = "på lager" if snapshot.in_stock else "udsolgt"
    title = f"Prisalert: {config['product_name']}"
    body = (
        f"{reason}\n\n"
        f"Pris nu: {price}\n"
        f"Grænse: under {threshold} kr\n"
        f"Lager: {stock}\n"
        f"Tid: {snapshot.checked_at:%d/%m/%Y %H:%M}\n\n"
        f"{config['product_url']}\n"
        f"Graf: https://hollowheights.github.io/a-game-price-tracker/price_chart.html"
    )
    return title, body


def send_ntfy(title: str, body: str) -> bool:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return False

    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": title, "Priority": "high", "Tags": "moneybag"}
    token = os.environ.get("NTFY_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.post(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=20,
    )
    response.raise_for_status()
    return True


def send_email(title: str, body: str) -> bool:
    recipient = os.environ.get("NOTIFY_EMAIL", "").strip()
    smtp_user = os.environ.get("SMTP_USER", "").strip()
    smtp_password = os.environ.get("SMTP_PASSWORD", "").strip()
    if not recipient or not smtp_user or not smtp_password:
        return False

    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))

    message = EmailMessage()
    message["Subject"] = title
    message["From"] = smtp_user
    message["To"] = recipient
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(message)

    return True


def maybe_notify(snapshot: PriceSnapshot) -> list[str]:
    decision = evaluate_alert(snapshot)
    update_state_after_check(snapshot)

    if not decision.should_notify:
        return [f"no alert: {decision.reason}"]

    title, body = _build_message(snapshot, decision.reason)
    sent: list[str] = []

    try:
        if send_ntfy(title, body):
            sent.append("ntfy")
    except requests.RequestException as exc:
        sent.append(f"ntfy failed: {exc}")

    try:
        if send_email(title, body):
            sent.append("email")
    except OSError as exc:
        sent.append(f"email failed: {exc}")

    if not sent or all(s.endswith("failed") or "failed:" in s for s in sent):
        if not os.environ.get("NTFY_TOPIC") and not os.environ.get("NOTIFY_EMAIL"):
            sent.append("alert triggered but no notification channel configured")
        elif not any(s in ("ntfy", "email") for s in sent):
            sent.append("alert triggered but delivery failed")

    return sent
