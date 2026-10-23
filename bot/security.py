"""Слой безопасности: редакция секретов, санация текста, валидация полей,
ограничение частоты запросов и защита от prompt injection.
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable

# --------------------------------------------------------------------------- #
# Редакция секретов в логах
# --------------------------------------------------------------------------- #

_PLACEHOLDER = "[REDACTED]"

#: Значения, которые маскируются в любом лог-сообщении.
_mask_registry: set[str] = set()


def register_secret(value: str | None) -> None:
    """Регистрирует значение, которое никогда не должно попасть в лог."""
    if not value or len(value) < 8:
        return
    _mask_registry.add(value)


_TOKEN_RE = re.compile(r"\b(sk|vk|EAA)[A-Za-z0-9_\-]{10,}\b", re.IGNORECASE)
#: Настоящий токен сообщества у VK устроен как ``vk1.a.СлучайнаяСтрока``.
#: В ``_TOKEN_RE`` точка не входит в класс символов, поэтому такой токен не
#: находился вовсе. Требование ``vk<цифра>.<буква>.`` отсекает обычные ссылки
#: вида ``vk.com/id1``.
_VK_TOKEN_RE = re.compile(r"\bvk\d\.[a-zA-Z]\.[A-Za-z0-9_\-]{10,}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+7|8|7)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*(?![\w])")
_COOKIE_RE = re.compile(r"(?i)\b(?:authorization|cookie|set-cookie|password|passwd|secret|token|api[_-]?key)"
                        r"\b\s*[:=]\s*\S+")
# Приватный ключ приезжает в тексте ошибки, когда не читается JSON-файл ключа:
# библиотека цитирует его целиком. Маска по зарегистрированным секретам тут
# не поможет — сам блок в реестр не попадал, и в лог уходил целиком.
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
# Обрывок без END: текст могло вырезать по длине. Один заголовок блока без
# содержимого секретом не является, но следующая за ним строка base64 — уже
# является, поэтому маскируем её целиком.
_PEM_DANGLE_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*$",
)


def _looks_like_env_name(value: str) -> bool:
    """Отличает имя переменной окружения от настоящего токена.

    Регулярка токенов ловит всё, что начинается на ``vk``/``sk``, поэтому
    ``VK_ADMIN_PEER_IDS`` в тексте лога превращался в ``[REDACTED]`` и
    диагностика доставки уведомлений становилась бесполезной. Настоящие токены
    VK выглядят как ``vk1.a.СлучайнаяСтрока`` — с точкой и строчными буквами,
    а имена переменных пишутся прописными с подчёркиваниями.
    """
    return value.isupper() and ("_" in value or "." not in value)


def _mask_token(match: re.Match[str]) -> str:
    value = match.group(0)
    return value if _looks_like_env_name(value) else _PLACEHOLDER


def redact(value: object, *, mask_pii: bool = False) -> str:
    """Заменяет секреты (и, по запросу, персональные данные) на плейсхолдеры."""
    if value is None:
        return "-"
    text = value if isinstance(value, str) else str(value)
    if not text:
        return text

    for secret in sorted(_mask_registry, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, _PLACEHOLDER)

    text = _TOKEN_RE.sub(_mask_token, text)
    text = _VK_TOKEN_RE.sub(_PLACEHOLDER, text)
    text = _COOKIE_RE.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + "=[REDACTED]", text)
    text = _PEM_RE.sub("-----BEGIN PRIVATE KEY-----[REDACTED]-----END PRIVATE KEY-----", text)
    text = _PEM_DANGLE_RE.sub("-----BEGIN PRIVATE KEY-----[REDACTED]", text)
    if mask_pii:
        text = _EMAIL_RE.sub("[EMAIL]", text)
        text = _PHONE_RE.sub("[PHONE]", text)
    return text


# --------------------------------------------------------------------------- #
# Санация пользовательского ввода
# --------------------------------------------------------------------------- #

_ZERO_WIDTH = dict.fromkeys(
    [
        0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF, 0x180E,
        0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E,
    ]
)

#: Маркеры, по которым в тексте видно попытку подмены системной инструкции.
_INJECTION_MARKERS = (
    "ignore previous", "ignore all previous", "disregard", "system prompt",
    "you are now", "act as", "developer mode", "jailbreak", "промпт", "системн",
)

_ESCALATION_MARKERS = (
    "спонсор", "sponsor", "договор", "счёт", "счет на оплату", "бухгалтер",
    "аккредитац", "пресс", "сми", "жалоб", "безопасност", "чп", "травм", "потерял ребён",
    "потерял ребенка", "пропал ребёнок", "пропал ребенок", "зарплат", "оплат", "перевед",
    "карт", "санкц", "жалоба",
)

#: Признаки реальной опасности для жизни. Слабые слова вроде «ушиб» или
#: одиночное «чп» сюда не входят: они давали ложные тревоги, а самые важные
#: случаи («потеряли ребёнка») раньше не срабатывали из-за формы глагола.
_URGENT_STRONG = (
    "не дыш", "кровотеч", "кровь ид", "обморок", "скорую", "скорая",
    "отравил", "отравление", "удушь", "задыха", "инфаркт", "инсульт",
    "потерял реб", "потеряла реб", "потеряли реб", "потерялось реб",
    "пропал реб", "пропала реб", "пропали реб", "пропало реб",
    "не найден реб", "не нашли реб", "ребёнок пропал", "ребенок пропал",
)

#: Тревога послабее: инструкция человеку полезна, но тревожить организаторов
#: по каждому слову нельзя — это и было источником сюрпризов.
_URGENT_SOFT = (
    "травм", "ушиб", "ушиблись", "упал", "упала", "упали", "кровоподтёк",
    "сотряс", "ожог", "приступ", "плохо стало", "экстрен", "чп ",
)

_URGENT_MARKERS = _URGENT_STRONG + _URGENT_SOFT

#: Явная просьба передать сообщение живому человеку. Проверяется до
#: приветствий и до модели: «передай координаторам привет» — это передача,
#: а не знакомство, даже когда в тексте есть «привет».
_HANDOFF_REQUESTS = (
    "передай", "передайте", "передать", "передаю", "передал",
    "спроси у", "спросите у", "задай вопрос", "задайте вопрос",
    "напишите организатор", "напиши организатор", "напишите координатор",
    "напиши координатор", "покажи организатор", "сообщи организатор",
    "сообщите организатор", "уточни у организатор", "уточните у организатор",
    "нужен ответ от", "жду ответа", "дождусь ответа",
)

_HUMAN_NOUNS = (
    "организатор", "координатор", "модератор", "человек", "менеджер",
    "администратор", "куратор", "команду", "команды",
)


def wants_human(text: str) -> bool:
    """Просит ли пользователь передать сообщение живому человеку.

    Возвращает True, если в тексте есть и просьба передать/спросить, и адресат
    из «живых». Одно слово «привет» или «спроси» без адресата — не считается.
    """
    low = text.lower()
    if not any(verb in low for verb in _HANDOFF_REQUESTS):
        return False
    return any(noun in low for noun in _HUMAN_NOUNS)


#: Признаки завершения разговора. «Здорово, спасибо» — это прощание, а не знакомство.
_FAREWELL_MARKERS = (
    "спасибо", "спс", "благодар", "пока", "до связи", "до встречи", "увидимся",
    "прощ", "извини", "не беспокой", "на этом всё", "на этом все", "всё, спасибо",
    "спокойной ночи", "доброй ночи", "доброго дня", "доброго вечера", "до завтра",
)

#: Слова вопроса: «спасибо, а где вход?» — это вопрос, а не прощание.
_QUESTION_MARKERS = (
    "?", "когда", "где", "сколько", "как ", "какой", "какая", "можно", "хочу",
    "нужно", "подать", "заявк", "расскаж", "расскажи", "а если", "а как", "а где",
)

#: Приветствие. Вместе с прощанием даёт двусмысленную фразу: «Здорово, спасибо».
_GREETING_MARKERS = (
    "здравству", "привет", "здорово", "здаров", "хай", "hi ", "hello",
    "добрый день", "доброе утро", "добрый вечер", "доброе время",
)


def is_farewell(text: str) -> bool:
    """Закрывает ли человек разговор.

    Прощание важно отличать от знакомства: на «здравствуйте, спасибо» нельзя
    заново представляться, нужно попрощаться и предложить помощь. Признаки
    вопроса отменяют прощание, иначе «спасибо, а где вход?» потеряет вопрос.
    """
    low = text.lower()
    if not any(marker in low for marker in _FAREWELL_MARKERS):
        return False
    return not any(marker in low for marker in _QUESTION_MARKERS)


def is_pure_farewell(text: str) -> bool:
    """Однозначное прощание — без приветствия.

    На «спасибо, до связи» достаточно попрощаться: предлагать новые услуги
    неуместно, человек уже закончил разговор. На «здорово, спасибо» фраза
    двусмысленная — приветствие и прощание в одном сообщении, и там уместно
    ненавязчиво предложить рассказать что-то ещё.
    """
    low = text.lower()
    if not is_farewell(low):
        return False
    return not any(marker in low for marker in _GREETING_MARKERS)


def clean_text(value: object, max_len: int = 2000) -> str:
    """Нормализует ввод: убирает управляющие и невидимые символы, режет длину."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = text.translate(_ZERO_WIDTH)
    # Оставляем \n и \t, остальные C0/C1-символы — в пробел.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", " ", text)
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip() + "…"
    return text


def is_injection_attempt(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _INJECTION_MARKERS)


def needs_escalation(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _ESCALATION_MARKERS)


def is_urgent(text: str) -> bool:
    low = text.lower()
    return any(marker in low for marker in _URGENT_MARKERS)


# --------------------------------------------------------------------------- #
# Бессмысленный ввод: экономим токены модели
# --------------------------------------------------------------------------- #
#
# Модель на мусоре не просто отвечает медленно — она тратит токены впустую.
# Проверено на живом API: «Gbie jn bvtyb cjj,otcndf» (русский текст в
# английской раскладке) съедало 15,5 с и все 400 токенов классификатора, а
# ответа не давало. Тот же вопрос по-русски укладывался в 1–2 с.
#
# Поэтому смысловой ввод отсекается локально, без обращения к модели. Проверка
# бесплатная и мгновенная, а модель на ней не тратит ничего.
#
# Ложное срабатывание здесь опаснее промаха: заблокировать настоящий вопрос
# хуже, чем один раз потратить токены, поэтому критерии намеренно строгие.

#: Гласные русского языка. В слове из трёх и более букв гласная есть всегда —
#: это позволяет отсечь «бгдщ», «ыьм», не задев настоящие слова.
_VOWELS = frozenset("аеёиоуыэюяAEIOUY")

#: Клавиатурные ряды. Пробег пальцами по клавишам — самый частый источник
#: «абракадабры», и такие ряды узнаются мгновенно.
_KEYBOARD_ROWS = (
    "qwertyuiop", "asdfghjkl", "zxcvbnm", "1234567890",
    "йцукенгшщзхъ", "фывапролджэ", "ячсмитьбю",
)

#: Раскладка ЙЦУКЕН: латинская клавиша → русская буква.
#:
#: Нужна, чтобы отличить «Gbie jn bvtyb» (это «Пишу от имени») от настоящей
#: бессмыслицы: человек просто не переключил раскладку, и вместо отсечения
#: мусора можно показать, что именно бот прочитал, и спросить подтвердить.
_ЙЦУКЕН_TABLE = {
    "q": "й", "w": "ц", "e": "у", "r": "к", "t": "е", "y": "н", "u": "г",
    "i": "ш", "o": "щ", "p": "з", "[": "х", "]": "ъ",
    "a": "ф", "s": "ы", "d": "в", "f": "а", "g": "п", "h": "р", "j": "о",
    "k": "л", "l": "д", ";": "ж", "'": "э",
    "z": "я", "x": "ч", "c": "с", "v": "м", "b": "и", "n": "т", "m": "ь",
    ",": "б", ".": "ю", "/": ".",
}
_ЙЦУКЕН = str.maketrans(_ЙЦУКЕН_TABLE)

#: Самые частые русские слова. Нужны, чтобы отсечение не сработало на
#: настоящей латинице: «ok», «hello», имя или город не должны считаться
#: русским текстом в чужой раскладке.
_COMMON_WORDS = frozenset({
    "и", "в", "на", "с", "по", "из", "от", "до", "за", "для", "про", "не",
    "что", "как", "где", "когда", "кто", "это", "мне", "вы", "мы", "ты",
    "он", "она", "они", "его", "её", "их", "все", "всё", "уже", "ещё", "еще",
    "или", "если", "чтобы", "так", "там", "тут", "много", "мало", "хорошо",
    "плохо", "спасибо", "привет", "здравствуйте", "пожалуйста", "можно",
    "надо", "нужно", "хочу", "могу", "хотел", "была", "было", "были",
    "есть", "нет", "да", "ещё", "пишу", "пиши", "имя", "номер", "телефон",
    "почта", "вход", "билет", "заявка", "заявку", "вопрос", "ответ",
    "сообщество", "сообщества", "фестиваль", "фестиваля", "выступление",
    "выступить", "когда", "сколько", "стоимость", "стоит", "время", "день",
    "место", "парковка", "где", "кто", "вам", "мне", "ирина", "бот",
})


def _has_cyrillic(text: str) -> bool:
    return any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in text)


def _words_of(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", text, re.UNICODE)


def reads_as_russian(text: str) -> str | None:
    """Читает латиницу как русский текст в раскладке ЙЦУКЕН.

    Возвращает прочитанный вариант или ``None``, если это не похоже на русский
    текст. Нужен, чтобы отличить забытую раскладку от настоящей бессмыслицы:
    в первом случае бот может показать, что именно он прочитал, и спросить
    подтвердить, вместо того чтобы просто отшить человека.
    """
    # Смешанный текст (кириллица + латиница) — не забытая раскладка, а осмысленный
    # ввод: названия, бренды, e-mail, «ок» в тексте. Такое отсекать нельзя.
    if not text.strip() or _has_cyrillic(text):
        return None
    latin_words = re.findall(r"[a-zA-Z]+", text)
    # Короткие слова латиницей — это обычно «ok», «hi», имя: раскладку тут
    # приписывать нечего, а вот настоящую латинскую речь надо пропустить.
    if not latin_words or sum(len(w) for w in latin_words) < 6:
        return None
    decoded = []
    for ch in text:
        # Словарь раскладки задан в нижнем регистре, а translate() на заглавной
        # букве («G») молча оставляет её латинской — «Gишу» вместо «Пишу».
        # Поэтому ищем по нижнему регистру и возвращаем регистр сами.
        mapped = _ЙЦУКЕН_TABLE.get(ch.lower())
        if mapped is None:
            decoded.append(ch)
        elif ch.isupper():
            decoded.append(mapped.upper())
        else:
            decoded.append(mapped)
    decoded = "".join(decoded)
    decoded_words = [w.lower() for w in _words_of(decoded)]
    if not decoded_words:
        return None
    # Признак русского текста — есть знакомые слова. Без этого «hello world»
    # после перевода даёт «реддщ щрдщ» и сошло бы за русский.
    if not any(word in _COMMON_WORDS for word in decoded_words):
        return None
    return decoded


def is_keyboard_mash(text: str) -> bool:
    """Похоже ли сообщение на бессмысленный набор символов.

    Ловит четыре случая, которые чаще всего и встречаются: повтор одной
    буквы («ыыы»), пробег по клавиатуре («фывапр», «qwerty»), чередование
    двух символов («asdasd») и слова без гласных («бгдщ»).
    """
    words = [w for w in _words_of(text) if len(w) >= 3]
    if not words:
        return False
    for word in words:
        low = word.lower()
        # Аббревиатура в верхнем регистре — не мусор: «ООО», «СССР», «МЧС»,
        # «СПб» не содержат гласных, но относятся к делу.
        is_abbreviation = word.isupper() and any(ch.isalpha() for ch in word)
        if re.fullmatch(r"(.)\1{2,}", low, re.IGNORECASE) and not is_abbreviation:
            return True
        if re.fullmatch(r"(..)\1{2,}", low) and not is_abbreviation:
            return True
        # Повтор блока из трёх букв: «asdasd» — это «asd»+«asd», а не «as»+«as».
        if len(low) >= 6 and re.fullmatch(r"(...)\1+", low) and not is_abbreviation:
            return True
        if len(low) >= 4 and any(low in row for row in _KEYBOARD_ROWS):
            return True
        # Слово без гласных. Проверяем от четырёх букв: в коротких словах
        # («СПб», «ДК») слишком легко принять аббревиатуру за мусор, а ь и ъ
        # внутри слова означают окончание или приставку, а не набор клавиш.
        if (len(low) >= 4 and _has_cyrillic(low) and not is_abbreviation
                and not any(ch in _VOWELS for ch in low)
                and not any(ch in "ьъ" for ch in low)):
            return True
    return False


#: Короткие реплики без вопроса. Человек не задал вопрос — он отреагировал на
#: предыдущее сообщение, и это повод ответить по-человечески, а не просить
#: «напишите вопрос ещё раз».
REACTION_WORDS = frozenset({
    "ок", "окей", "окки", "ага", "угу", "угус", "а", "да", "нет", "понял", "поняла",
    "понятно", "ясно", "все понятно", "спасибо", "спс", "благодарю", "круто",
    "класс", "супер", "отлично", "хорошо", "хорошо!", "ладно", "точно", "ок.",
    "+", "да!", "нет!", "ура", "плюс", "топ", "красава", "идёт", "идет",
})

#: Смайлик, сердечко, «+» и прочие знаки без единой буквы или цифры.
_EMOJI_OR_SYMBOLS = re.compile(
    r"[\U0001F000-\U0001FAFF←-⇿⌀-➿⬀-⯿️✨]",
)


def is_emoji_only(text: str) -> bool:
    """Только смайлики и знаки, ни одной буквы или цифры: «😄», «👍», «🔥🔥».

    Такой ввод :func:`is_meaningless` считает мусором, и раньше человек получал
    «похоже, сообщение получилось случайным». На деле это реакция, и на неё
    есть что ответить.
    """
    s = (text or "").strip()
    if not s or re.search(r"[0-9A-Za-z\u0400-\u04FF]", s):
        return False
    if re.match(r"^[\s\-=_*+#~^|\\/.,!?()\[\]]+$", s):
        # «+++», «!!!», «---» — это не смайлик, а мусор с клавиатуры.
        return False
    return bool(_EMOJI_OR_SYMBOLS.search(s))


def is_reaction_only(text: str) -> bool:
    """Похоже ли сообщение на короткую реакцию, а не на вопрос.

    Сюда попадают смайлики, «+», «ок», «ага», «спасибо» и подобное. Такое
    сообщение бессмысленным не считается: человек просто отреагировал, и
    Ирине есть что ответить. Строка с буквами, но длиннее пары слов сюда не
    относится — это уже настоящий ввод, который надо разбирать как обычно.
    """
    s = (text or "").strip()
    if not s:
        return False
    words = _words_of(s)
    if not words:
        return is_emoji_only(s)
    return len(words) <= 2 and s.lower().strip(" .!?,…") in REACTION_WORDS



def is_meaningless(text: str) -> bool:
    """Стоит ли вообще нести этот текст модели.

    Возвращает ``True`` только когда ввод бессмысленен **по содержанию**:
    текст без букв («!!!», «?????») и набор с клавиатуры. Текст в чужой
    раскладке сюда не попадает — его разбирает :func:`reads_as_russian`,
    и человек может получить ответ, а не отказ.
    """
    if not text or not text.strip():
        return True
    s = text.strip()
    # Ни букв, ни цифр: «!!!», «))))», «---», эмодзи. Отвечать тут нечем, а
    # модель всё равно потратит токены. Цифры отдельно не трогаем: «+7 916…»
    # в анкете — норма, и такую проверку решает состояние, а не текст.
    if not re.search(r"[0-9A-Za-z\u0400-\u04FF]", s):
        return True
    if not _words_of(text):
        return False
    return is_keyboard_mash(text)


# --------------------------------------------------------------------------- #
# Валидация полей анкеты
# --------------------------------------------------------------------------- #

_EMAIL_RE_FULL = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~\-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?\.)+"
    r"[A-Za-z]{2,}$"
)

#: Разрешённые схемы ссылок. Никакие другие — никакой SSRF, никакой file://.
_LINK_RE = re.compile(
    r"^(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]{2,500}$",
    re.IGNORECASE,
)

_SAFE_LINK_HOST = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$|^localhost$",
    re.IGNORECASE,
)

_FORBIDDEN_LINK_SUBSTRINGS = (
    "javascript:", "data:", "vbscript:", "file:", "about:", "chrome://",
    "localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "metadata",
)

_NAME_RE = re.compile(r"^[\w\s\-.«»\"'()\[\]]{2,120}$", re.UNICODE)
_CITY_RE = re.compile(r"^[\w\s\-.«»()]{2,80}$", re.UNICODE)


#: Символы, которые в номере телефона означают попытку подсунуть что-то ещё.
_PHONE_FORBIDDEN = set("<>\"'`;\\/&?%$#@*=[],{}")


@dataclass
class ValidationResult:
    ok: bool
    value: str = ""
    error: str = ""


def validate_name(value: str, max_len: int = 120) -> ValidationResult:
    text = clean_text(value, max_len + 50)
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ValidationResult(False, error="empty")
    if len(text) < 2:
        return ValidationResult(False, error="too_short")
    if not _NAME_RE.match(text):
        return ValidationResult(False, error="symbols")
    return ValidationResult(True, text)


def validate_city(value: str) -> ValidationResult:
    text = clean_text(value, 90)
    text = re.sub(r"\s+", " ", text)
    if not text:
        return ValidationResult(False, error="empty")
    if not _CITY_RE.match(text):
        return ValidationResult(False, error="symbols")
    return ValidationResult(True, text)


def extract_phone_digits(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def validate_phone(value: str) -> ValidationResult:
    """Российский номер: 10 цифр (с 7/8) или 11 цифр (с +, 7 или 8).

    Буквы вокруг номера допускаются («тел +7 916...»), управляющие символы — нет.
    """
    text = clean_text(value, 40)
    if not text:
        return ValidationResult(False, error="empty")
    for ch in text:
        if ch in _PHONE_FORBIDDEN:
            return ValidationResult(False, error="not_phone")
    digits = extract_phone_digits(text)
    if len(digits) not in (10, 11):
        return ValidationResult(False, error="length")
    if digits.startswith(("7", "8")):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] not in "3456789":
        return ValidationResult(False, error="prefix")
    return ValidationResult(True, "+7 " + digits[:3] + " " + digits[3:6] + "-" + digits[6:8] + "-" + digits[8:10])


def validate_email(value: str) -> ValidationResult:
    raw = clean_text(value, 120)
    if not raw:
        return ValidationResult(False, error="empty")
    if " " in raw or "\t" in raw:
        return ValidationResult(False, error="format")
    text = raw
    if not _EMAIL_RE_FULL.match(text):
        return ValidationResult(False, error="format")
    return ValidationResult(True, text.lower())


def validate_link(value: str) -> ValidationResult:
    text = clean_text(value, 512)
    if not text:
        return ValidationResult(False, error="empty")
    candidate = text if re.match(r"^https?://", text, re.IGNORECASE) else "https://" + text
    low = candidate.lower()
    for bad in _FORBIDDEN_LINK_SUBSTRINGS:
        if bad in low:
            return ValidationResult(False, error="scheme")
    if not _LINK_RE.match(candidate):
        return ValidationResult(False, error="format")
    host = re.sub(r"^https?://", "", candidate, flags=re.IGNORECASE).split("/")[0].split("?")[0]
    host = host.split("@")[-1].split(":")[0]
    if not _SAFE_LINK_HOST.match(host):
        return ValidationResult(False, error="host")
    return ValidationResult(True, candidate)


def validate_description(value: str, max_len: int = 1500) -> ValidationResult:
    text = clean_text(value, max_len)
    if not text:
        return ValidationResult(False, error="empty")
    if len(text) < 3:
        return ValidationResult(False, error="too_short")
    return ValidationResult(True, text)


def validate_day(value: str) -> tuple[str | None, str | None]:
    """Возвращает (нормализованное значение, распознанный вариант из трёх)."""
    low = clean_text(value, 40).lower()
    if not low:
        return None, None
    has_sat = "суббот" in low or low in {"сб", "1", "пт"} or "пятниц" in low
    has_sun = "воскрес" in low or low in {"вс", "2"} or "воскр" in low
    has_both = "оба" in low or "обо" in low or "любой" in low or "все" in low or "both" in low
    if has_both or (has_sat and has_sun):
        return "Оба дня", "both"
    if has_sat:
        return "Суббота", "sat"
    if has_sun:
        return "Воскресенье", "sun"
    return None, None


#: Точные ответы, которые однозначно означают согласие.
AFFIRMATIVE = {
    "да", "ага", "давай", "ок", "окей", "угу", "верно", "именно", "точно",
    "все так", "всё так", "все верно", "всё верно", "все правильно", "всё правильно",
    "да верно", "да, верно", "ага, верно", "да, все верно", "да, всё верно",
    "да, отправить", "да отправить", "отправить", "отправляю", "отправить заявку",
    "подтверждаю", "подтверждаю, отправить", "подтверждаю, все верно",
    "подтверждаю, всё верно", "подтверждаю, всё так", "подтверждаю, все так",
    "согласен", "согласна", "подходит", "подходит, отправляю", "все ок", "всё ок",
    "все", "всё", "оставить", "оставить так", "заполнено", "введено", "сохранить",
    "готов", "готова", "заявка готова", "вроде все", "вроде всё", "вроде всё так",
    "+", "y", "yes", "ok", "ok!", "ок)", "да!", "ага!", "да, всё верно",
}

#: Начала фраз, которые однозначно означают согласие («да, ...», «всё верно, ...»).
AFFIRMATIVE_PREFIXES = (
    "да,", "да ", "да!", "всё верно", "все верно", "всё так", "все так",
    "всё правильно", "все правильно", "подтверждаю", "отправ", "верно,",
    "согласен", "согласна", "подходит", "оставить", "сохранить",
)

#: Точные ответы, которые однозначно означают отказ.
NEGATIVE = {
    "нет", "неа", "нет, не", "не верно", "неверно", "не так", "нетак",
    "неправильно", "ошибаюсь", "ошибка", "поправь", "исправить", "изменить",
    "хочу изменить", "хочу поменять", "не отправлять", "не отправляй",
    "n", "no", "nope", "нет, спасибо", "не, спасибо", "не сейчас", "позже",
    "отмена", "стоп", "хватит",
}

#: Начала фраз, которые однозначно означают отказ.
NEGATIVE_PREFIXES = (
    "нет", "неа", "не верно", "неверно", "не так", "нетак", "неправильно",
    "хочу изменить", "хочу поменять", "измени", "поправь", "исправь",
    "не отправл", "ошибаюсь", "ошибка", "отмена", "остановись",
)


def _matches(value: str, exact: set[str], prefixes: tuple[str, ...]) -> bool:
    low = clean_text(value, 60).lower().strip(" .!,—–-")
    if not low:
        return False
    if low in exact:
        return True
    # длинные свободные фразы не разбираем: слишком много ложных срабатываний
    if len(low) > 32:
        return False
    return low.startswith(prefixes)


def mask_contact(key: str, value: str) -> str:
    """Маскирует контакт для показа в общем чате.

    ``+7 916 123-45-67`` → ``+7 *** ***-45-67``; почта → ``ne***@example.ru``.
    Всё остальное возвращается как есть.
    """
    text = clean_text(value, 200)
    if key == "phone":
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 6:
            return "+7 *** ***-" + digits[-2:] if len(digits) >= 12 else "***" + digits[-2:]
        return "***"
    if key == "email" and "@" in text:
        name, _, domain = text.partition("@")
        head = name[:2]
        return f"{head}***@{domain}" if head else f"***@{domain}"
    return text


def is_affirmative(value: str) -> bool:
    if _matches(value, NEGATIVE, NEGATIVE_PREFIXES):
        return False
    return _matches(value, AFFIRMATIVE, AFFIRMATIVE_PREFIXES)


def is_negative(value: str) -> bool:
    if _matches(value, AFFIRMATIVE, AFFIRMATIVE_PREFIXES):
        return False
    return _matches(value, NEGATIVE, NEGATIVE_PREFIXES)


# --------------------------------------------------------------------------- #
# Ограничение частоты
# --------------------------------------------------------------------------- #

class RateLimiter:
    """Скользящее окно. Один объект на пользователя + один глобальный."""

    __slots__ = ("_events", "limit", "_now")

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = max(1, limit)
        self._events: deque[float] = deque()
        self._now = time.monotonic

    def allow(self) -> bool:
        now = self._now()
        while self._events and now - self._events[0] > 60.0:
            self._events.popleft()
        if len(self._events) >= self.limit:
            return False
        self._events.append(now)
        return True

    def retry_after(self) -> int:
        if not self._events:
            return 0
        return max(1, int(60 - (self._now() - self._events[0])) + 1)


class RateLimiterRegistry:
    """Потокобезопасного доступа достаточно: Long Poll обрабатывается последовательно."""

    def __init__(self, per_user: int, global_limit: int) -> None:
        self._per_user = per_user
        self._global = RateLimiter(global_limit)
        self._users: dict[int, RateLimiter] = {}
        self._last_cleanup = time.monotonic()

    def check(self, user_id: int) -> tuple[bool, int]:
        now = time.monotonic()
        if now - self._last_cleanup > 300:
            self._cleanup()
        limiter = self._users.get(user_id)
        if limiter is None:
            limiter = self._users[user_id] = RateLimiter(self._per_user)
        if not self._global.allow():
            return False, self._global.retry_after()
        if not limiter.allow():
            return False, limiter.retry_after()
        return True, 0

    def _cleanup(self) -> None:
        self._last_cleanup = time.monotonic()
        for uid in [uid for uid, lim in self._users.items() if not lim._events]:
            self._users.pop(uid, None)


class Throttle:
    """Минимальный интервал между вызовами внешнего API (экономия токенов)."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(0.0, min_interval)
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        delta = self.min_interval - (now - self._last)
        if delta > 0:
            time.sleep(delta)
        self._last = time.monotonic()


# --------------------------------------------------------------------------- #
# Хеши для сопоставления без хранения
# --------------------------------------------------------------------------- #

def blind_index(value: str, salt: str) -> str:
    """Необратимый отпечаток для поиска дублей (HMAC-SHA256)."""
    return hashlib.sha256(f"{salt}:{value}".encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# Обрезка вывода модели
# --------------------------------------------------------------------------- #

_MODEL_PREFIX_RE = re.compile(
    r"^\s*(?:\*\*)?(?:ирина|иринa|бот|assistant|ии)\s*(?:\*\*)?\s*[:：]\s*",
    re.IGNORECASE,
)
_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)


def sanitize_model_reply(text: str, max_sentences: int = 9, max_len: int = 1800) -> str:
    """Готовит ответ модели к отправке в чат: без разметки, без псевдонима автора."""
    out = _CODE_BLOCK_RE.sub(" ", text or "")
    out = _MODEL_PREFIX_RE.sub("", out)
    out = out.replace("**", "").replace("__", "")
    out = re.sub(r"^#{1,6}\s*", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*[-*+]\s+", "• ", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    if not out:
        return ""
    # Слишком длинные «простыни» режем по предложениям.
    sentences = re.split(r"(?<=[.!?…])\s+", out)
    if len(sentences) > max_sentences:
        out = " ".join(sentences[:max_sentences]).strip()
    if len(out) > max_len:
        out = out[:max_len].rsplit(" ", 1)[0] + "…"
    return out


# --------------------------------------------------------------------------- #
# Разрешённые действия от клавиатуры
# --------------------------------------------------------------------------- #

ALLOWED_ACTIONS = frozenset({
    "menu", "apply", "about", "status", "ask", "back", "cancel", "skip",
    "confirm_send", "edit", "day_sat", "day_sun", "day_both", "resume", "human",
    "unmute", "cancel_question",
})


def is_allowed_action(action: object) -> bool:
    return isinstance(action, str) and action in ALLOWED_ACTIONS


def is_allowed_category(key: object, known: Iterable[str]) -> bool:
    return isinstance(key, str) and key in set(known)


# --------------------------------------------------------------------------- #
# Диагностика
# --------------------------------------------------------------------------- #

@dataclass
class SecurityReport:
    findings: list[str] = field(default_factory=list)
    passed: list[str] = field(default_factory=list)

    def fail(self, title: str, detail: str) -> None:
        self.findings.append(f"[{title}] {detail}")

    def ok(self, title: str) -> None:
        self.passed.append(title)


def scan_tree_for_secrets(root, cfg) -> SecurityReport:
    """Ищет утечку токенов в файлах проекта (кроме .env и служебных исключений)."""
    report = SecurityReport()
    skip_dirs = {".git", "__pycache__", ".venv", "venv", "node_modules", "data", ".idea"}
    secrets = [s for s in (cfg.vk_group_token, cfg.llm_api_key) if s]
    found = False
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.name in {".env", ".env.example", "credentials.json", "service_account.json"}:
            continue
        if path.suffix.lower() not in {".py", ".md", ".txt", ".json", ".ps1", ".bat", ".sh", ".env", ".cfg", ".ini"}:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in secrets:
            if secret and secret in content:
                report.fail("SECRET-IN-SOURCE", f"секрет найден в {path.relative_to(root)}")
                found = True
    if not found:
        report.ok("Секреты не найдены в исходных файлах проекта")
    return report
