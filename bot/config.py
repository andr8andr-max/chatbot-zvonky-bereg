"""Загрузка конфигурации из .env и переменных окружения.

Секреты (VK_GROUP_TOKEN, LLM_API_KEY, ключ сервис-аккаунта Google) читаются
ТОЛЬКО из окружения / .env и никогда не пишутся в код, логи или репозиторий.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_ENV_LOADED: list[str] = []

_TRUE = {"1", "true", "yes", "y", "on", "да", "истина"}
_FALSE = {"0", "false", "no", "n", "off", "нет", "ложь"}


class ConfigError(RuntimeError):
    """Некорректная или неполная конфигурация."""


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #

def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_dotenv(path: Path | None = None, *, override: bool = False) -> list[str]:
    """Минимальный парсер .env (без python-dotenv).

    Поддерживает KEY=value, # комментарии, одинарные и двойные кавычки,
    экспорт KEY=value и перенос строки через «\\» в конце строки.
    """
    if path is None:
        path = Path(os.environ.get("ENV_FILE", ROOT / ".env"))
    if not path.is_file():
        return []

    raw = path.read_text(encoding="utf-8-sig")
    # Склеиваем продолжения строк, не трогая содержимое кавычек.
    raw = re.sub(r"\\\r?\n\s*", "", raw)

    loaded: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = _strip_quotes(value.split(" #")[0].strip() if " #" in value else value.strip())
        if override or key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    _ENV_LOADED.extend(loaded)
    return loaded


# --------------------------------------------------------------------------- #
# Геттеры
# --------------------------------------------------------------------------- #

def _get(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _req(name: str) -> str:
    value = _get(name)
    if not value:
        raise ConfigError(
            f"Не задана обязательная переменная {name}. "
            f"Скопируйте .env.example в .env и заполните её. "
            f"Файл .env обязательно должен быть в .gitignore."
        )
    return value  # type: ignore[return-value]


def _int(name: str, default: int) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть целым числом, получено: {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} должно быть числом, получено: {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _get(name)
    if raw is None:
        return default
    low = raw.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise ConfigError(f"{name} должно быть true/false, получено: {raw!r}")


def _int_list(name: str, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    raw = _get(name)
    if not raw:
        return default
    out: list[int] = []
    for chunk in re.split(r"[,\s;]+", raw):
        if not chunk:
            continue
        try:
            out.append(int(chunk))
        except ValueError as exc:
            raise ConfigError(f"{name}: не удалось разобрать число {chunk!r}") from exc
    return tuple(out)


def _str_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _get(name)
    if not raw:
        return default
    return tuple(x for x in re.split(r"[,\s;]+", raw) if x)


def loaded_env_keys() -> list[str]:
    """Список ключей, прочитанных из .env (для диагностики)."""
    return list(_ENV_LOADED)


# --------------------------------------------------------------------------- #
# Проверка «похоже на секрет»
# --------------------------------------------------------------------------- #

_SECRET_PATTERNS = (
    re.compile(r"^sk-[A-Za-z0-9_\-]{16,}$"),                 # ProxyAPI / OpenAI
    re.compile(r"^[A-Za-z0-9_\-]{30,}$"),                    # VK community token
    re.compile(r"^vk[0-9a-zA-Z_\-]{20,}$"),                   # VK user token (v1)
    re.compile(r"^ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT / Google
    re.compile(r"^[0-9a-f]{40}$"),                           # SHA-1 hex
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def looks_like_secret(value: str) -> bool:
    """Эвристика: похоже ли значение на секрет (для защиты от утечки в лог)."""
    if not value:
        return False
    value = value.strip()
    if any(p.search(value) for p in _SECRET_PATTERNS):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]{24,}", value)) and not re.search(r"[ /\u0400-\u04ff]", value)


# --------------------------------------------------------------------------- #
# Конфигурация
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Category:
    key: str
    title: str           # кнопка
    sheet: str           # лист в Google Таблице
    prompt_hint: str     # подсказка к полю «Описание»
    #: Вопрос про «Описание» под эту категорию. Музыкантская формулировка
    #: («что покажете или как звучит») волонтёру или фудкорту не подходит.
    prompt_question: str = ""


@dataclass(frozen=True)
class Config:
    # --- ВКонтакте -------------------------------------------------------- #
    vk_group_id: int
    vk_group_token: str
    vk_api_version: str
    vk_lp_wait: int
    vk_admin_peers: tuple[int, ...]
    vk_admin_users: tuple[int, ...]
    vk_access_token_is_group: bool

    # --- ProxyAPI / LLM ---------------------------------------------------- #
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_max_tokens: int
    llm_timeout: float
    llm_retries: int
    #: Общий лимит секунд на все запросы к модели в одном сообщении.
    llm_budget_sec: float
    llm_enabled: bool
    prompt_file: Path
    #: Описание характера и тона Ирины. Загружается вместе с промптом, но
    #: отдельно от него: правила поведения можно править, не трогая системный
    #: промпт, и наоборот.
    character_file: Path
    knowledge_base_file: Path
    kb_mode: str

    # --- Google Sheets ----------------------------------------------------- #
    sheets_enabled: bool
    sheets_poll_enabled: bool
    sheets_poll_interval_sec: int
    google_spreadsheet_id: str
    google_credentials_file: Path | None
    google_credentials_json: str | None
    categories: tuple[Category, ...]

    # --- Хранилище и логи -------------------------------------------------- #
    db_path: Path
    log_file: Path
    log_level: str
    csv_export_path: Path

    # --- Поведение --------------------------------------------------------- #
    idle_timeout_min: int
    context_messages: int
    max_text_len: int
    rate_limit_per_min: int
    rate_limit_global_per_min: int
    llm_min_interval_sec: float
    max_description_len: int
    store_pii_locally: bool
    ask_private_chat_hint: bool
    typing_enabled: bool
    typing_delay_sec: float
    emergency_contacts: tuple[str, ...]
    #: Тестовый прогон: уведомления организаторам не уходят в живые чаты.
    dry_run: bool
    public_site: str
    public_email: str

    # служебное
    secrets: tuple[str, ...] = field(default=(), repr=False)

    # ------------------------------------------------------------------ #
    def category(self, key: str) -> Category | None:
        for c in self.categories:
            if c.key == key:
                return c
        return None

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.vk_admin_users

    def redacted(self) -> str:
        """Строка конфигурации для логов — без единого секрета."""
        return (
            f"group_id={self.vk_group_id} model={self.llm_model} "
            f"sheets={'on' if self.sheets_enabled else 'off'} "
            f"admin_peers={len(self.vk_admin_peers)} admin_users={len(self.vk_admin_users)} "
            f"store_pii={self.store_pii_locally}"
        )


DEFAULT_CATEGORIES = (
    Category(
        key="musician",
        title="Музыкант / группа",
        sheet="Музыканты_Группы",
        prompt_hint=(
            "жанр, состав, сколько человек, своя техника или нужен базовый райдер"
        ),
        prompt_question="Расскажите пару предложений: что покажете или как звучит 🙂",
    ),
    Category(
        key="artist",
        title="Художник / мастер",
        sheet="Художники_Мастера",
        prompt_hint=(
            "что делаешь, нужны ли материалы, продажа работ или мастер-класс, нужен ли стенд"
        ),
        prompt_question="Расскажите пару предложений: что делаете и что покажете 🙂",
    ),
    Category(
        key="food",
        title="Фуд-корт",
        sheet="Фудкорт",
        prompt_hint=(
            "что готовите, есть ли веган-опции, готовы ли работать с многоразовой посудой, "
            "документы на торговлю"
        ),
        prompt_question="Расскажите пару предложений: что готовите 🙂",
    ),
    Category(
        key="volunteer",
        title="Волонтёр",
        sheet="Волонтёры",
        prompt_hint="в какие дни, зона интереса (вход, инфо-стол, экология, детская зона), опыт",
        prompt_question=(
            "Расскажите пару предложений: чем вы хотите помочь и в какие дни 🙂"
        ),
    ),
)

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "musician": (
        "музык", "музыкант", "группа", "гурт", "бэнд", "бенд", "вокалист", "вокалистка",
        "гитара", "гитар", "скрипк", "баян", "ударн", "барабан", "клавиш", "пиано",
        "дуэт", "ансамбль", "оркестр", "рок", "метал", "панк", "инди", "фолк", "джаз",
        "хип-хоп", "электроник", "диджей", "днб", "play", "спеть", "выступи", "концерт",
    ),
    "artist": (
        "художник", "художница", "рисов", "живопис", "малер", "иллюстра", "граффити",
        "стрит-арт", "анимаци", "керамик", "глина", "гончар", "вышив", "текстил",
        "ремесл", "мастер-класс", "инсталляц", "фотограф", "дизайнер", "арт",
    ),
    "food": (
        "фуд", "фудкорт", "еда", "кухн", "кафе", "кофейн", "coffee", "чай", "десерт",
        "вафл", "морожен", "пицц", "бургер", "шашлык", "плов", "кутаб", "сэндвич",
        "напитк", "лимонад", "квас", "смузи", "выпечк", "стол",
    ),
    "volunteer": (
        "волонт", "волонтёр", "волонтер", "помощ", "помочь", "ассистент", "смен",
        "дежур", "координатор", "помогу",
    ),
}


def load_config() -> Config:
    load_dotenv()

    group_id = _int("VK_GROUP_ID", 0)
    if group_id <= 0:
        raise ConfigError(
            "VK_GROUP_ID не задан или некорректен. Укажите ID сообщества "
            "(число без минуса) — его видно в адресной строке сообщества."
        )

    token = _req("VK_GROUP_TOKEN")
    if looks_like_secret(token) is False and len(token) < 20:  # noqa: SIM103
        raise ConfigError("VK_GROUP_TOKEN выглядит слишком коротким. Проверьте токен сообщества.")

    llm_key = _get("LLM_API_KEY", "") or ""
    llm_enabled = _bool("LLM_ENABLED", bool(llm_key))
    if llm_enabled and not llm_key:
        raise ConfigError(
            "LLM_ENABLED=true, но LLM_API_KEY не задан. Впишите ключ ProxyAPI из личного кабинета."
        )

    creds_file_raw = _get("GOOGLE_CREDENTIALS_FILE")
    creds_file = Path(creds_file_raw) if creds_file_raw else None
    creds_json = _get("GOOGLE_CREDENTIALS_JSON")
    spreadsheet_id = _get("GOOGLE_SPREADSHEET_ID", "") or ""
    sheets_enabled = _bool("SHEETS_ENABLED", bool(spreadsheet_id and (creds_file or creds_json)))

    if sheets_enabled and not (creds_file or creds_json):
        raise ConfigError(
            "SHEETS_ENABLED=true, но не задан GOOGLE_CREDENTIALS_FILE или GOOGLE_CREDENTIALS_JSON."
        )

    base_url = (_get("LLM_BASE_URL", "https://api.proxyapi.ru/v1") or "").rstrip("/")
    if not base_url.startswith("https://"):
        raise ConfigError(
            "LLM_BASE_URL обязан начинаться с https:// — передавать API-ключ по http нельзя. "
            "ПроксиAPI использует адрес https://api.proxyapi.ru/v1"
        )

    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    def _lock(path: Path) -> Path:
        try:
            path.touch(exist_ok=True)
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    categories = DEFAULT_CATEGORIES
    if _get("CATEGORIES_JSON"):
        try:
            raw = json.loads(_get("CATEGORIES_JSON") or "[]")
            categories = tuple(
                Category(
                    key=str(item["key"]),
                    title=str(item["title"]),
                    sheet=str(item["sheet"]),
                    prompt_hint=str(item.get("prompt_hint", "")),
                    prompt_question=str(item.get("prompt_question", "")),
                )
                for item in raw
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"CATEGORIES_JSON некорректен: {exc}") from exc

    cfg = Config(
        vk_group_id=group_id,
        vk_group_token=token,
        vk_api_version=_get("VK_API_VERSION", "5.199") or "5.199",
        vk_lp_wait=max(5, min(90, _int("VK_LP_WAIT", 25))),
        vk_admin_peers=_int_list("VK_ADMIN_PEER_IDS"),
        vk_admin_users=_int_list("VK_ADMIN_USER_IDS"),
        vk_access_token_is_group=_bool("VK_TOKEN_IS_GROUP", True),
        llm_base_url=base_url,
        llm_api_key=llm_key,
        llm_model=_get("LLM_MODEL", "inclusionai/ling-3.0-flash") or "",
        llm_temperature=_float("LLM_TEMPERATURE", 0.6),
        llm_max_tokens=_int("LLM_MAX_TOKENS", 500),
        llm_timeout=_float("LLM_TIMEOUT", 12.0),
        llm_retries=max(0, min(5, _int("LLM_RETRIES", 2))),
        # 15 с на сообщение: на столько хватает обычному ответу, а при
        # «зависшей» модели человек раньше получит ответ шаблоном, чем будет
        # смотреть на индикатор «печатает…».
        llm_budget_sec=max(1.0, _float("LLM_BUDGET_SEC", 15.0)),
        llm_enabled=llm_enabled,
        prompt_file=Path(_get("PROMPT_FILE", str(ROOT / "SYSTEM_PROMPT_Irina_Zvonky_Bereg.md"))),
        character_file=Path(_get("CHARACTER_FILE", str(ROOT / "Irina_Character.md"))),
        knowledge_base_file=Path(_get("KNOWLEDGE_BASE_FILE", str(ROOT / "Zvonky_Bereg_Knowledge_Base.md"))),
        kb_mode=_get("KB_MODE", "summary") or "summary",
        sheets_enabled=sheets_enabled,
        sheets_poll_enabled=_bool("SHEETS_POLL_ENABLED", True),
        sheets_poll_interval_sec=max(30, _int("SHEETS_POLL_INTERVAL_SEC", 120)),
        google_spreadsheet_id=spreadsheet_id,
        google_credentials_file=creds_file,
        google_credentials_json=creds_json,
        categories=categories,
        db_path=_lock(Path(_get("DB_PATH", str(data_dir / "bot.sqlite3")))),
        log_file=Path(_get("LOG_FILE", str(data_dir / "bot.log"))),
        log_level=(_get("LOG_LEVEL", "INFO") or "INFO").upper(),
        csv_export_path=_lock(
            Path(_get("CSV_EXPORT", str(data_dir / "applications_export.csv")))
        ),
        idle_timeout_min=_int("IDLE_TIMEOUT_MIN", 15),
        context_messages=max(0, min(20, _int("CONTEXT_MESSAGES", 8))),
        max_text_len=_int("MAX_TEXT_LEN", 2000),
        rate_limit_per_min=_int("RATE_LIMIT_PER_MIN", 12),
        rate_limit_global_per_min=_int("RATE_LIMIT_GLOBAL_PER_MIN", 120),
        llm_min_interval_sec=_float("LLM_MIN_INTERVAL_SEC", 1.0),
        max_description_len=_int("MAX_DESCRIPTION_LEN", 1500),
        store_pii_locally=_bool("STORE_PII_LOCALLY", True),
        ask_private_chat_hint=_bool("ASK_PRIVATE_CHAT_HINT", False),
    typing_enabled=_bool("TYPING_ENABLED", True),
    # Индикатор гаснет в момент отправки ответа, поэтому тянуть с его стартом
    # нельзя: при ответе модели за 2 с индикатор и жил бы 1 с. Ставим сразу,
    # для мгновенных ответов (нажатие кнопки) он и так не показывается.
    typing_delay_sec=max(0.0, min(10.0, _float("TYPING_DELAY_SEC", 0.0))),
        emergency_contacts=_str_list("EMERGENCY_CONTACTS"),
        dry_run=_bool("DRY_RUN", False),
        public_site=_get("PUBLIC_SITE", "zv-bereg.ru") or "",
        public_email=_get("PUBLIC_EMAIL", "info@zv-bereg.ru") or "",
        secrets=tuple(x for x in (token, llm_key, creds_json or "") if x),
    )
    return cfg


def self_check(cfg: Config) -> list[str]:
    """Возвращает список предупреждений по безопасности конфигурации."""
    warnings: list[str] = []
    if len(cfg.vk_group_token) < 40:
        warnings.append("VK_GROUP_TOKEN выглядит коротким — проверьте, что это полный токен.")
    if not cfg.vk_admin_users:
        warnings.append(
            "VK_ADMIN_USER_IDS пуст — команды смены статуса и вопросов организатору "
            "не сможет выполнить никто. Укажите ID организаторов."
        )
    if not cfg.vk_admin_peers:
        warnings.append(
            "VK_ADMIN_PEER_IDS пуст — уведомления о новых заявках некуда отправлять."
        )
    # В VK_ADMIN_PEER_IDS должны быть только беседы. Личка там означает
    # дублирование: организатор получает одно уведомление трижды и перестаёт
    # читать. Личные сообщения бот шлёт сам — как запасной канал, когда беседы
    # недоступны, и при срочных оповещениях.
    personal_in_peers = [p for p in cfg.vk_admin_peers if p < 2_000_000_000]
    if personal_in_peers:
        warnings.append(
            f"В VK_ADMIN_PEER_IDS есть личные чаты {personal_in_peers} — уведомления "
            f"придут и в беседу, и в личку. Оставьте там только беседы: "
            f"личные сообщения бот отправляет сам, когда беседа недоступна "
            f"или случай срочный."
        )
    if cfg.store_pii_locally and cfg.sheets_enabled:
        warnings.append(
            "STORE_PII_LOCALLY=true: телефоны и почта останутся в локальной базе. "
            "Если машина не доверенная — поставьте STORE_PII_LOCALLY=false."
        )
    if cfg.llm_budget_sec > 45:
        # Больше 45 с человек не ждёт: он успеет решить, что бот сломался, и
        # уйдёт. Длинный бюджет означает только одно — долгое молчание.
        warnings.append(
            f"LLM_BUDGET_SEC={cfg.llm_budget_sec:.0f} — человек столько не будет ждать "
            f"ответа. Для чат-бота разумно 10-20 секунд."
        )
    if cfg.llm_timeout > cfg.llm_budget_sec:
        warnings.append(
            f"LLM_TIMEOUT={cfg.llm_timeout:.0f} больше LLM_BUDGET_SEC="
            f"{cfg.llm_budget_sec:.0f}: таймаут не сработает, запрос всё равно "
            f"оборвётся по бюджету. Сделайте LLM_TIMEOUT меньше бюджета."
        )
    for path in (cfg.db_path,):
        try:
            mode = path.stat().st_mode & 0o777
            if os.name != "nt" and mode & 0o077:
                warnings.append(f"У файла {path.name} слишком широкие права ({mode:o}).")
        except OSError:
            pass
    return warnings
