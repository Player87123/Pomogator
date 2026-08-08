import json
import logging
import random
import re
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from vkbottle.bot import Bot, Message

import db
import seed_data
from config import CHECK_INTERVAL_MINUTES, GROUP_TOKEN, REMINDER_HOURS, SEED_PEER_ID

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("poll-bot")

bot = Bot(token=GROUP_TOKEN)
db.init_db()


def rid():
    """Случайный random_id для messages.send (VK требует уникальности)."""
    return random.randint(1, 2_147_483_647)


def parse_poll_command(text: str):
    """
    Форматы:
      /poll Вопрос?|Вариант1|Вариант2|Вариант3
    или несколькими строками:
      /poll
      Вопрос?
      Вариант1
      Вариант2
    Возвращает (question, options) или None, если формат некорректен.
    """
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    payload = parts[1].strip()

    if "\n" in payload:
        lines = [line.strip() for line in payload.split("\n") if line.strip()]
    else:
        lines = [p.strip() for p in payload.split("|") if p.strip()]

    if len(lines) < 3:  # вопрос + минимум 2 варианта
        return None

    question, *options = lines
    return question, options[:10]  # VK допускает максимум 10 вариантов


async def get_answered_user_ids(owner_id: int, vk_poll_id: int) -> set:
    poll = await bot.api.polls.get_by_id(owner_id=owner_id, poll_id=vk_poll_id)
    answer_ids = [a.id for a in poll.answers]
    if not answer_ids:
        return set()
    voters = await bot.api.polls.get_voters(
        owner_id=owner_id, poll_id=vk_poll_id, answer_ids=answer_ids
    )
    answered = set()
    for group in voters:
        answered.update(group.users.items)
    return answered


async def mentions_for(user_ids) -> list:
    if not user_ids:
        return []
    users = await bot.api.users.get(user_ids=list(user_ids))
    return [f"[id{u.id}|{u.first_name} {u.last_name}]" for u in users]


MENTION_RE = re.compile(r"\[id(\d+)\|[^\]]*\]")


def extract_mentions(text: str) -> list:
    """Достаёт id пользователей из VK-упоминаний вида [id123|Имя] в тексте."""
    seen = []
    for m in MENTION_RE.findall(text):
        uid = int(m)
        if uid not in seen:
            seen.append(uid)
    return seen


async def is_chat_admin(peer_id: int, user_id: int) -> bool:
    try:
        members = await bot.api.messages.get_conversation_members(peer_id=peer_id)
    except Exception:
        log.exception("Не удалось получить список участников беседы %s", peer_id)
        return False
    for m in members.items:
        if m.member_id == user_id:
            return bool(getattr(m, "is_admin", False) or getattr(m, "is_owner", False))
    return False


@bot.on.message(func=lambda m: m.text.startswith("/poll"))
async def create_poll_handler(message: Message):
    if message.text.startswith("/polls"):
        # /polls обрабатывается отдельным хендлером ниже
        return
    parsed = parse_poll_command(message.text)
    if not parsed:
        await message.answer(
            "Формат:\n"
            "/poll Вопрос?|Вариант1|Вариант2\n"
            "или несколькими строками:\n"
            "/poll\nВопрос?\nВариант1\nВариант2"
        )
        return

    question, options = parsed
    peer_id = message.peer_id

    poll = await bot.api.polls.create(
        question=question,
        add_answers=json.dumps(options, ensure_ascii=False),
        is_anonymous=False,  # обязательно, иначе нельзя узнать, кто ответил
        peer_id=peer_id,
    )

    await bot.api.messages.send(
        peer_id=peer_id,
        random_id=rid(),
        attachment=f"poll{poll.owner_id}_{poll.id}",
    )

    members = await bot.api.messages.get_conversation_members(peer_id=peer_id)
    participant_ids = [m.member_id for m in members.items if m.member_id > 0]

    local_id = db.save_poll(
        vk_poll_id=poll.id,
        owner_id=poll.owner_id,
        peer_id=peer_id,
        question=question,
        participant_ids=participant_ids,
        reminder_hours=REMINDER_HOURS,
    )

    await message.answer(
        f"Опрос №{local_id} создан. Напоминание не ответившим придёт через "
        f"{REMINDER_HOURS:g} ч. Проверить статистику: /stats {local_id}"
    )


@bot.on.message(text="/polls")
async def list_polls_handler(message: Message):
    polls = db.list_active_polls(message.peer_id)
    if not polls:
        await message.answer("Активных опросов в этой беседе нет.")
        return
    text = "Активные опросы:\n" + "\n".join(
        f"№{p['id']}: {p['question']}" for p in polls
    )
    await message.answer(text)


@bot.on.message(func=lambda m: m.text.startswith("/stats"))
async def stats_handler(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Укажите номер опроса: /stats <номер>")
        return

    local_id = int(parts[1])
    poll = db.get_poll(local_id)
    if not poll:
        await message.answer("Опрос с таким номером не найден.")
        return

    answered_ids = await get_answered_user_ids(poll["owner_id"], poll["vk_poll_id"])
    participants = set(poll["participant_ids"])
    answered_participants = answered_ids & participants
    not_answered = participants - answered_ids

    text = (
        f"📊 Опрос №{local_id}: «{poll['question']}»\n"
        f"Ответили: {len(answered_participants)} из {len(participants)}\n"
    )
    if not_answered:
        mentions = await mentions_for(not_answered)
        text += "Не ответили:\n" + "\n".join(mentions)
    else:
        text += "Все ответили ✅"

    await message.answer(text)


@bot.on.message(func=lambda m: m.text.startswith("/add_child"))
async def add_child_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Добавлять детей могут только администраторы беседы.")
        return

    raw = message.text[len("/add_child"):].strip()
    name_part = raw.split("|", 1)[0].strip()
    if not name_part:
        await message.answer(
            "Формат: /add_child Имя Фамилия | @родитель1 @родитель2\n"
            "(родителей нужно выбрать из подсказок при вводе @, чтобы они стали ссылками)"
        )
        return

    parent_ids = extract_mentions(raw)
    child_id = db.add_child(message.peer_id, name_part)
    for pid in parent_ids:
        db.add_parent(child_id, pid)

    if parent_ids:
        parents_text = ", ".join(await mentions_for(parent_ids))
    else:
        parents_text = f"не указаны (добавить: /add_parent {child_id} @родитель)"

    await message.answer(f"Ребёнок «{name_part}» добавлен, №{child_id}. Родители: {parents_text}")


@bot.on.message(func=lambda m: m.text.startswith("/add_parent"))
async def add_parent_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Изменять список родителей могут только администраторы беседы.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /add_parent <номер_ребёнка> @родитель")
        return

    child_id = int(parts[1])
    if not db.get_child(child_id):
        await message.answer("Ребёнок с таким номером не найден.")
        return

    parent_ids = extract_mentions(message.text)
    if not parent_ids:
        await message.answer("Не нашёл упоминания. Выберите родителя из подсказок при вводе @.")
        return

    for pid in parent_ids:
        db.add_parent(child_id, pid)

    mentions = ", ".join(await mentions_for(parent_ids))
    await message.answer(f"Добавлено родителям ребёнка №{child_id}: {mentions}")


@bot.on.message(func=lambda m: m.text.startswith("/remove_parent"))
async def remove_parent_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Изменять список родителей могут только администраторы беседы.")
        return

    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /remove_parent <номер_ребёнка> @родитель")
        return

    child_id = int(parts[1])
    parent_ids = extract_mentions(message.text)
    if not parent_ids:
        await message.answer("Не нашёл упоминания. Выберите родителя из подсказок при вводе @.")
        return

    for pid in parent_ids:
        db.remove_parent(child_id, pid)

    await message.answer(f"Родитель(и) откреплены от ребёнка №{child_id}.")


@bot.on.message(func=lambda m: m.text.startswith("/remove_child"))
async def remove_child_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Удалять детей могут только администраторы беседы.")
        return

    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /remove_child <номер_ребёнка>")
        return

    child_id = int(parts[1])
    if not db.get_child(child_id):
        await message.answer("Ребёнок с таким номером не найден.")
        return

    db.remove_child(child_id)
    await message.answer(f"Ребёнок №{child_id} удалён из списка.")


@bot.on.message(text="/children")
async def list_children_handler(message: Message):
    children = db.list_children(message.peer_id)
    if not children:
        await message.answer("Список детей пока пуст.")
        return

    all_parent_ids = {pid for c in children for pid in c["parent_ids"]}
    mention_map = {}
    if all_parent_ids:
        users = await bot.api.users.get(user_ids=list(all_parent_ids))
        mention_map = {u.id: f"[id{u.id}|{u.first_name} {u.last_name}]" for u in users}

    lines = []
    for c in children:
        parents = (
            ", ".join(mention_map.get(pid, f"id{pid}") for pid in c["parent_ids"])
            if c["parent_ids"]
            else "родители не указаны"
        )
        lines.append(f"№{c['id']}: {c['full_name']} — {parents}")

    await message.answer("Список детей:\n" + "\n".join(lines))


@bot.on.message(text="/my_children")
async def my_children_handler(message: Message):
    children = db.get_children_by_parent(message.peer_id, message.from_id)
    if not children:
        await message.answer("За вами не закреплено ни одного ребёнка в этой беседе.")
        return
    lines = [f"№{c['id']}: {c['full_name']}" for c in children]
    await message.answer("Ваши дети:\n" + "\n".join(lines))


@bot.on.message(func=lambda m: m.text.startswith("/tournament_close"))
async def tournament_close_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Закрывать турниры могут только администраторы беседы.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /tournament_close <номер_турнира>")
        return
    tournament_id = int(parts[1])
    if not db.get_tournament(tournament_id):
        await message.answer("Турнир с таким номером не найден.")
        return
    db.close_tournament(tournament_id)
    await message.answer(f"Турнир №{tournament_id} закрыт.")


@bot.on.message(func=lambda m: m.text.startswith("/tournament"))
async def tournament_create_handler(message: Message):
    # более специфичные команды перехватываются своими хендлерами выше/ниже —
    # этот обрабатывает только "голый" /tournament для создания турнира
    if message.text.startswith(("/tournament_close", "/tournament_add_child", "/tournaments")):
        return

    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Создавать турниры могут только администраторы беседы.")
        return

    raw = message.text[len("/tournament"):].strip()
    if "|" not in raw:
        await message.answer("Формат: /tournament Название | Сумма_за_ребёнка")
        return

    name_part, amount_part = raw.split("|", 1)
    name = name_part.strip()
    try:
        amount = float(amount_part.strip().replace(",", "."))
    except ValueError:
        await message.answer("Сумма должна быть числом. Формат: /tournament Название | Сумма")
        return

    if not name or amount <= 0:
        await message.answer("Формат: /tournament Название | Сумма_за_ребёнка")
        return

    children = db.list_children(message.peer_id)
    if not children:
        await message.answer("В списке пока нет ни одного ребёнка (/add_child), добавлять не с кого.")
        return

    child_ids = [c["id"] for c in children]
    tournament_id = db.create_tournament(message.peer_id, name, amount, child_ids)

    await message.answer(
        f"Турнир «{name}» создан, №{tournament_id}. Сумма с ребёнка: {amount:g}₽.\n"
        f"Участников: {len(child_ids)}.\n"
        f"Родители отмечают оплату командой /paid <номер_ребёнка>.\n"
        f"Посмотреть долги: /debts {tournament_id}"
    )


@bot.on.message(text="/tournaments")
async def tournaments_list_handler(message: Message):
    tournaments = db.list_active_tournaments(message.peer_id)
    if not tournaments:
        await message.answer("Активных турниров нет.")
        return
    lines = [f"№{t['id']}: {t['name']} — {t['amount']:g}₽ с ребёнка" for t in tournaments]
    await message.answer("Активные турниры:\n" + "\n".join(lines))


@bot.on.message(func=lambda m: m.text.startswith("/tournament_add_child"))
async def tournament_add_child_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Изменять состав турнира могут только администраторы беседы.")
        return
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /tournament_add_child <номер_турнира> <номер_ребёнка>")
        return
    tournament_id, child_id = int(parts[1]), int(parts[2])
    if not db.get_tournament(tournament_id):
        await message.answer("Турнир с таким номером не найден.")
        return
    if not db.get_child(child_id):
        await message.answer("Ребёнок с таким номером не найден.")
        return
    db.add_tournament_participant(tournament_id, child_id)
    await message.answer(f"Ребёнок №{child_id} добавлен в турнир №{tournament_id}.")


@bot.on.message(func=lambda m: m.text.startswith("/paid"))
async def paid_handler(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /paid <номер_ребёнка> [номер_турнира]")
        return

    child_id = int(parts[1])
    child = db.get_child(child_id)
    if not child:
        await message.answer("Ребёнок с таким номером не найден.")
        return

    is_parent = message.from_id in child["parent_ids"]
    is_admin = await is_chat_admin(message.peer_id, message.from_id)
    if not (is_parent or is_admin):
        await message.answer(
            "Отметить оплату может только родитель этого ребёнка или администратор беседы."
        )
        return

    if len(parts) >= 3 and parts[2].isdigit():
        tournament_id = int(parts[2])
        tournament = db.get_tournament(tournament_id)
    else:
        tournament = db.get_latest_open_tournament(message.peer_id)
        tournament_id = tournament["id"] if tournament else None

    if not tournament:
        await message.answer("Активный турнир не найден. Укажите номер: /paid <ребёнок> <турнир>")
        return

    if not db.is_tournament_participant(tournament_id, child_id):
        db.add_tournament_participant(tournament_id, child_id)

    db.mark_paid(tournament_id, child_id, message.from_id)
    await message.answer(
        f"Оплата за «{child['full_name']}» по турниру «{tournament['name']}» "
        f"({tournament['amount']:g}₽) отмечена ✅"
    )


@bot.on.message(func=lambda m: m.text.startswith("/unpaid"))
async def unpaid_handler(message: Message):
    if not await is_chat_admin(message.peer_id, message.from_id):
        await message.answer("Снимать отметку об оплате может только администратор беседы.")
        return
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /unpaid <номер_ребёнка> [номер_турнира]")
        return

    child_id = int(parts[1])
    if len(parts) >= 3 and parts[2].isdigit():
        tournament_id = int(parts[2])
    else:
        tournament = db.get_latest_open_tournament(message.peer_id)
        if not tournament:
            await message.answer("Активный турнир не найден. Укажите номер турнира явно.")
            return
        tournament_id = tournament["id"]

    db.mark_unpaid(tournament_id, child_id)
    await message.answer(f"Отметка об оплате снята (ребёнок №{child_id}, турнир №{tournament_id}).")


@bot.on.message(func=lambda m: m.text.startswith("/debts"))
async def debts_handler(message: Message):
    parts = message.text.split()
    if len(parts) >= 2 and parts[1].isdigit():
        tournament_id = int(parts[1])
    else:
        tournament = db.get_latest_open_tournament(message.peer_id)
        if not tournament:
            await message.answer("Активный турнир не найден. Формат: /debts <номер_турнира>")
            return
        tournament_id = tournament["id"]

    tournament = db.get_tournament(tournament_id)
    if not tournament:
        await message.answer("Турнир с таким номером не найден.")
        return

    status = db.get_tournament_status(tournament_id)
    if not status:
        await message.answer(f"В турнире «{tournament['name']}» пока нет участников.")
        return

    debtors = [row for row in status if not row["paid"]]
    paid_count = len(status) - len(debtors)
    collected = paid_count * tournament["amount"]
    total = len(status) * tournament["amount"]

    text = (
        f"💰 Турнир «{tournament['name']}» ({tournament['amount']:g}₽ с ребёнка)\n"
        f"Собрано: {collected:g}₽ из {total:g}₽ ({paid_count}/{len(status)} оплатили)\n"
    )

    if debtors:
        all_parent_ids = {pid for row in debtors for pid in row["parent_ids"]}
        mention_map = {}
        if all_parent_ids:
            users = await bot.api.users.get(user_ids=list(all_parent_ids))
            mention_map = {u.id: f"[id{u.id}|{u.first_name} {u.last_name}]" for u in users}

        lines = []
        for row in debtors:
            parents = (
                ", ".join(mention_map.get(pid, f"id{pid}") for pid in row["parent_ids"])
                if row["parent_ids"]
                else "родители не указаны"
            )
            lines.append(f"№{row['child_id']} {row['full_name']} — должны {parents}")
        text += "Не оплатили:\n" + "\n".join(lines)
    else:
        text += "Все оплатили ✅"

    await message.answer(text)


async def check_reminders():
    for poll in db.get_pending_reminders():
        created_at = datetime.fromisoformat(poll["created_at"])
        deadline = created_at + timedelta(hours=poll["reminder_hours"])
        if datetime.utcnow() < deadline:
            continue

        try:
            answered_ids = await get_answered_user_ids(
                poll["owner_id"], poll["vk_poll_id"]
            )
        except Exception:
            log.exception("Не удалось получить голоса для опроса №%s", poll["id"])
            continue

        participants = set(poll["participant_ids"])
        not_answered = participants - answered_ids

        if not_answered:
            mentions = await mentions_for(not_answered)
            text = (
                f"⏰ Напоминание по опросу «{poll['question']}»\n"
                f"Ещё не ответили ({len(not_answered)}):\n" + "\n".join(mentions)
            )
        else:
            text = f"✅ Опрос «{poll['question']}»: все участники ответили."

        await bot.api.messages.send(
            peer_id=poll["peer_id"], random_id=rid(), message=text
        )
        db.mark_reminded(poll["id"])
        log.info("Напоминание по опросу №%s отправлено", poll["id"])


scheduler = AsyncIOScheduler()
scheduler.add_job(check_reminders, "interval", minutes=CHECK_INTERVAL_MINUTES)


async def seed_initial_data():
    """Один раз при первом запуске заливает данные из seed_data.py в БД для SEED_PEER_ID."""
    if not SEED_PEER_ID:
        return
    if db.count_children(SEED_PEER_ID) > 0:
        log.info("Данные для беседы %s уже загружены, seed пропущен.", SEED_PEER_ID)
        return

    log.info("Загружаю зашитые данные детей/родителей для беседы %s...", SEED_PEER_ID)
    resolve_failed = []

    for child_name, parents in seed_data.CHILDREN:
        child_id = db.add_child(SEED_PEER_ID, child_name)
        for parent_name, token in parents:
            if token is None:
                continue  # VK-профиль неизвестен, добавляется вручную через /add_parent
            if isinstance(token, int):
                db.add_parent(child_id, token)
                continue
            try:
                result = await bot.api.utils.resolve_screen_name(screen_name=token)
            except Exception:
                result = None
            if result and getattr(result, "type", None) == "user":
                db.add_parent(child_id, result.object_id)
            else:
                resolve_failed.append((child_name, parent_name, token))

    log.info("Загрузка завершена: детей добавлено %d.", len(seed_data.CHILDREN))
    if resolve_failed:
        log.warning(
            "Не удалось определить VK id для: %s. Добавьте вручную через /add_parent.",
            resolve_failed,
        )


@bot.on.message(text="/chatid")
async def chatid_handler(message: Message):
    await message.answer(f"peer_id этой беседы: {message.peer_id}")


@bot.on.message(text="/help")
async def help_handler(message: Message):
    await message.answer(
        "Опросы:\n"
        "/poll Вопрос?|Вариант1|Вариант2 — создать опрос\n"
        "/polls — список активных опросов\n"
        "/stats <номер> — статистика по опросу и кто не ответил\n"
        f"Напоминание отправляется автоматически через {REMINDER_HOURS:g} ч. после старта опроса.\n\n"
        "Дети и родители (только для админов беседы, кроме /children и /my_children):\n"
        "/add_child Имя Фамилия | @родитель1 @родитель2 — добавить ребёнка\n"
        "/add_parent <номер_ребёнка> @родитель — прикрепить ещё одного родителя\n"
        "/remove_parent <номер_ребёнка> @родитель — открепить родителя\n"
        "/remove_child <номер_ребёнка> — удалить ребёнка из списка\n"
        "/children — список всех детей с родителями\n"
        "/my_children — список ваших детей\n\n"
        "Турниры и оплаты:\n"
        "/tournament Название | Сумма — создать турнир (админы, сумма одна для всех детей из списка)\n"
        "/tournaments — список активных турниров\n"
        "/paid <номер_ребёнка> [номер_турнира] — отметить оплату (родитель ребёнка или админ)\n"
        "/unpaid <номер_ребёнка> [номер_турнира] — снять отметку об оплате (админы)\n"
        "/debts [номер_турнира] — кто ещё не оплатил и сколько собрано\n"
        "/tournament_add_child <турнир> <ребёнок> — добавить ребёнка в уже созданный турнир (админы)\n"
        "/tournament_close <номер_турнира> — закрыть турнир (админы)"
    )


async def on_startup():
    scheduler.start()
    await seed_initial_data()


if __name__ == "__main__":
    log.info("Бот запускается...")
    bot.loop_wrapper.on_startup.append(on_startup())
    bot.run_forever()
