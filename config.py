import os

OZON_CLIENT_ID = os.getenv("OZON_CLIENT_ID", "")
OZON_API_KEY   = os.getenv("OZON_API_KEY", "")
GOOGLE_SHEETS_ID         = os.getenv("OZON_SHEETS_ID", "")
GOOGLE_CREDENTIALS_PATH  = os.getenv("GOOGLE_CREDENTIALS_PATH", "google_credentials.json")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

FLOOR_PRICES: dict[str, int] = {
    "Подушки декоративные": 450,
    "Валики":               450,
    "Сухоцветы":            800,
    "default":              200,
}

def floor_price(category: str) -> int:
    return FLOOR_PRICES.get(category, FLOOR_PRICES["default"])
