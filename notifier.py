"""Telegram уведомления для Ozon Monitor."""

import logging
from datetime import datetime

import requests

import config

logger = logging.getLogger(__name__)


def _send(text: str) -> bool:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.info(f"[TG disabled] {text[:80]}")
        return False
    url  = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        "chat_id":    config.TELEGRAM_CHAT_ID,
        "text":       text,
        "parse_mode": "HTML",
    }, timeout=15)
    if not resp.ok:
        logger.warning(f"Telegram ошибка: {resp.text[:200]}")
    return resp.ok


def send_daily_report(summary: dict, revenue_7d: float = 0.0):
    urgent  = summary.get("urgent_count",  0)
    warning = summary.get("warning_count", 0)
    ok      = summary.get("ok_count",      0)
    today   = datetime.now().strftime("%d.%m.%Y")
    rev_str = f"{int(revenue_7d):,}".replace(",", " ")
    _send(
        f"📊 <b>Ozon монитор — {today}</b>\n"
        f"🔴 Срочно: {urgent} | ⚠️ Внимание: {warning} | ✅ Норма: {ok}\n"
        f"Выручка 7д: {rev_str} руб"
    )


def send_queue_ready(total: int, n_up: int, n_down: int):
    _send(
        f"📋 <b>Ozon: список изменений цен готов: {total} SKU</b>\n"
        f"⬆️ Повышение: {n_up} | ⬇️ Снижение: {n_down}\n"
        f"Согласуйте → лист ОЧЕРЕДЬ ИЗМЕНЕНИЙ"
    )


def send_queue_reminder(n_unapproved: int):
    _send(f"⏰ <b>Ozon: ожидает согласования: {n_unapproved} SKU</b>")


def send_prices_sent(n_sent: int, n_up: int, n_down: int):
    _send(
        f"✅ <b>Ozon: цены обновлены: {n_sent} SKU</b>\n"
        f"⬆️ Повышено: {n_up} | ⬇️ Снижено: {n_down}"
    )


def send_error(message: str):
    _send(f"❌ <b>Ozon Monitor ошибка:</b>\n{message}")
