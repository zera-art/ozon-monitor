"""Расчёт статусов и рекомендаций по ценам для Ozon."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config

SHEET_HEADERS = [
    "Артикул", "Баркод", "Название", "Категория", "Остаток",
    "Заказы 7д", "Заказы 28д", "Ср. заказов/нед",
    "Оборачиваемость", "△ к прошлой неделе", "Цена",
    "Скидка %", "Прогноз распродажи", "Статус", "Новая цена", "Рекомендация",
]


@dataclass
class SKUMetrics:
    offer_id: str
    name:     str = ""
    category: str = ""
    barcode:  str = ""

    stock:    int   = 0
    reserved: int   = 0

    orders_28d:      int   = 0
    orders_prev_28d: int   = 0
    orders_7d:       float = 0.0
    avg_weekly:      float = 0.0

    price:     float = 0.0
    old_price: float = 0.0
    min_price: float = 0.0

    # Вычисляемые
    turnover_days:    float = 999.0
    demand_delta_pct: float = 0.0
    discount_pct:     float = 0.0
    forecast_days:    float = 999.0

    status:         str            = "🟡 МОНИТОРИНГ"
    priority:       int            = 3
    new_price:      Optional[float] = None
    recommendation: str            = ""

    def to_row(self) -> list:
        td  = round(self.turnover_days,  1) if self.turnover_days  < 999 else "—"
        fcd = round(self.forecast_days,  0) if self.forecast_days  < 999 else "—"
        dlt = f"{self.demand_delta_pct:+.0f}%" if self.demand_delta_pct else "—"
        dsc = f"{self.discount_pct:.0f}%"      if self.discount_pct    else "—"
        return [
            self.offer_id,
            self.barcode,
            self.name[:50],
            self.category,
            self.stock,
            round(self.orders_7d, 1),
            self.orders_28d,
            round(self.avg_weekly, 1),
            td,
            dlt,
            self.price,
            dsc,
            fcd,
            self.status,
            self.new_price or "",
            self.recommendation,
        ]


# ── Классификация ─────────────────────────────────────────────────────────────

def classify_sku(
    stock: int,
    orders_28d: int,
    orders_prev_28d: int,
    current_price: float,
    old_price: float,
    prev_price: float = 0.0,
    category: str = "",
) -> tuple[str, int]:
    """Возвращает (статус, приоритет)."""
    orders_7d        = orders_28d / 4
    turnover_days    = _turnover(stock, orders_28d)
    demand_delta_pct = _delta(orders_28d, orders_prev_28d)
    discount_pct     = _discount(current_price, old_price)

    if stock > 0 and orders_28d == 0:
        return "🔴 МЁРТВЫЙ ОСТАТОК", 1

    if stock < orders_7d:
        return "🔴 КРИТИЧНЫЙ ОСТАТОК", 1

    if discount_pct > 20 and demand_delta_pct < 10:
        return "🔴 РАСПРОДАЖА НЕЭФФЕКТИВНА", 1

    if turnover_days > 60:
        return "🟠 ЗАМЕДЛЕННАЯ ОБОРАЧИВАЕМОСТЬ", 2

    if prev_price > 0 and current_price > prev_price * 1.05:
        return "🔵 ЦЕНА ПОДНЯТА", 3

    if turnover_days < 21 and stock > 0:
        return "🟢 ПОДНЯТЬ ЦЕНУ", 3

    if demand_delta_pct > 20:
        return "🟢 НОРМАЛИЗАЦИЯ", 3

    return "🟡 МОНИТОРИНГ", 3


def compute_metrics(
    stocks:          dict[str, dict],
    products:        dict[str, dict],
    prices:          dict[str, dict],
    orders_28d:      dict[str, int],
    orders_prev_28d: dict[str, int],
    history:         dict[str, dict] | None = None,
) -> dict[str, SKUMetrics]:
    """Строит словарь offer_id → SKUMetrics."""
    all_ids = set(stocks) | set(products) | set(prices) | set(orders_28d)
    result: dict[str, SKUMetrics] = {}

    for oid in all_ids:
        prod  = products.get(oid, {})
        stk   = stocks.get(oid, {})
        prc   = prices.get(oid, {})
        ord28 = orders_28d.get(oid, 0)
        prev  = orders_prev_28d.get(oid, 0)

        stock     = stk.get("stock", 0)
        reserved  = stk.get("reserved", 0)
        price     = prc.get("price", 0.0)
        old_price = prc.get("old_price", 0.0)
        min_price = prc.get("min_price", 0.0)

        prev_price = (history or {}).get(oid, {}).get("price", 0.0) if history else 0.0
        category   = prod.get("category", "")

        status, priority = classify_sku(
            stock, ord28, prev, price, old_price, prev_price, category
        )

        orders_7d   = ord28 / 4
        avg_weekly  = ord28 / 4
        turnover    = _turnover(stock, ord28)
        delta       = _delta(ord28, prev)
        discount    = _discount(price, old_price)
        forecast    = turnover  # дней до нуля при текущем темпе

        new_price: Optional[float] = None
        recommendation = ""

        if "ПОДНЯТЬ ЦЕНУ" in status:
            new_price      = calc_price_raise(turnover, delta, price)
            recommendation = f"Поднять цену до {new_price} руб." if new_price else ""
        elif "МЁРТВЫЙ" in status or "ЗАМЕДЛЕННАЯ" in status:
            new_price      = calc_price_decrease(turnover, price, category)
            recommendation = f"Снизить цену до {new_price} руб." if new_price else ""
        elif "КРИТИЧНЫЙ" in status:
            recommendation = "Срочно пополнить остаток"
        elif "РАСПРОДАЖА НЕЭФФЕКТИВНА" in status:
            recommendation = "Проверить карточку, SEO, рекламу"

        result[oid] = SKUMetrics(
            offer_id=oid,
            name=prod.get("name", ""),
            category=category,
            barcode=prod.get("barcode", ""),
            stock=stock,
            reserved=reserved,
            orders_28d=ord28,
            orders_prev_28d=prev,
            orders_7d=orders_7d,
            avg_weekly=avg_weekly,
            price=price,
            old_price=old_price,
            min_price=min_price,
            turnover_days=turnover,
            demand_delta_pct=delta,
            discount_pct=discount,
            forecast_days=forecast,
            status=status,
            priority=priority,
            new_price=new_price,
            recommendation=recommendation,
        )
    return result


def build_summary(metrics: dict[str, SKUMetrics]) -> dict:
    urgent  = sum(1 for m in metrics.values() if m.priority == 1)
    warning = sum(1 for m in metrics.values() if m.priority == 2)
    ok      = sum(1 for m in metrics.values() if m.priority == 3)
    status_breakdown: dict[str, int] = {}
    for m in metrics.values():
        status_breakdown[m.status] = status_breakdown.get(m.status, 0) + 1
    return {
        "total_skus":       len(metrics),
        "urgent_count":     urgent,
        "warning_count":    warning,
        "ok_count":         ok,
        "status_breakdown": status_breakdown,
        "updated_at":       datetime.now().strftime("%d.%m.%Y %H:%M"),
    }


# ── Рекомендации по ценам ─────────────────────────────────────────────────────

def calc_price_raise(turnover_days: float, demand_delta_pct: float,
                     current_price: float) -> Optional[float]:
    rising = demand_delta_pct > 0
    if turnover_days < 7:
        pct = 28
    elif turnover_days < 14:
        pct = 20 if rising else 13
    elif turnover_days < 21:
        pct = 11 if rising else 8
    elif turnover_days < 30:
        pct = 7
    else:
        return None
    raw = current_price * (1 + pct / 100)
    return _round10(raw)


def calc_price_decrease(turnover_days: float, current_price: float,
                        category: str, has_no_sales_14d: bool = False) -> Optional[float]:
    if has_no_sales_14d or turnover_days > 180:
        pct = 30
    elif turnover_days > 90:
        pct = 20
    elif turnover_days > 60:
        pct = 10
    else:
        return None
    raw      = current_price * (1 - pct / 100)
    fp       = config.floor_price(category)
    new_price = max(raw, fp)
    return _round10(new_price)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _turnover(stock: int, orders_28d: int) -> float:
    if orders_28d <= 0:
        return 999.0 if stock > 0 else 0.0
    return round(stock / (orders_28d / 28), 1)


def _delta(cur: int, prev: int) -> float:
    if prev <= 0:
        return 0.0
    return round((cur - prev) / prev * 100, 1)


def _discount(price: float, old_price: float) -> float:
    if old_price > price > 0:
        return round((old_price - price) / old_price * 100, 1)
    return 0.0


def _round10(value: float) -> float:
    return round(value / 10) * 10
