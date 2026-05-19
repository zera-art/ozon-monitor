"""Ozon Seller API client."""

import logging
import time
from datetime import datetime, timedelta

import requests

import config

logger = logging.getLogger(__name__)

BASE_URL    = "https://api-seller.ozon.ru"
RETRY_MAX   = 5
RETRY_DELAY = 10


class OzonAPIError(Exception):
    pass


class OzonClient:
    def __init__(self, client_id: str = None, api_key: str = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id":    client_id or config.OZON_CLIENT_ID,
            "Api-Key":      api_key   or config.OZON_API_KEY,
            "Content-Type": "application/json",
        })
        # sku_id (str) → offer_id; заполняется в get_stocks и get_products
        self._sku_offer_map: dict[str, str] = {}

    def _post(self, path: str, body: dict, attempt: int = 0) -> dict:
        url = BASE_URL + path
        try:
            resp = self.session.post(url, json=body, timeout=60)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"Rate limit {path}, ждём {wait}с")
                time.sleep(wait)
                return self._post(path, body, attempt)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            if attempt < RETRY_MAX:
                logger.warning(f"Timeout {path}, попытка {attempt + 2}/{RETRY_MAX + 1}")
                time.sleep(RETRY_DELAY * (attempt + 1))
                return self._post(path, body, attempt + 1)
            raise
        except requests.exceptions.HTTPError as e:
            logger.error(f"HTTP ошибка {path}: {e} | тело: {e.response.text[:300]}")
            raise OzonAPIError(str(e)) from e

    # ── Остатки ───────────────────────────────────────────────────────────────

    def get_stocks(self) -> dict[str, dict]:
        """offer_id → {stock, reserved}. Также заполняет _sku_offer_map."""
        result: dict[str, dict] = {}
        cursor = ""
        limit  = 1000
        while True:
            body = {"filter": {"visibility": "ALL"}, "limit": limit}
            if cursor:
                body["cursor"] = cursor
            data   = self._post("/v4/product/info/stocks", body)
            items  = data.get("items", [])
            cursor = data.get("cursor", "")
            for item in items:
                oid = item.get("offer_id", "")
                if not oid:
                    continue
                total    = sum(s.get("present",  0) for s in item.get("stocks", []))
                reserved = sum(s.get("reserved", 0) for s in item.get("stocks", []))
                result[oid] = {"stock": total, "reserved": reserved}
                # Строим маппинг sku_id → offer_id (FBO и FBS)
                for s in item.get("stocks", []):
                    sku_id = str(s.get("sku", "") or "")
                    if sku_id and sku_id != "0":
                        self._sku_offer_map[sku_id] = oid
            if len(items) < limit or not cursor:
                break
        logger.info(f"Остатки: {len(result)} SKU, sku_map: {len(self._sku_offer_map)} записей")
        return result

    # ── Товары ────────────────────────────────────────────────────────────────

    def get_products(self) -> dict[str, dict]:
        """offer_id → {name, category, barcode, product_id}. Также заполняет _sku_offer_map."""
        # Шаг 1 — список всех offer_id
        offer_ids: list[str] = []
        product_ids: dict[str, int] = {}  # offer_id → product_id
        last_id = ""
        limit   = 1000
        while True:
            data    = self._post("/v3/product/list", {
                "filter":  {"visibility": "ALL"},
                "last_id": last_id,
                "limit":   limit,
            })
            items   = data.get("result", {}).get("items", [])
            last_id = data.get("result", {}).get("last_id", "")
            for item in items:
                oid = item.get("offer_id", "")
                if oid:
                    offer_ids.append(oid)
                    product_ids[oid] = item.get("product_id", 0)
            if len(items) < limit or not last_id:
                break

        # Шаг 2 — детали пакетами по 100
        result: dict[str, dict] = {}
        for i in range(0, len(offer_ids), 100):
            batch = offer_ids[i:i + 100]
            try:
                data  = self._post("/v3/product/info/list", {"offer_id": batch})
                items = data.get("items", [])
                for item in items:
                    oid = item.get("offer_id", "")
                    if not oid:
                        continue
                    barcode  = (item.get("barcodes") or [""])[0]
                    category = item.get("type", "") or item.get("description_category_name", "")
                    result[oid] = {
                        "name":       item.get("name", ""),
                        "category":   category,
                        "barcode":    barcode,
                        "product_id": product_ids.get(oid, 0),
                    }
                    # Дополняем маппинг sku_id → offer_id
                    sku_id = str(item.get("sku", "") or "")
                    if sku_id and sku_id != "0":
                        self._sku_offer_map[sku_id] = oid
            except Exception as e:
                logger.warning(f"Ошибка get_products batch {i}: {e}")
            time.sleep(0.3)
        logger.info(f"Товары: {len(result)} SKU, sku_map: {len(self._sku_offer_map)} записей")
        return result

    # ── Цены ──────────────────────────────────────────────────────────────────

    def get_prices(self) -> dict[str, dict]:
        """offer_id → {price, old_price, min_price}."""
        result: dict[str, dict] = {}
        cursor = ""
        limit  = 1000
        while True:
            body = {"filter": {"visibility": "ALL"}, "limit": limit}
            if cursor:
                body["cursor"] = cursor
            data   = self._post("/v5/product/info/prices", body)
            items  = data.get("items", [])
            cursor = data.get("cursor", "")
            for item in items:
                oid = item.get("offer_id", "")
                if not oid:
                    continue
                p = item.get("price", {})
                result[oid] = {
                    "price":     _f(p.get("price",     0)),
                    "old_price": _f(p.get("old_price", 0)),
                    "min_price": _f(p.get("min_price", 0)),
                }
            if len(items) < limit or not cursor:
                break
        logger.info(f"Цены: {len(result)} SKU")
        return result

    # ── Заказы ────────────────────────────────────────────────────────────────

    def get_orders_period(self, days_ago_from: int, days_ago_to: int = 0) -> dict[str, int]:
        """offer_id → кол-во заказов за период [days_ago_from, days_ago_to] дней назад.
        Использует _sku_offer_map для перевода числового sku → offer_id.
        """
        date_from = (datetime.now() - timedelta(days=days_ago_from)).strftime("%Y-%m-%dT00:00:00.000Z")
        date_to   = (datetime.now() - timedelta(days=days_ago_to  )).strftime("%Y-%m-%dT00:00:00.000Z")
        result: dict[str, int] = {}
        offset = 0
        limit  = 1000
        unmapped = 0
        while True:
            data = self._post("/v1/analytics/data", {
                "date_from": date_from,
                "date_to":   date_to,
                "metrics":   ["ordered_units"],
                "dimension": ["sku"],
                "filters":   [],
                "sort":      [{"key": "ordered_units", "order": "DESC"}],
                "offset":    offset,
                "limit":     limit,
            })
            rows = data.get("result", {}).get("data", [])
            for row in rows:
                dims    = row.get("dimensions", [])
                metrics = row.get("metrics", [0])
                sku_id  = dims[0].get("id", "") if dims else ""
                units   = int(metrics[0]) if metrics else 0
                if not sku_id:
                    continue
                # Переводим числовой sku → offer_id продавца
                oid = self._sku_offer_map.get(sku_id, "")
                if not oid:
                    unmapped += 1
                    continue
                result[oid] = result.get(oid, 0) + units
            if len(rows) < limit:
                break
            offset += limit
        if unmapped:
            logger.warning(f"Заказы: {unmapped} SKU не нашли offer_id в sku_map")
        return result

    def get_orders_28d(self) -> dict[str, int]:
        r = self.get_orders_period(28)
        logger.info(f"Заказы 28д: {len(r)} SKU, {sum(r.values())} шт")
        return r

    def get_orders_prev_28d(self) -> dict[str, int]:
        return self.get_orders_period(56, 28)


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0
