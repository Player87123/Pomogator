"""
Импорт детей и родителей в базу бота из данных, зашитых в seed_data.py.

Запуск (из папки бота, рядом с polls.db и .env):
    python seed_import.py <peer_id>

peer_id беседы можно узнать, отправив в чат команду /chatid (бот ответит числом).

Скрипт можно запускать повторно (например, после правки seed_data.py) —
уже привязанные родители не задублируются (используется INSERT OR IGNORE).
Повторный запуск создаст детей заново — если нужно обновить данные, а не
добавить новых, сначала удалите старые записи командой /remove_child.
"""
import asyncio
import sys

from vkbottle import API

import db
from config import GROUP_TOKEN
from seed_data import CHILDREN


async def resolve_screen_name(api: API, screen_name: str):
    try:
        result = await api.utils.resolve_screen_name(screen_name=screen_name)
    except Exception:
        return None
    if result and getattr(result, "type", None) == "user":
        return result.object_id
    return None


async def main():
    if len(sys.argv) < 2:
        print("Использование: python seed_import.py <peer_id>")
        return

    peer_id = int(sys.argv[1])
    api = API(token=GROUP_TOKEN)
    db.init_db()

    print(f"Детей в seed_data.py: {len(CHILDREN)}\n")

    no_link = []          # (ребёнок, родитель) - в данных вообще нет VK-идентификатора
    resolve_failed = []   # (ребёнок, родитель, screen_name) - не удалось резолвнуть
    added = 0

    for child_name, parents in CHILDREN:
        child_id = db.add_child(peer_id, child_name)
        linked = 0

        for parent_name, token in parents:
            if token is None:
                no_link.append((child_name, parent_name))
                continue
            if isinstance(token, int):
                vk_id = token
            else:
                vk_id = await resolve_screen_name(api, token)
                if vk_id is None:
                    resolve_failed.append((child_name, parent_name, token))
                    continue
            db.add_parent(child_id, vk_id)
            linked += 1

        added += 1
        print(f"№{child_id}: {child_name} — привязано родителей: {linked}/{len(parents)}")

    print(f"\nГотово. Добавлено детей: {added}")

    if resolve_failed:
        print("\nНе удалось определить VK id по нику (возможно, профиль удалён/переименован):")
        for child_name, parent_name, token in resolve_failed:
            print(f"  {child_name}: {parent_name} (@{token})")

    if no_link:
        print("\nVK-профиль неизвестен — добавьте вручную через /add_parent <номер> @упоминание:")
        for child_name, parent_name in no_link:
            print(f"  {child_name}: {parent_name}")


if __name__ == "__main__":
    asyncio.run(main())
