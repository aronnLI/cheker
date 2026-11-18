"""
Проверка доступности юзернеймов в Telegram через Telethon.

Как это работает
----------------
Скрипт по очереди проверяет имена методом account.checkUsername и пишет
освободившиеся в free_usernames.txt. Между проверками — пауза, всё в одном
потоке, без прокси и без обхода FloodWait.

ИНСТРУКЦИЯ ПО ЗАПУСКУ
---------------------
1. Установите зависимости:

       pip install telethon

   Нужен Python 3.9+ и интернет. Прокси не используется и не поддерживается.

2. Получите api_id и api_hash:

   - откройте https://my.telegram.org и войдите по своему номеру телефона
     (код придёт в приложение Telegram, не по SMS);
   - выберите «API development tools»;
   - заполните название приложения (App title / Short name могут быть любыми,
     например "checker") и платформу (Desktop);
   - после создания страница покажет api_id (число) и api_hash (строка);
   - впишите их в переменные API_ID и API_HASH ниже.

   Эти данные привязаны к аккаунту. Не публикуйте их и не передавайте третьим
   лицам: по ним можно выпустить сессию от вашего имени.

3. Сессия. При первом запуске файла <SESSION_NAME>.session ещё нет, программа
   сама создаст его в текущей папке:

   - спросит номер телефона в международном формате (+79001234567);
   - отправит код, попросит ввести его из чата Telegram;
   - если включена двухэтапная аутентификация — попросит пароль (ввод скрыт).

   После входа файл сессии переиспользуется: повторный ввод кода не нужен.
   Файл сессии равносилен паролю — не выкладывайте его и не передавайте никому.
   Чтобы «выйти», удалите <SESSION_NAME>.session (и .session-journal, если есть).

4. Укажите, что проверять:
   - MANUAL_USERNAMES — свой список (можно без @, регистр приводится к нижнему);
   - GENERATE_COUNT — сколько имён сгенерировать случайно (5–10 символов, a-z и 0-9).

5. Запуск:

       python username_checker.py

   Результат: free_usernames.txt (только свободные, по одному в строке) и
   progress.json (состояние на момент остановки).
"""

import asyncio
import getpass
import json
import random
import string
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient, errors, functions

# ============================ НАСТРОЙКИ ============================

# Данные приложения с my.telegram.org (см. инструкцию выше).
API_ID = 32228834         # число, например 1234567
API_HASH = "f553324ff9949072e551e274d5199ea8"  # строка из 32 символов

# Имя (или путь) файла сессии. При первом запуске будет создан <SESSION_NAME>.session.
SESSION_NAME = "username_checker"

# Свои кандидаты. Можно с «@» или без, регистр не важен — приведём к нижнему.
MANUAL_USERNAMES = [
    # "пример", "mynick2026",
]

# Сколько имён сгенерировать случайно (0 — только ручной список).
GENERATE_COUNT = 50

# Длина генерируемых имён и алфавит.
MIN_LEN = 5
MAX_LEN = 10

# Telegram не принимает имена, начинающиеся с цифры (ответ USERNAME_INVALID),
# поэтому по умолчанию первым символом ставим букву. Остальные символы — a-z и 0-9.
START_WITH_LETTER = True

# Пауза между проверками, секунды. Меньше делать не стоит: риск блокировки.
DELAY_SECONDS = 3.0

# Файлы результата.
FREE_FILE = "free_usernames.txt"
PROGRESS_FILE = "progress.json"

# ==================================================================

ALPHABET = string.ascii_lowercase + string.digits   # a-z и 0-9
LETTERS = string.ascii_lowercase                    # только буквы


def random_username() -> str:
    """Случайное имя длиной MIN_LEN..MAX_LEN из a-z и 0-9."""
    length = random.randint(MIN_LEN, MAX_LEN)
    if START_WITH_LETTER:
        # Первый символ — буква, остальные — из общего алфавита.
        return random.choice(LETTERS) + "".join(
            random.choice(ALPHABET) for _ in range(length - 1)
        )
    return "".join(random.choice(ALPHABET) for _ in range(length))


def build_candidates() -> list[str]:
    """Ручной список + случайные имена, без дублей."""
    candidates: list[str] = []
    seen: set[str] = set()

    # Ручной список: убираем @, пробелы, приводим к нижнему регистру.
    for raw in MANUAL_USERNAMES:
        name = raw.strip().lstrip("@").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        candidates.append(name)

    # Добираем случайные имена до GENERATE_COUNT штук.
    generated = 0
    while generated < GENERATE_COUNT:
        name = random_username()
        if name in seen:
            continue
        seen.add(name)
        candidates.append(name)
        generated += 1

    return candidates


def append_free(username: str) -> None:
    """Дописывает свободное имя в файл сразу, чтобы не потерять при остановке."""
    with open(FREE_FILE, "a", encoding="utf-8") as f:
        f.write(username + "\n")


def save_progress(
    stats: dict,
    next_index: int,
    candidates: list[str],
    free_list: list[str],
    reason: str,
) -> None:
    """Сохраняет состояние проверки в progress.json."""
    data = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "stopped_reason": reason,
        "checked": stats["checked"],
        "free": stats["free"],
        "occupied": stats["occupied"],
        "invalid": stats["invalid"],
        "errors": stats["errors"],
        "next_index": next_index,
        "remaining": candidates[next_index:],
        "free_usernames": free_list,
    }
    Path(PROGRESS_FILE).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


async def check_username(client: TelegramClient, username: str) -> tuple[str, str]:
    """
    Проверяет одно имя.

    Возвращает (статус, пояснение), где статус — одно из:
    free, occupied, invalid, purchase, error.
    Исключение FloodWaitError не перехватывается: его обрабатывает main(),
    чтобы корректно остановиться и сохранить прогресс.
    """
    try:
        # account.checkUsername: True — свободен, False — занят.
        result = await client(
            functions.account.CheckUsernameRequest(username=username)
        )
    except errors.UsernameOccupiedError:
        # Часть версий API сообщает о занятости исключением, а не False.
        return "occupied", ""
    except errors.UsernameInvalidError:
        return "invalid", ""
    except errors.FloodWaitError:
        # Пробрасываем наверх: никаких обходов и ожиданий внутри проверки.
        raise
    except errors.RPCError as exc:
        return "error", str(exc)

    # В новых слоях API для недоступного, но продающегося имени приходит
    # объект usernamePurchaseAvailable вместо False.
    purchase_flag = getattr(result, "available_for_purchase", None)
    if purchase_flag is not None:
        if purchase_flag:
            return "purchase", getattr(result, "purchase_url", "") or ""
        return "occupied", ""

    return ("free", "") if result is True else ("occupied", "")


def print_stats(stats: dict, reason: str) -> None:
    """Итоговая статистика."""
    print("=" * 52)
    print("ИТОГОВАЯ СТАТИСТИКА")
    print(f"  Причина остановки : {reason}")
    print(f"  Проверено         : {stats['checked']}")
    print(f"  Свободно          : {stats['free']}")
    print(f"  Занято            : {stats['occupied']}")
    print(f"  Недопустимо       : {stats['invalid']}")
    print(f"  Ошибок            : {stats['errors']}")
    if stats["purchase"]:
        print(f"  Занято, за покупку: {stats['purchase']}")
    if stats["free"]:
        print(f"  Свободные имена   : см. {FREE_FILE}")
    print("=" * 52)


async def authorize(client: TelegramClient) -> None:
    """Вход в аккаунт, если сессия отсутствует или не авторизована."""
    await client.connect()
    if await client.is_user_authorized():
        return

    print("Сессия не авторизована — выполняется вход в аккаунт.")
    phone = input("Номер телефона в международном формате (+79001234567): ").strip()

    # Отправляем код в приложение Telegram.
    await client.send_code_request(phone)
    code = input("Код из Telegram: ").strip()

    try:
        await client.sign_in(phone=phone, code=code)
    except errors.SessionPasswordNeededError:
        # У аккаунта включена двухэтапная аутентификация.
        password = getpass.getpass("Пароль двухэтапной аутентификации: ")
        await client.sign_in(password=password)

    print("Вход выполнен, файл сессии сохранён.")


async def main() -> None:
    if API_ID == 0 or not API_HASH:
        print("Заполните API_ID и API_HASH в начале скрипта (см. инструкцию).")
        return

    candidates = build_candidates()
    if not candidates:
        print("Нет кандидатов: задайте MANUAL_USERNAMES или GENERATE_COUNT.")
        return
    print(f"Кандидатов к проверке: {len(candidates)}. "
          f"Пауза между проверками: {DELAY_SECONDS:g} с.")

    stats = {"checked": 0, "free": 0, "occupied": 0,
             "invalid": 0, "errors": 0, "purchase": 0}
    free_list: list[str] = []
    next_index = 0
    reason = "проверка завершена"

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    try:
        # Требование 7: при отсутствии авторизованной сессии просим номер и код.
        await authorize(client)

        me = await client.get_me()
        print(f"Подключено: id={me.id}, @{me.username or 'без юзернейма'}\n")

        for index, username in enumerate(candidates):
            next_index = index
            try:
                status, extra = await check_username(client, username)
            except errors.FloodWaitError as exc:
                # Требование 3: немедленно останавливаемся, прогресс сохраняем.
                print(f"[FLOODWAIT] {username}: нужно подождать {exc.seconds} с. "
                      f"Останавливаюсь, прогресс сохранён.")
                reason = f"FloodWait {exc.seconds} с (лимит Telegram)"
                break

            stats["checked"] += 1
            if status == "free":
                stats["free"] += 1
                free_list.append(username)
                append_free(username)                                  # сразу в файл
                print(f"[СВОБОДЕН]    {username}")
            elif status == "occupied":
                stats["occupied"] += 1
                print(f"[ЗАНЯТ]       {username}")
            elif status == "invalid":
                stats["invalid"] += 1
                print(f"[НЕДОПУСТИМ]  {username}")
            elif status == "purchase":
                stats["purchase"] += 1
                print(f"[ЗАНЯТ/ПРОДАЖА] {username} ({extra})")
            else:
                stats["errors"] += 1
                print(f"[ОШИБКА]      {username}: {extra}")

            next_index = index + 1
            # Требование 4: пауза перед следующей проверкой.
            await asyncio.sleep(DELAY_SECONDS)
    except KeyboardInterrupt:
        reason = "прервано пользователем (Ctrl+C)"
        print("\nПрервано пользователем, сохраняю прогресс.")
    finally:
        # Сохраняем прогресс и печатаем статистику при любом выходе.
        save_progress(stats, next_index, candidates, free_list, reason)
        print_stats(stats, reason)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
