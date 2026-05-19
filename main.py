"""Ozon Monitor — ежедневный запуск."""

import logging
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.stdout.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("ozon_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

import config
from ozon_client    import OzonClient
from analytics      import compute_metrics, build_summary
from sheets_writer  import OzonSheetsWriter
from price_sender   import send_prices_to_ozon
import notifier


def main():
    now     = datetime.now()
    weekday = now.weekday()  # 0=пн, 1=вт, 2=ср

    logger.info("=" * 60)
    logger.info(f"🚀 Запуск Ozon Monitor — {now.strftime('%d.%m.%Y %H:%M')}")
    logger.info("=" * 60)

    # ── 1. Ozon API ───────────────────────────────────────────────────────────
    if not config.OZON_CLIENT_ID or not config.OZON_API_KEY:
        logger.error("OZON_CLIENT_ID и OZON_API_KEY не заданы. Добавь их в .env")
        sys.exit(1)

    ozon = OzonClient()

    logger.info("📋 Загружаем товары...")
    products = ozon.get_products()
    logger.info(f"  → {len(products)} товаров")

    logger.info("📦 Загружаем остатки...")
    stocks = ozon.get_stocks()
    logger.info(f"  → {len(stocks)} SKU с остатками")

    logger.info("💰 Загружаем цены...")
    prices = ozon.get_prices()
    logger.info(f"  → {len(prices)} SKU с ценами")

    logger.info("📊 Загружаем заказы (текущие 28д)...")
    orders_28d = ozon.get_orders_28d()
    logger.info(f"  → {sum(orders_28d.values())} заказов по {len(orders_28d)} SKU")

    logger.info("📊 Загружаем заказы (предыдущие 28д)...")
    orders_prev = ozon.get_orders_prev_28d()

    # ── 2. Метрики ────────────────────────────────────────────────────────────
    logger.info("🧮 Считаем метрики...")

    if not config.GOOGLE_SHEETS_ID:
        logger.error("OZON_SHEETS_ID не задан в .env")
        sys.exit(1)

    sheets = OzonSheetsWriter(config.GOOGLE_CREDENTIALS_PATH, config.GOOGLE_SHEETS_ID)

    # Читаем историю для статуса ЦЕНА ПОДНЯТА
    history = sheets._read_history_snapshot()

    metrics = compute_metrics(stocks, products, prices, orders_28d, orders_prev, history)
    summary = build_summary(metrics)
    logger.info(f"  → {summary['total_skus']} SKU | Срочных: {summary['urgent_count']}")

    # ── 3. Google Sheets ──────────────────────────────────────────────────────
    logger.info("📝 Обновляем Google Sheets...")
    sorted_metrics = sheets.update_all(metrics)
    sheets.setup_settings_sheet()
    logger.info("  ✅ Sheets обновлён")

    # ── 4. Одобренные изменения цен ──────────────────────────────────────────
    logger.info("💸 Проверяем одобренные изменения цен...")
    approved = sheets.get_approved_changes()
    if approved:
        logger.info(f"  → {len(approved)} изменений к отправке")
        result = send_prices_to_ozon(approved, sheets_writer=sheets)
        if result["sent"] > 0:
            n_up   = sum(1 for a in approved if _is_raise(a))
            n_down = result["sent"] - n_up
            notifier.send_prices_sent(result["sent"], n_up, n_down)
            logger.info(f"  ✅ Отправлено: {result['sent']} цен")
    else:
        logger.info("  Нет одобренных изменений")

    # ── 5. Контрольные точки эффективности ───────────────────────────────────
    logger.info("📈 Обновляем контрольные точки эффективности...")
    sheets.update_effectiveness_checkpoints(metrics)

    # ── 6. Понедельник — очередь изменений ───────────────────────────────────
    if weekday == 0:
        logger.info("📋 Понедельник — формируем очередь изменений...")
        queue_result = sheets.update_price_queue(sorted_metrics)
        if not queue_result.get("skipped"):
            notifier.send_queue_ready(
                queue_result.get("total", 0),
                queue_result.get("n_up",  0),
                queue_result.get("n_down",0),
            )

    # ── 7. Вт/ср — напоминание если не согласовано ───────────────────────────
    if weekday in (1, 2):
        n_unapproved = sheets.queue_unapproved_count()
        if n_unapproved > 0:
            logger.info(f"  Напоминание: {n_unapproved} SKU ожидают согласования")
            notifier.send_queue_reminder(n_unapproved)

    # ── 8. Ежедневный отчёт ───────────────────────────────────────────────────
    notifier.send_daily_report(summary)

    logger.info("=" * 60)
    logger.info(f"✅ Готово за {datetime.now().strftime('%H:%M:%S')}")
    logger.info("=" * 60)


def _is_raise(item: dict) -> bool:
    try:
        old = float(str(item.get("old_price", "0")).replace(",", "."))
        new = float(str(item.get("new_price", "0")).replace(",", ".").replace(" руб (min)", ""))
        return new > old
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    main()
