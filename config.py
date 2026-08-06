import os

from dotenv import load_dotenv

load_dotenv()

GROUP_TOKEN = os.getenv("GROUP_TOKEN")
REMINDER_HOURS = float(os.getenv("REMINDER_HOURS", "24"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))

if not GROUP_TOKEN:
    raise RuntimeError(
        "Не найден GROUP_TOKEN. Скопируйте .env.example в .env и укажите токен сообщества."
    )
