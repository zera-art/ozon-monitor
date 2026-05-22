"""Google Sheets writer для Ozon Monitor."""

import logging
import time
from datetime import datetime

import gspread
from gspread.exceptions import APIError
from google.oauth2.service_account import Credentials

from analytics import SKUMetrics, SHEET_HEADERS, build_summary, calc_price_raise, calc_price_decrease
import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

NUM_COLS = len(SHEET_HEADERS)

# ── Color helpers ─────────────────────────────────────────────────────────────

def _hex(color: str) -> dict:
    h = color.lstrip("#")
    return {"red": int(h[0:2], 16)/255, "green": int(h[2:4], 16)/255, "blue": int(h[4:6], 16)/255}

WHITE     = {"red": 1.0, "green": 1.0, "blue": 1.0}
HEADER_BG = "#1a2f4a"

GROUP_COLORS = {1: "#d32f2f", 2: "#e65100", 3: "#1565c0"}

STATUS_ROW_COLORS = {
    "КРИТИЧНЫЙ":      "#FEE2E2",
    "МЕДЛЕННЫЕ":      "#FFEDD5",
    "НЕТ СПРОСА":     "#F3F4F6",
    "НЕТ В НАЛИЧИИ":  "#E5E7EB",
    "ВЫВЕДЕН":        "#9CA3AF",
    "НУЖНА ПОСТАВКА": "#DBEAFE",
    "ПЕРЕСМОТРЕТЬ":   "#FEF9C3",
    "ПОДНЯТЬ ЦЕНУ":   "#DCFCE7",
    "СПРОС РАСТЁТ":   "#F0FDF4",
}

TURNOVER_COL = SHEET_HEADERS.index("Оборачиваемость")


def _status_color(status: str) -> str | None:
    upper = status.upper()
    for key, color in STATUS_ROW_COLORS.items():
        if key in upper:
            return color
    return None


# ── OzonSheetsWriter ──────────────────────────────────────────────────────────

class OzonSheetsWriter:
    SHEET_NAMES = [
        "🎯 РЕШЕНИЯ",
        "📊 СВОДКА",
        "📦 ВСЕ SKU",
        "📦 АРХИВ",
        "📅 ИСТОРИЯ",
        "⚙️ НАСТРОЙКИ",
        "📋 ПРАВИЛА",
        "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ",
        "📊 ЭФФЕКТИВНОСТЬ",
    ]

    _Q_OFFER   = 0
    _Q_NAME    = 1
    _Q_CAT     = 2
    _Q_CUR     = 3
    _Q_NEW     = 4
    _Q_PCT     = 5
    _Q_REASON  = 6
    _Q_DATE    = 7
    _Q_APPROVE = 8
    _Q_SENT    = 9

    def __init__(self, credentials_path: str, spreadsheet_id: str):
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
        self.gc          = gspread.authorize(creds)
        self.spreadsheet = self.gc.open_by_key(spreadsheet_id)
        self._ensure_sheets()

    def _ensure_sheets(self):
        existing = {ws.title for ws in self.spreadsheet.worksheets()}
        for name in self.SHEET_NAMES:
            if name not in existing:
                self.spreadsheet.add_worksheet(title=name, rows=2000, cols=20)
                logger.info(f"Создан лист: {name}")
                time.sleep(0.5)

    def _get_sheet(self, name: str) -> gspread.Worksheet:
        return self.spreadsheet.worksheet(name)

    # ── Low-level helpers ─────────────────────────────────────────────────────

    def _cell_req(self, sid, r1, r2, c1, c2, fmt) -> dict:
        return {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": r1, "endRowIndex": r2,
                      "startColumnIndex": c1, "endColumnIndex": c2},
            "cell": {"userEnteredFormat": fmt},
            "fields": "userEnteredFormat",
        }}

    def _header_req(self, sid, row, ncols, bg=HEADER_BG) -> dict:
        return self._cell_req(sid, row, row+1, 0, ncols, {
            "backgroundColor": _hex(bg),
            "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 10},
            "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE",
        })

    def _freeze_req(self, sid, rows) -> dict:
        return {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": rows}},
            "fields": "gridProperties.frozenRowCount",
        }}

    def _resize_req(self, sid, end_col=20) -> dict:
        return {"autoResizeDimensions": {"dimensions": {
            "sheetId": sid, "dimension": "COLUMNS", "startIndex": 0, "endIndex": end_col,
        }}}

    def _delete_cf_rules(self, sid) -> list:
        try:
            meta = self.spreadsheet.fetch_sheet_metadata()
            for s in meta.get("sheets", []):
                if s["properties"]["sheetId"] == sid:
                    n = len(s.get("conditionalFormats", []))
                    return [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
                            for _ in range(n)]
        except Exception:
            pass
        return []

    def _delete_banding(self, sid) -> list:
        try:
            meta = self.spreadsheet.fetch_sheet_metadata()
            for s in meta.get("sheets", []):
                if s["properties"]["sheetId"] == sid:
                    return [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}}
                            for b in s.get("bandedRanges", [])]
        except Exception:
            pass
        return []

    def _batch(self, requests: list):
        if requests:
            try:
                self.spreadsheet.batch_update({"requests": requests})
            except Exception as e:
                logger.warning(f"Ошибка форматирования: {e}")

    def _write(self, ws, rows, start="A1"):
        if not rows:
            return
        try:
            ws.update(start, rows, value_input_option="USER_ENTERED")
        except APIError as e:
            logger.error(f"Ошибка записи: {e}")
            time.sleep(5)
            ws.update(start, rows, value_input_option="USER_ENTERED")

    # ── Public API ────────────────────────────────────────────────────────────

    def update_all(self, metrics: dict[str, SKUMetrics]) -> list[SKUMetrics]:
        history = self._read_history_snapshot()
        if history:
            _apply_price_raised(metrics, history)

        # Разбиваем на активные и архивные (ВЫВЕДЕН ИЗ ПРОДАЖИ)
        active_metrics   = {oid: m for oid, m in metrics.items() if "ВЫВЕДЕН" not in m.status}
        archived_metrics = {oid: m for oid, m in metrics.items() if "ВЫВЕДЕН" in m.status}

        summary        = build_summary(active_metrics)
        sorted_metrics = sorted(active_metrics.values(), key=lambda m: (m.priority, -m.orders_28d))

        self._update_decisions(sorted_metrics)
        self._update_summary(summary, sorted_metrics)
        self._update_all_skus(sorted_metrics)
        self._update_archive(archived_metrics)
        self._setup_rules_sheet()
        self._save_history_snapshot(sorted_metrics)
        logger.info("✅ Дашборд обновлён")
        return sorted_metrics

    # ── РЕШЕНИЯ ───────────────────────────────────────────────────────────────

    def _update_decisions(self, metrics: list[SKUMetrics]):
        ws = self._get_sheet("🎯 РЕШЕНИЯ")
        ws.clear()
        sid = ws.id

        p1 = [m for m in metrics if m.priority == 1]
        p2 = [m for m in metrics if m.priority == 2]
        p3 = [m for m in metrics if m.priority == 3]

        title = f"🎯 РЕШЕНИЯ И ПРИОРИТЕТЫ OZON — обновлено {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        rows  = [[title], [""]]
        reqs: list[dict] = []
        ri = 2

        def add_group(priority, label, group):
            nonlocal ri
            if not group:
                return
            rows.append([label] + [""] * (NUM_COLS - 1))
            reqs.append(self._cell_req(sid, ri, ri+1, 0, NUM_COLS, {
                "backgroundColor": _hex(GROUP_COLORS[priority]),
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            }))
            ri += 1
            rows.append(SHEET_HEADERS)
            reqs.append(self._header_req(sid, ri, NUM_COLS))
            ri += 1
            for m in group:
                rows.append(m.to_row())
                color = _status_color(m.status)
                if color:
                    reqs.append(self._cell_req(sid, ri, ri+1, 0, NUM_COLS,
                                               {"backgroundColor": _hex(color)}))
                ri += 1
            rows.append([""])
            ri += 1

        add_group(1, f"🚨 СРОЧНЫЕ ДЕЙСТВИЯ ({len(p1)} SKU)", p1)
        add_group(2, f"⚠️ ТРЕБУЮТ ВНИМАНИЯ ({len(p2)} SKU)", p2)
        add_group(3, f"🔍 МОНИТОРИНГ ({len(p3)} SKU)", p3)

        self._write(ws, rows)
        time.sleep(1)
        self._batch(
            self._delete_cf_rules(sid) + reqs +
            [self._freeze_req(sid, 1), self._resize_req(sid)]
        )
        logger.info(f"Лист РЕШЕНИЯ: P1={len(p1)}, P2={len(p2)}, P3={len(p3)}")

    # ── СВОДКА ────────────────────────────────────────────────────────────────

    def _update_summary(self, summary: dict, metrics: list[SKUMetrics]):
        ws = self._get_sheet("📊 СВОДКА")
        ws.clear()
        sid = ws.id
        rows = [
            [f"📊 СВОДНАЯ СТАТИСТИКА OZON — {summary['updated_at']}"], [""],
            ["Метрика", "Значение", "Комментарий"],
            ["Всего SKU",                  summary["total_skus"],    "Активных позиций"],
            ["🚨 Срочных",                 summary["urgent_count"],  "Приоритет 1"],
            ["⚠️ Требуют внимания",        summary["warning_count"], "Приоритет 2"],
            ["✅ Мониторинг",              summary["ok_count"],      "Приоритет 3"],
            [""], ["РАЗБИВКА ПО СТАТУСАМ", "", ""],
        ]
        for status, cnt in sorted(summary["status_breakdown"].items(), key=lambda x: -x[1]):
            rows.append([status, cnt, "SKU"])
        self._write(ws, rows)
        time.sleep(1)
        self._batch([
            self._cell_req(sid, 0, 1, 0, 3, {"textFormat": {"bold": True, "fontSize": 12}}),
            self._header_req(sid, 2, 3),
            self._freeze_req(sid, 3),
            self._resize_req(sid, 3),
        ])

    # ── ВСЕ SKU ───────────────────────────────────────────────────────────────

    def _update_all_skus(self, metrics: list[SKUMetrics]):
        ws = self._get_sheet("📦 ВСЕ SKU")
        ws.clear()
        sid = ws.id
        rows = [
            [f"📦 ВСЕ SKU OZON — {datetime.now().strftime('%d.%m.%Y %H:%M')}"],
            [""], SHEET_HEADERS,
        ]
        rows.extend(m.to_row() for m in metrics)
        self._write(ws, rows)
        time.sleep(1)
        self._batch(
            self._delete_banding(sid) + self._delete_cf_rules(sid) + [
                self._header_req(sid, 2, NUM_COLS),
                {"addBanding": {"bandedRange": {
                    "range": {"sheetId": sid, "startRowIndex": 3,
                              "startColumnIndex": 0, "endColumnIndex": NUM_COLS},
                    "rowProperties": {
                        "firstBandColor":  _hex("#ffffff"),
                        "secondBandColor": _hex("#f8f9fa"),
                    },
                }}},
                {"addConditionalFormatRule": {"rule": {
                    "ranges": [{"sheetId": sid, "startRowIndex": 3,
                                "startColumnIndex": TURNOVER_COL,
                                "endColumnIndex": TURNOVER_COL+1}],
                    "gradientRule": {
                        "minpoint": {"color": _hex("#43a047"), "type": "NUMBER", "value": "0"},
                        "midpoint": {"color": _hex("#ffb300"), "type": "NUMBER", "value": "30"},
                        "maxpoint": {"color": _hex("#d32f2f"), "type": "NUMBER", "value": "90"},
                    },
                }, "index": 0}},
                self._freeze_req(sid, 3),
                self._resize_req(sid),
            ]
        )

    # ── АРХИВ ─────────────────────────────────────────────────────────────────

    def _update_archive(self, archived: dict[str, SKUMetrics]):
        ws = self._get_sheet("📦 АРХИВ")
        ws.clear()
        sid = ws.id
        archive_headers = [
            "Артикул", "Название", "Категория",
            "Последний заказ", "Дней без продаж", "Последняя цена", "Остаток",
        ]
        rows = [
            ["АРХИВ — товары без продаж 90+ дней"],
            [""],
            archive_headers,
        ]
        sorted_archived = sorted(archived.values(), key=lambda m: m.offer_id)
        for m in sorted_archived:
            rows.append([
                m.offer_id,
                m.name[:50],
                m.category,
                "—",
                90,
                m.price,
                m.stock,
            ])
        self._write(ws, rows)
        time.sleep(1)
        ncols = len(archive_headers)
        self._batch([
            self._cell_req(sid, 0, 1, 0, ncols, {
                "textFormat": {"bold": True, "fontSize": 12},
            }),
            self._header_req(sid, 2, ncols),
            self._freeze_req(sid, 3),
            self._resize_req(sid, ncols),
        ])
        logger.info(f"Лист АРХИВ: {len(sorted_archived)} товаров")

    # ── ИСТОРИЯ ───────────────────────────────────────────────────────────────

    def _save_history_snapshot(self, metrics: list[SKUMetrics]):
        ws      = self._get_sheet("📅 ИСТОРИЯ")
        headers = ["Дата", "Артикул", "Название", "Остаток", "Заказы 28д",
                   "Оборачив.", "△ %", "Цена", "Скидка %", "Статус"]
        existing = ws.get_all_values()
        if not existing:
            ws.append_row(headers)
        today = datetime.now().strftime("%d.%m.%Y")
        new_rows = [
            [today, m.offer_id, m.name[:40], m.stock, m.orders_28d,
             round(m.turnover_days, 0) if m.turnover_days < 999 else "—",
             round(m.demand_delta_pct, 1), m.price,
             round(m.discount_pct, 1), m.status]
            for m in metrics if m.stock > 0 or m.orders_28d > 0
        ]
        for i in range(0, len(new_rows), 500):
            ws.append_rows(new_rows[i:i+500], value_input_option="USER_ENTERED")
            time.sleep(1)
        logger.info(f"История: добавлено {len(new_rows)} строк за {today}")

    def _read_history_snapshot(self) -> dict[str, dict]:
        try:
            ws   = self._get_sheet("📅 ИСТОРИЯ")
            rows = ws.get_all_values()
        except Exception:
            return {}
        if len(rows) < 2:
            return {}
        headers = rows[0]
        try:
            i_id    = headers.index("Артикул")
            i_price = headers.index("Цена")
            i_stat  = headers.index("Статус")
        except ValueError:
            return {}
        latest: dict[str, dict] = {}
        for row in rows[1:]:
            if len(row) <= max(i_id, i_price, i_stat):
                continue
            oid    = row[i_id]
            price  = float(row[i_price]) if row[i_price] else 0.0
            status = row[i_stat]
            latest[oid] = {"price": price, "status": status}
        return latest

    # ── НАСТРОЙКИ ─────────────────────────────────────────────────────────────

    def setup_settings_sheet(self):
        ws = self._get_sheet("⚙️ НАСТРОЙКИ")
        ws.clear()
        rows = [
            ["⚙️ НАСТРОЙКИ OZON МОНИТОРА"], [""],
            ["Параметр", "Значение", "Описание"],
            ["Floor price по умолчанию",   config.FLOOR_PRICES["default"],            "Мин. цена руб."],
            ["Подушки декоративные",       config.FLOOR_PRICES["Подушки декоративные"],"Floor price"],
            ["Валики",                     config.FLOOR_PRICES["Валики"],             "Floor price"],
            ["Сухоцветы",                  config.FLOOR_PRICES["Сухоцветы"],          "Floor price"],
        ]
        ws.update("A1", rows)

    # ── ПРАВИЛА ───────────────────────────────────────────────────────────────

    def _setup_rules_sheet(self):
        ws = self._get_sheet("📋 ПРАВИЛА")
        ws.clear()
        sid  = ws.id
        rows = [
            ["📋 ПРАВИЛА OZON МОНИТОРА"], [""],
            ["РАЗДЕЛ 1 — СТАТУСЫ И ДЕЙСТВИЯ", "", "", ""],
            ["Статус", "Условие", "Действие", "Приоритет"],
            ["⚫ НЕТ СПРОСА",            "stock > 0, заказов 28д = 0",                               "Снизить цену −30% или вывезти",          "1"],
            ["🔴 КРИТИЧНЫЙ ОСТАТОК",     "оборачиваемость > 90 дней",                                "Снизить цену 20%, усилить трафик",       "1"],
            ["🟠 МЕДЛЕННЫЕ ПРОДАЖИ",     "оборачиваемость 60–90 дней",                               "Снизить цену 10–20%",                    "2"],
            ["🟡 ПЕРЕСМОТРЕТЬ ЦЕНУ",     "скидка > 20% и спрос не вырос",                           "Проверить карточку, SEO",                "1"],
            ["🔵 НУЖНА ПОСТАВКА",        "stock = 0 при заказах или stock < заказов 7д",             "Срочно пополнить",                       "1"],
            ["🟢 ПОДНЯТЬ ЦЕНУ",          "оборачиваемость < 21 дня",                                 "Поднять цену по таблице",                "3"],
            ["✅ СПРОС РАСТЁТ",          "спрос вырос > 20%",                                        "Готовиться к повышению цены",            "3"],
            ["⬜ НЕТ В НАЛИЧИИ",         "stock = 0, продаж нет 28д, но были в 90д",                 "Пополнить при необходимости",            "5"],
            ["📦 ВЫВЕДЕН ИЗ ПРОДАЖИ",   "stock = 0, продаж нет 90д",                               "Перенесён в АРХИВ",                      "6"],
            ["🟡 МОНИТОРИНГ",            "всё остальное",                                            "Продолжать мониторинг",                  "4"],
            [""],
            ["РАЗДЕЛ 2 — ПРАВИЛА ПОДЪЁМА ЦЕНЫ", "", "", ""],
            ["Оборачиваемость", "Динамика спроса", "Подъём цены", "Примечание"],
            ["< 7 дней",    "любая",   "+28%", "Срочное торможение"],
            ["7–14 дней",   "растёт",  "+20%", "Активный спрос"],
            ["7–14 дней",   "падает",  "+13%", "Спрос снижается"],
            ["14–21 день",  "растёт",  "+11%", "Плавная коррекция"],
            ["14–21 день",  "падает",  "+8%",  "Минимальный сигнал"],
            ["21–30 дней",  "любая",   "+7%",  "Профилактика"],
            ["> 30 дней",   "любая",   "—",    "Не поднимать"],
            [""],
            ["РАЗДЕЛ 3 — FLOOR PRICES", "", "", ""],
            ["Категория", "Минимальная цена (руб.)", "", ""],
        ] + [[cat, price, "", ""] for cat, price in config.FLOOR_PRICES.items()]

        self._write(ws, rows)
        time.sleep(1)
        reqs = []
        for row_idx in [2, 15, 25]:
            reqs.append(self._cell_req(sid, row_idx, row_idx+1, 0, 4, {
                "backgroundColor": _hex(HEADER_BG),
                "textFormat": {"bold": True, "foregroundColor": WHITE, "fontSize": 11},
            }))
        for row_idx in [3, 16, 26]:
            reqs.append(self._header_req(sid, row_idx, 4))
        reqs += [self._freeze_req(sid, 1), self._resize_req(sid, 4)]
        self._batch(reqs)
        logger.info("Лист ПРАВИЛА обновлён")

    # ── ОЧЕРЕДЬ ИЗМЕНЕНИЙ ─────────────────────────────────────────────────────

    _QUEUE_HEADERS = [
        "Артикул", "Название", "Категория",
        "Текущая цена", "Новая цена", "Изменение %",
        "Причина", "Дата добавления", "Согласовано", "Отправлено",
    ]

    def update_price_queue(self, sorted_metrics: list[SKUMetrics]) -> dict:
        if not self._queue_all_sent():
            logger.info("Очередь: есть неотправленные — пропуск")
            return {"skipped": True}

        ws       = self._get_sheet("📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ")
        sid      = ws.id
        today    = datetime.now().strftime("%d.%m.%Y")
        rows     = [self._QUEUE_HEADERS]
        n_up = n_down = 0

        for m in sorted_metrics:
            if "ПЕРЕСМОТРЕТЬ" in m.status:
                continue
            if "ПОДНЯТЬ ЦЕНУ" in m.status:
                np = calc_price_raise(m.turnover_days, m.demand_delta_pct, m.price)
                if not np:
                    continue
                pct = round((np / m.price - 1) * 100)
                rows.append([m.offer_id, m.name[:40], m.category,
                             m.price, np, f"+{pct}%", "Поднять цену", today, False, ""])
                n_up += 1
            elif ("НЕТ СПРОСА" in m.status or "МЕДЛЕННЫЕ" in m.status
                  or "КРИТИЧНЫЙ" in m.status):
                has_no_sales = "НЕТ СПРОСА" in m.status
                dec = calc_price_decrease(m.turnover_days, m.price, m.category,
                                          has_no_sales_14d=has_no_sales)
                if not dec:
                    continue
                np = dec["new_price"]
                pct = round((np / m.price - 1) * 100)
                if dec.get("is_wb_limit"):
                    new_p_display = f"{np} (лимит WB)"
                elif dec.get("is_floor"):
                    new_p_display = f"{np} (min)"
                else:
                    new_p_display = np
                rows.append([m.offer_id, m.name[:40], m.category,
                             m.price, new_p_display, f"{pct}%", "Снизить цену", today, False, ""])
                n_down += 1

        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        time.sleep(1)

        n_data = len(rows) - 1
        reqs   = [
            self._header_req(sid, 0, len(self._QUEUE_HEADERS)),
            self._freeze_req(sid, 1),
            self._resize_req(sid, len(self._QUEUE_HEADERS)),
        ]
        if n_data > 0:
            reqs.append({"setDataValidation": {
                "range": {"sheetId": sid, "startRowIndex": 1, "endRowIndex": 1+n_data,
                          "startColumnIndex": self._Q_APPROVE, "endColumnIndex": self._Q_APPROVE+1},
                "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
            }})
        self._batch(reqs)
        total = n_up + n_down
        logger.info(f"Очередь изменений: {total} SKU (↑{n_up} ↓{n_down})")
        return {"total": total, "n_up": n_up, "n_down": n_down}

    def _queue_all_sent(self) -> bool:
        rows = self._get_queue_rows()
        data = [r for r in rows[1:] if r and r[0].strip()]
        if not data:
            return True
        return all(len(r) > self._Q_SENT and r[self._Q_SENT].strip() for r in data)

    def queue_pending_count(self) -> int:
        rows = self._get_queue_rows()
        return sum(1 for r in rows[1:]
                   if r and r[0].strip()
                   and not (len(r) > self._Q_SENT and r[self._Q_SENT].strip()))

    def queue_unapproved_count(self) -> int:
        count = 0
        for r in self._get_queue_rows()[1:]:
            if not r or not r[0].strip():
                continue
            approved = (len(r) > self._Q_APPROVE
                        and r[self._Q_APPROVE].strip().upper() in ("TRUE", "ИСТИНА", "1"))
            sent     = len(r) > self._Q_SENT and r[self._Q_SENT].strip()
            if not approved and not sent:
                count += 1
        return count

    def _get_queue_rows(self) -> list[list]:
        try:
            return self._get_sheet("📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ").get_all_values()
        except Exception:
            return []

    def get_approved_changes(self) -> list[dict]:
        rows = self._get_queue_rows()
        if len(rows) < 2:
            return []
        result = []
        for i, r in enumerate(rows[1:], start=1):
            if not r or not r[0].strip():
                continue
            approved = (len(r) > self._Q_APPROVE
                        and r[self._Q_APPROVE].strip().upper() in ("TRUE", "ИСТИНА", "1"))
            sent     = len(r) > self._Q_SENT and r[self._Q_SENT].strip()
            if approved and not sent:
                result.append({
                    "row_index": i + 1,  # 1-based sheet row
                    "offer_id":  r[self._Q_OFFER],
                    "name":      r[self._Q_NAME],
                    "category":  r[self._Q_CAT],
                    "old_price": r[self._Q_CUR],
                    "new_price": r[self._Q_NEW],
                    "reason":    r[self._Q_REASON],
                })
        return result

    def mark_sent(self, row_index: int, status: str = ""):
        ws        = self._get_sheet("📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ")
        sent_col  = self._Q_SENT + 1  # gspread is 1-based
        sent_val  = status or datetime.now().strftime("%d.%m.%Y %H:%M")
        ws.update_cell(row_index, sent_col, sent_val)

    # ── ЭФФЕКТИВНОСТЬ ─────────────────────────────────────────────────────────

    _EFF_HEADERS = [
        "Артикул", "Название", "Действие",
        "Цена до", "Цена после", "Изм. %", "Дата",
        "Точка", "Заказы/нед", "Оборачиваемость", "Итог",
    ]

    def record_price_change(self, offer_id, name, action, price_before, price_after,
                            orders_week, turnover_days):
        try:
            ws       = self._get_sheet("📊 ЭФФЕКТИВНОСТЬ")
            existing = ws.get_all_values()
            if not existing:
                ws.append_row(self._EFF_HEADERS)
            pct_str = f"{(price_after/price_before-1)*100:+.0f}%" if price_before else "—"
            ws.append_row([
                offer_id, name[:40], action,
                price_before, price_after, pct_str,
                datetime.now().strftime("%d.%m.%Y"),
                "База", round(orders_week, 1), round(turnover_days, 0), "⏳",
            ], value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning(f"Ошибка record_price_change {offer_id}: {e}")

    def update_effectiveness_checkpoints(self, metrics: dict[str, SKUMetrics]):
        try:
            ws   = self._get_sheet("📊 ЭФФЕКТИВНОСТЬ")
            rows = ws.get_all_values()
        except Exception:
            return
        if len(rows) < 2:
            return
        headers = rows[0]
        try:
            i_id     = headers.index("Артикул")
            i_action = headers.index("Действие")
            i_before = headers.index("Цена до")
            i_after  = headers.index("Цена после")
            i_pct    = headers.index("Изм. %")
            i_date   = headers.index("Дата")
            i_point  = headers.index("Точка")
            i_orders = headers.index("Заказы/нед")
        except ValueError:
            return
        now      = datetime.now()
        existing = {(r[i_id], r[i_date], r[i_point]) for r in rows[1:]
                    if len(r) > max(i_id, i_date, i_point)}
        new_rows = []
        for r in rows[1:]:
            if len(r) <= max(i_id, i_date, i_point) or r[i_point] != "База":
                continue
            try:
                oid        = r[i_id]
                change_dt  = datetime.strptime(r[i_date], "%d.%m.%Y")
                base_orders= float(r[i_orders]) if r[i_orders] else 0
                action     = r[i_action]
            except (ValueError, IndexError):
                continue
            days_passed = (now - change_dt).days
            m = metrics.get(oid)
            if not m:
                continue
            for point, min_days in [("День 7", 7), ("День 14", 14)]:
                if (oid, r[i_date], point) in existing or days_passed < min_days:
                    continue
                if point == "День 7":
                    result = "⏳ ждём"
                elif action == "Поднять цену":
                    result = "✅ работает" if m.turnover_days > 21 else "❌ нет эффекта"
                else:
                    pct_chg = ((m.avg_weekly - base_orders) / base_orders * 100
                               if base_orders > 0 else 0)
                    result = "✅ работает" if pct_chg > 20 else "❌ нет эффекта"
                new_rows.append([
                    oid, m.name[:40], action,
                    r[i_before], r[i_after], r[i_pct], r[i_date],
                    point, round(m.avg_weekly, 1), round(m.turnover_days, 0), result,
                ])
        if new_rows:
            ws.append_rows(new_rows, value_input_option="USER_ENTERED")
            logger.info(f"Эффективность: +{len(new_rows)} строк")


# ── Статус ЦЕНА ПОДНЯТА ───────────────────────────────────────────────────────

def _apply_price_raised(metrics: dict[str, SKUMetrics], history: dict[str, dict]):
    for oid, m in metrics.items():
        h = history.get(oid)
        if not h:
            continue
        prev_status = h.get("status", "")
        prev_price  = h.get("price", 0.0)
        if "ПОДНЯТЬ ЦЕНУ" in prev_status and m.price > prev_price * 1.05:
            m.status   = "🔵 ЦЕНА ПОДНЯТА"
            m.priority = 3
