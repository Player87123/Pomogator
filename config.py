import os

from dotenv import load_dotenv

load_dotenv()

GROUP_TOKEN = os.getenv("GROUP_TOKEN")
REMINDER_HOURS = float(os.getenv("REMINDER_HOURS", "24"))
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "10"))
SEED_PEER_ID = os.getenv("SEED_PEER_ID")
SEED_PEER_ID = int(SEED_PEER_ID) if SEED_PEER_ID else None

if not GROUP_TOKEN:
    raise RuntimeError(
        "Не найден GROUP_TOKEN. Скопируйте .env.example в .env и укажите токен сообщества."
    )
