"""
Поиск свободных и «ценных» юзернеймов в Telegram через Telethon.

Как это работает
----------------
Скрипт не перебирает случайный мусор, а сначала оценивает каждое имя и
проверяет только те, что имеют смысл: короткие, из одних букв, произносимые,
похожие на слово. Всё в одном потоке, с паузой между запросами, без прокси и
без обхода FloodWait.

Что считается «ценным»
----------------------
Оценка 0–100 складывается из:
  + длина: 5 символов — 40 баллов, 6 — 30, 7 — 20, дальше меньше;
  + только буквы без цифр (+15);
  + чередование гласных и согласных, то есть произносимость (+20);
  + нормальная доля гласных (+10);
  - непроизносимые скопления согласных, тройные повторы, редкие q/x/z/j.

Классы: A — 70+, B — 55+, C — 40+, D — ниже.
Имена класса A и B дополнительно пишутся в valuable_usernames.txt с оценкой.
Порог MIN_SCORE отсекает заведомо дешёвые варианты и экономит лимит запросов.

Самый ценный источник — реальные слова: положите в words.txt английские слова
по одному в строке (5–10 букв). Свободное словарное слово почти всегда дороже
любой случайной комбинации. Файл необязательный.

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
   - GENERATE_COUNT — сколько имён подобрать автоматически;
   - words.txt — необязательный словарь, из него берутся самые ценные кандидаты.

5. Запуск:

       python username_checker.py

   Результат: free_usernames.txt (все свободные), valuable_usernames.txt
   (свободные с оценкой), progress.json (состояние на момент остановки).
"""

import asyncio
import getpass
import json
import random
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
# Проверяются всегда, даже если оценка ниже MIN_SCORE.
MANUAL_USERNAMES = [
    # "пример", "mynick2026",
]

# Сколько имён подобрать автоматически (0 — только ручной список).
GENERATE_COUNT = 60

# Ограничения длины автоматически подобранных имён.
MIN_LEN = 5
MAX_LEN = 10

# Порог оценки: имена ниже него не проверяются (ручные — проверяются).
# Цифры в именах сильно снижают оценку, поэтому при ALLOW_DIGITS = True
# разумно опустить порог до 30–35.
MIN_SCORE = 45

# Порог для записи в valuable_usernames.txt (A и B классы).
VALUE_SCORE = 55

# Разрешать цифры в автоматически генерируемых именах.
ALLOW_DIGITS = False

# Словарь слов (необязательный). Одно слово в строке, только a-z, 5–10 букв.
WORDS_FILE = "words.txt"

# Пауза между проверками, секунды. Меньше делать не стоит: риск блокировки.
DELAY_SECONDS = 3.0

# Файлы результата.
FREE_FILE = "free_usernames.txt"
VALUABLE_FILE = "valuable_usernames.txt"
PROGRESS_FILE = "progress.json"

# ==================================================================

# Буквы для «человеческих» имён: q, x, j, w встречаются редко и портят читаемость.
ONSETS = [
    "", "b", "c", "d", "f", "g", "h", "k", "l", "m", "n", "p", "r", "s", "t", "v", "z",
    "bl", "br", "cl", "cr", "dr", "fl", "gr", "pl", "pr", "sk", "sl", "sp", "st", "tr",
    "ch", "sh", "th",
]
NUCLEI = ["a", "e", "i", "o", "u", "ai", "ea", "ee", "oo", "ou", "oa", "ie", "y"]
CODAS = ["", "", "", "", "n", "r", "s", "t", "l", "m", "k", "d",
         "nd", "nt", "st", "ck", "ll", "ss"]

VOWELS = set("aeiouy")
RARE = set("qxzj")
DIGITS = set("0123456789")

# Бонус за длину: короткое имя найти труднее и оно дороже.
LENGTH_BONUS = {5: 40, 6: 30, 7: 20, 8: 12, 9: 6, 10: 3}


def score_username(name: str) -> int:
    """Оценка «ценности» имени от 0 до 100 (эвристика, не рыночная цена)."""
    score = LENGTH_BONUS.get(len(name), 0)
    letters = [c for c in name if c.isalpha()]
    digits = [c for c in name if c in DIGITS]

    # Имя из одних букв читается и стоит заметно дороже.
    if not digits:
        score += 15
    else:
        score -= 15
        if name[0] in DIGITS:
            score -= 10
        if len(digits) > 2:
            score -= 10

    # Произносимость: чем чаще чередуются гласные и согласные, тем лучше.
    max_run = run = 1
    for i in range(1, len(name)):
        same = (name[i] in VOWELS) == (name[i - 1] in VOWELS)
        run = run + 1 if same else 1
        max_run = max(max_run, run)
    if max_run <= 1:
        score += 20
    elif max_run == 2:
        score += 8
    elif max_run >= 4:
        score -= 8 * (max_run - 3)

    # Редкие буквы и тройные повторы («aaa») удешевляют имя.
    score -= 3 * sum(1 for c in name if c in RARE)
    for i in range(len(name) - 2):
        if name[i] == name[i + 1] == name[i + 2]:
            score -= 10
            break

    # Доля гласных: слишком мало — нечитаемо, слишком много — выглядит странно.
    if letters:
        vratio = sum(1 for c in name if c in VOWELS) / len(letters)
        if 0.3 <= vratio <= 0.6:
            score += 10
        elif vratio < 0.2 or vratio > 0.75:
            score -= 8

    return max(0, min(100, score))


def value_class(score: int) -> str:
    """Буквенный класс ценности для вывода и файла."""
    if score >= 70:
        return "A"
    if score >= VALUE_SCORE:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def generate_pronounceable() -> str:
    """Имя из 2–4 слогов, похожее на слово: brondel, straveck, melisa."""
    for _ in range(200):
        parts = []
        for i in range(random.randint(2, 4)):
            # Пустое начало допустимо только у первого слога, иначе «aeiou» подряд.
            onset = "" if (i == 0 and random.random() < 0.4) else random.choice(ONSETS)
            parts.append(onset + random.choice(NUCLEI) + random.choice(CODAS))
        name = "".join(parts)

        # Цифра в конце — только если явно разрешено.
        if ALLOW_DIGITS and random.random() < 0.25:
            name += str(random.randint(0, 9))

        if MIN_LEN <= len(name) <= MAX_LEN and not name[0].isdigit():
            return name
    # Страховка: не даём бесконечный цикл, если настройки противоречивы.
    return "brondel"[:MAX_LEN]


def load_words() -> list[str]:
    """Читает words.txt, если он есть рядом со скриптом."""
    path = Path(WORDS_FILE)
    if not path.is_file():
        return []

    words: list[str] = []
    seen: set[str] = set()
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        word = raw.strip().lower()
        if not word or word in seen:
            continue
        if not word.isalpha() or not word.isascii():
            continue
        if not (MIN_LEN <= len(word) <= MAX_LEN):
            continue
        seen.add(word)
        words.append(word)
    return words


def build_candidates() -> list[tuple[str, int]]:
    """
    Возвращает список пар (имя, оценка).

    Порядок: сначала ручные имена, затем автоматические — от самых ценных
    к менее ценным, чтобы при раннем FloodWait успеть проверить лучшее.
    """
    manual: list[str] = []
    seen: set[str] = set()

    # Ручной список: убираем @, пробелы, приводим к нижнему регистру.
    for raw in MANUAL_USERNAMES:
        name = raw.strip().lstrip("@").lower()
        if not name or name in seen:
            continue
        seen.add(name)
        manual.append(name)

    auto: list[str] = []

    # 1) Слова из словаря — самый ценный источник.
    words = load_words()
    if words:
        random.shuffle(words)
        for word in words[:GENERATE_COUNT]:
            if word in seen:
                continue
            seen.add(word)
            auto.append(word)
        print(f"Из {WORDS_FILE} взято кандидатов: {len(auto)}")

    # 2) Произносимые имена, отобранные по минимальной оценке.
    attempts = 0
    attempts_limit = GENERATE_COUNT * 200 + 1000
    while len(auto) < GENERATE_COUNT and attempts < attempts_limit:
        attempts += 1
        name = generate_pronounceable()
        if name in seen or score_username(name) < MIN_SCORE:
            continue
        seen.add(name)
        auto.append(name)

    # Сортируем автоматические имена по убыванию оценки.
    auto.sort(key=score_username, reverse=True)

    return [(name, score_username(name)) for name in manual + auto]


def append_free(username: str) -> None:
    """Дописывает свободное имя в файл сразу, чтобы не потерять при остановке."""
    with open(FREE_FILE, "a", encoding="utf-8") as f:
        f.write(username + "\n")


def append_valuable(username: str, score: int) -> None:
    """Дописывает ценное свободное имя с оценкой: «имя<TAB>оценка»."""
    with open(VALUABLE_FILE, "a", encoding="utf-8") as f:
        f.write(f"{username}\t{score}\n")


def save_progress(
    stats: dict,
    next_index: int,
    plan: list[tuple[str, int]],
    found: list[dict],
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
        "remaining": [name for name, _ in plan[next_index:]],
        "found": found,
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


def print_stats(stats: dict, reason: str, best: dict | None) -> None:
    """Итоговая статистика."""
    print("=" * 56)
    print("ИТОГОВАЯ СТАТИСТИКА")
    print(f"  Причина остановки : {reason}")
    print(f"  Проверено         : {stats['checked']}")
    print(f"  Свободно          : {stats['free']}")
    print(f"  Занято            : {stats['occupied']}")
    print(f"  Недопустимо       : {stats['invalid']}")
    print(f"  Ошибок            : {stats['errors']}")
    if stats["purchase"]:
        print(f"  Занято, за покупку: {stats['purchase']}")
    if best:
        print(f"  Лучшая находка    : {best['name']} "
              f"(оценка {best['score']}, класс {value_class(best['score'])})")
    if stats["free"]:
        print(f"  Свободные имена   : {FREE_FILE} (ценные — {VALUABLE_FILE})")
    print("=" * 56)


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

    plan = build_candidates()
    if not plan:
        print("Нет кандидатов: задайте MANUAL_USERNAMES, GENERATE_COUNT или words.txt.")
        return

    avg = sum(score for _, score in plan) / len(plan)
    print(f"Кандидатов к проверке: {len(plan)} (средняя оценка {avg:.0f}). "
          f"Пауза между запросами: {DELAY_SECONDS:g} с.")
    if plan:
        top = plan[0]
        print(f"Первый в очереди: {top[0]} — оценка {top[1]}.\n")

    stats = {"checked": 0, "free": 0, "occupied": 0,
             "invalid": 0, "errors": 0, "purchase": 0}
    found: list[dict] = []
    best: dict | None = None
    next_index = 0
    reason = "проверка завершена"

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    try:
        # Требование 7: при отсутствии авторизованной сессии просим номер и код.
        await authorize(client)

        me = await client.get_me()
        print(f"Подключено: id={me.id}, @{me.username or 'без юзернейма'}\n")

        for index, (username, score) in enumerate(plan):
            next_index = index
            try:
                status, extra = await check_username(client, username)
            except errors.FloodWaitError as exc:
                # Немедленно останавливаемся, прогресс сохраняем.
                print(f"[FLOODWAIT] {username}: нужно подождать {exc.seconds} с. "
                      f"Останавливаюсь, прогресс сохранён.")
                reason = f"FloodWait {exc.seconds} с (лимит Telegram)"
                break

            stats["checked"] += 1
            mark = f"{score:>3} ({value_class(score)})"

            if status == "free":
                stats["free"] += 1
                free_info = {"name": username, "score": score}
                found.append(free_info)
                append_free(username)                                   # сразу в файл
                if score >= VALUE_SCORE:
                    append_valuable(username, score)
                if best is None or score > best["score"]:
                    best = free_info
                print(f"[СВОБОДЕН]    {username:<12} оценка {mark}")
            elif status == "occupied":
                stats["occupied"] += 1
                print(f"[ЗАНЯТ]       {username:<12} оценка {mark}")
            elif status == "invalid":
                stats["invalid"] += 1
                print(f"[НЕДОПУСТИМ]  {username:<12} оценка {mark}")
            elif status == "purchase":
                stats["purchase"] += 1
                print(f"[ЗАНЯТ/ПРОДАЖА] {username:<10} оценка {mark} ({extra})")
            else:
                stats["errors"] += 1
                print(f"[ОШИБКА]      {username:<12} {extra}")

            next_index = index + 1
            # Пауза перед следующей проверкой.
            await asyncio.sleep(DELAY_SECONDS)
    except KeyboardInterrupt:
        reason = "прервано пользователем (Ctrl+C)"
        print("\nПрервано пользователем, сохраняю прогресс.")
    finally:
        # Сохраняем прогресс и печатаем статистику при любом выходе.
        save_progress(stats, next_index, plan, found, reason)
        print_stats(stats, reason, best)
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
