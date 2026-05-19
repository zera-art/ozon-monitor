"""Отправка изменений цен на Ozon."""

import logging
from datetime import datetime

import requests

import config

logger = logging.getLogger(__name__)

PRICES_URL = "https://api-seller.ozon.ru/v1/product/import/prices"


def send_prices_to_ozon(approved_items: list[dict], sheets_writer=None) -> dict:
    """
    Отправляет изменения цен пакетами по 1000.
    approved_items: список словарей из get_approved_changes() sheets_writer'а.
    Возвращает {"sent": n, "errors": n}.
    """
    if not approved_items:
        return {"sent": 0, "errors": 0}

    headers = {
        "Client-Id":    config.OZON_CLIENT_ID,
        "Api-Key":      config.OZON_API_KEY,
        "Content-Type": "application/json",
    }

    sent = errors = 0
    batch_size = 1000

    for i in range(0, len(approved_items), batch_size):
        batch = approved_items[i:i + batch_size]
        prices_payload = []
        for item in batch:
            offer_id  = str(item.get("offer_id", "")).strip()
            new_price = str(item.get("new_price", "")).strip().replace(" руб (min)", "").replace(",", ".")
            old_price = str(item.get("old_price", "")).strip().replace(",", ".")
            category  = item.get("category", "")
            min_price = str(config.floor_price(category))
            if not offer_id or not new_price:
                continue
            prices_payload.append({
                "offer_id":  offer_id,
                "price":     new_price,
                "old_price": old_price,
                "min_price": min_price,
            })

        if not prices_payload:
            continue

        try:
            resp = requests.post(
                PRICES_URL,
                json={"prices": prices_payload},
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            sent_now = len(prices_payload)

            # Проверяем ошибки в ответе
            failed_ids = set()
            for res_item in result.get("result", []):
                if not res_item.get("updated", True):
                    failed_ids.add(res_item.get("offer_id", ""))
                    errors += 1
                    logger.warning(f"Ошибка цены для {res_item.get('offer_id')}: "
                                   f"{res_item.get('errors')}")

            sent += sent_now - len(failed_ids)
            sent_at = datetime.now().strftime("%d.%m.%Y %H:%M")

            if sheets_writer:
                for item in batch:
                    oid   = str(item.get("offer_id", ""))
                    ridx  = item.get("row_index")
                    if ridx is None:
                        continue
                    if oid in failed_ids:
                        sheets_writer.mark_sent(ridx, f"ОШИБКА: {oid}")
                    else:
                        sheets_writer.mark_sent(ridx, sent_at)
                        if sheets_writer:
                            try:
                                old_p = float(str(item.get("old_price", "0")).replace(",", "."))
                                new_p = float(str(item.get("new_price", "0")).replace(",", "."))
                                action = "Поднять цену" if new_p > old_p else "Снизить цену"
                                sheets_writer.record_price_change(
                                    oid, item.get("name", ""), action,
                                    old_p, new_p,
                                    orders_week=0, turnover_days=0,
                                )
                            except Exception:
                                pass

        except Exception as e:
            logger.error(f"Ошибка отправки цен batch {i}: {e}")
            errors += len(batch)
            if sheets_writer:
                for item in batch:
                    ridx = item.get("row_index")
                    if ridx:
                        sheets_writer.mark_sent(ridx, f"ОШИБКА: {e}")

    logger.info(f"Цены отправлены: {sent} успешно, {errors} ошибок")
    return {"sent": sent, "errors": errors}
