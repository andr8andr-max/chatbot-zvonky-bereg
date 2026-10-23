"""Сценарии чат-бота: меню, анкета, статус, вопрос организатору, эскалация.

Ключевой принцип: критичные части (анкета, валидация, сводка, статус, эскалация,
сверка фактов) работают детерминированно в коде. Модель отвечает только за
свободный диалог и формулировки — и её ответ не может изменить состояние заявки.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from . import keyboards as kb
from .config import Config
from .llm import LLMError, ProxyLLM
from .prompts import PromptBuilder
from .security import (
    ValidationResult,
    clean_text,
    is_affirmative,
    is_emoji_only,
    is_farewell,
    is_negative,
    is_pure_farewell,
    is_reaction_only,
    is_urgent,
    mask_contact,
    needs_escalation,
    validate_city,
    validate_day,
    validate_description,
    validate_email,
    validate_link,
    validate_name,
    validate_phone,
    wants_human,
)
from .sheets import CsvFallback, SheetsError, SheetsWriter
from .storage import (
    STATUS_APPROVED,
    STATUS_NEEDS_ANSWER,
    STATUS_NEW,
    STATUS_REJECTED,
    STATUS_REVIEW,
    Application,
    DuplicateApplication,
    Store,
)
from .vk_api import Message, VKError, attachment_summary

log = logging.getLogger("bot.flows")

STATE_MENU = "menu"
STATE_CATEGORY = "apply_category"
STATE_STEP = "apply_step"
STATE_REVIEW = "apply_review"
STATE_ASK = "ask_org"
STATE_HUMAN = "human"

MAX_ATTEMPTS = 2

#: Ссылки в ответах не нужны: в личных сообщениях они всё равно не кликаются,
#: а выглядят как реклама. Вырезаем адреса, почту и «обёртки» вокруг них.
#: Хвост ограничен пунктуацией — жадный ``\S*`` съедал бы строку после адреса.
_TAIL = r"[.,;:!?)\]\u00bb\u201c\u201d]*"
_URL_RE = re.compile(
    r"(?i)(?:https?://|www\.)[^\s<>()\[\]]+" + _TAIL
    + r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+" + _TAIL
    # Мессенджеры и соцсети без схемы: «t.me/zvbereg», «vk.com/zv». Важно раньше
    # общего домена: иначе домен съедается, а путь вида /zvbereg остаётся.
    + r"|\b(?:t\.me|telegram\.me|wa\.me|discord\.(?:gg|com)|vk\.com|ok\.ru)"
    r"/[\w./-]+" + _TAIL
    + r"|\b[\w-]+(?:\.[\w-]+)*\.(?:ru|com|net|org|info|рф|рф\b)" + _TAIL
)
_LINK_LEAD = re.compile(r"(?i)\s*(?:Подробнее на|Подробности на|Сайт)\s*:?\s*$")
#: Удалённая ссылка оставляет висящие предлоги: «Подробности на ,».
_DANGLING = re.compile(
    r"(?i)\b(?:и|на|по|с|в|к|для|от|до|или|а|но)\b[ \t]*"
    r"(?=[.,!?:;…]|\bи\b|\bа\b|\bили\b|\bно\b|$|\n)"
)


def strip_links(text: str) -> str:
    """Убирает адреса сайтов и почту из текста для пользователя."""
    cleaned = _URL_RE.sub("", text or "")
    cleaned = _LINK_LEAD.sub("", cleaned)
    # Пробелы схлопываем до чистки предлогов, иначе «на  ,» не распознаётся.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    for _ in range(3):  # цепочки вида «на , а ,» чистим в несколько проходов
        cleaned = _DANGLING.sub("", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[,;:]+[ \t]*(?=[.!?…\n])", "", cleaned)
    cleaned = re.sub(r"[ \t]+([,.!?:;…])", r"\1", cleaned)
    cleaned = re.sub(r"(?i)\b(?:и|а|но|или)\s*$", "", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

#: Поля анкеты в порядке сценария.
@dataclass(frozen=True)
class Field:
    key: str
    title: str
    question: str
    optional: bool = False
    kind: str = "text"


FIELDS: tuple[Field, ...] = (
    Field("full_name", "ФИО / название",
          "Как вас зовут? Если у вас группа или проект — напишите название."),
    Field("city", "Город", "Откуда вы будете?"),
    Field("phone", "Телефон",
          "Телефон, чтобы куратор мог быстро связаться."),
    Field("email", "E-mail",
          "E-mail, на который пришлём детали, если заявку одобрят."),
    Field("description", "Описание", ""),   # вопрос зависит от категории
    # Общие поля держим нейтральными: «райдер» и «портфолио» — музыкантские
    # и арт-слова, волонтёру и фудкорту они не подходят.
    Field("link", "Ссылка",
          "Ссылка на соцсети или канал — если хотите, чтобы посмотрели. "
          "Если неактуально — пропускайте.", optional=True),
    Field("attachment", "Фото / файл",
          "Можете скинуть фото или файл? Если пока нет — пропускайте.", optional=True),
    Field("day", "Удобный день", "Какой день вам удобнее?"),
)

STEP_OF = {f.key: i + 1 for i, f in enumerate(FIELDS)}

#: Вежливые ответы без обращения к модели — экономия токенов и предсказуемость.
#: Ссылки намеренно не вставляем: пользователь их всё равно не откроет в личке,
#: а текст с адресами выглядит как спам.
ABOUT_TEXT = """Здравствуйте! Коротко о главном 🙂

«Звонкий берег» — уличный фестиваль в Шуе, на берегу Тезы, в зоне Городского парка. Два дня: суббота и воскресенье. Три площадки: «Колокол» — главная, «Теза» — акустика у воды, «Мастер-берег» — арт-поляна со стрит-артом, мастер-классами и открытым микрофоном.

Вход свободный, 0+. Точные даты сезона публикуем в соцсетях и на сайте фестиваля за две недели до мероприятия."""

CATEGORY_QUESTION = "Отлично, давайте оформим заявку 🙂 Кто вы на фестивале?"

SKIP_WORDS = {"пропустить", "пропуск", "пропустим", "дальше", "поехали дальше",
              "нет", "нету", "не надо", "не буду", "не хочется", "нет данных", "пока нет",
              "не хочу", "ничего", "skip", "позже", "later", "-", "—", "не сейчас",
              "оставить пустым", "пусто", "пускай пусто"}

#: Синонимы названий полей для режима «Изменить»: человеку не обязательно
#: помнить номер, достаточно написать «почта», «город» или «телефон».
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "full_name": ("имя", "фио", "фамилия", "имя и фамилия", "название", "позывной", "ник"),
    "city": ("город", "откуда"),
    "phone": ("телефон", "номер", "номер телефона", "тел", "мобильный", "сотовый"),
    "email": ("почт", "email", "e-mail", "е-mail", "мейл", "меил", "электронная почта"),
    "description": ("описание", "расскажите", "о себе", "рассказ"),
    "link": ("ссылка", "ссылки", "соцсети", "соцсеть", "страница", "канал"),
    "attachment": ("фото", "файл", "файлы", "фотография", "вложение", "картинка", "прикрепить"),
    "day": ("день", "дни", "удобный день", "когда", "дата", "суббота", "воскресенье"),
}

#: Приглашение в режиме «Изменить»: нумерация совпадает со сводкой _summary.
EDIT_PROMPT = (
    "Если что-то поправить — напишите номер поля (например, 4) "
    "или просто название: «телефон», «почта», «город»… 🙂"
)

#: После передачи вопроса бот ждёт ответа организатора. Эти фразы означают
#: «всё, не надо ждать» — иначе человек застревает в ожидании навсегда.
CANCEL_WAIT_WORDS = {
    "отмена", "отменить", "стоп", "хватит", "не жду", "всё", "все", "всё равно",
    "неважно", "забей", "отпишись", "ответ не нужен", "не нужен ответ",
}

#: Признаки вопроса о фестивале. Нужны, чтобы в состоянии выбора категории
#: не отвечать «не поняла категорию» на вопрос «сколько стоит вход».
QUESTION_WORDS = (
    "сколько", "стоит", "стоимость", "вход", "бесплат", "где", "когда", "во сколько",
    "что это", "что такое", "как", "почему", "зачем", "кто", "какой", "какая", "можно",
    "выступ", "слот", "площадк", "сцен", "программ", "расписан", "билет", "дети",
    "собака", "парков", "метро", "автобус", "еда", "напит", "туалет", "wi-fi", "wifi",
    "дожд", "погод", "безопасн", "куда писать", "вас зовут", "кто вы",
)


@dataclass
class Reply:
    text: str
    keyboard: dict | None = None
    mute: bool = False
    #: Номер созданного вопроса организатору. По нему приложение запоминает
    #: id отправленного сообщения: без этого нечем снять кнопку
    #: «Отменить вопрос», когда организатор ответит.
    question_id: int = 0


def now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _is_scenario_keyword(text: str) -> bool:
    """Похоже ли сообщение на вопрос о фестивале, а не на выбор категории."""
    low = text.lower()
    if len(low) > 160:
        return False
    return any(word in low for word in QUESTION_WORDS)


#: Ответы на голый смайлик. Тон — из файла характера: спокойно, без суеты,
#: без «чем могу помочь» в конце и всегда с понятным следующим шагом. Вариант
#: выбирается по самому смайлику, поэтому повтор одного и того же знака даёт
#: одну и ту же реплику — это заметно спокойнее, чем случайный набор.
EMOJI_REPLIES = (
    "Принято 🙂 Скажите, что для вас важно — подскажу про фестиваль или приму заявку.",
    "Ок 🙂 Готова ответить на вопрос о фестивале или принять заявку.",
    "Хорошо 🙂 Когда решите, с чего начать, — пишите, я на связи.",
    "Вижу 🙂 Рассказывайте, что интересует, — подскажу по фестивалю.",
    "Слышу 🙂 Если что-то по программе осталось неясным — спрашивайте.",
)


class BotEngine:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        llm: ProxyLLM,
        prompts: PromptBuilder,
        sheets: SheetsWriter,
        csv_fallback: CsvFallback,
        vk=None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.llm = llm
        self.prompts = prompts
        self.sheets = sheets
        self.csv = csv_fallback
        self.vk = vk
        self.throttle = None  # Throttle ставится в app.py: экономит токены
        self.category_keys = tuple(c.key for c in cfg.categories)
        # Одно «живое» сообщение-меню на диалог: id и текст последнего
        # сообщения, где ещё могут быть нажимаемые кнопки.
        self._menu_msg: dict[int, tuple[int, str]] = {}

    # ================================================================== #
    # Входная точка
    # ================================================================== #
    def handle(self, msg: Message) -> list[Reply]:
        """Точка входа: решает, показать ли представление, и маршрутизирует.

        Представление «Я Ирина» отправляется ровно один раз — в начале
        общения с человеком. Флаг ставится здесь, до маршрутизации, иначе имя
        всплыло бы позже, когда пользователь просто поздоровался в середине
        диалога. Кнопка или прощание в первом же сообщении поводом для
        представления не являются.

        Смысл флага — «решение о представлении принято», а не «представление
        показано»: он тратится в любом случае, и человек, сказавший на первом
        сообщении только «пока», считается знакомым, чтобы представление не
        всплыло перед ним через минуту. Поэтому по нему нельзя судить, было ли
        представление действительно отправлено.
        """
        self.store.touch(msg.from_id, msg.peer_id)
        # Открываем бюджет времени на ответ: дальше классификация и генерация
        # ответа берут время из него, и при нехватке бот отвечает шаблоном
        # сразу, а не висит с индикатором «печатает…».
        begin = getattr(self.llm, "begin_message", None)
        if callable(begin):
            begin()
        first_contact = not self.store.greeted_at(msg.from_id)
        if first_contact:
            self.store.mark_greeted(msg.from_id)
            text = clean_text(msg.text, self.cfg.max_text_len)
            # Кнопка, прощание, ЧП и передача вопроса — не поводы представляться:
            # «Привет, я Ирина, рассказать о фестивале?» перед словом «помогите»
            # звучит неуместно. Исключение — «Начать»: это начало общения.
            # Флаг при этом всё равно тратится, чтобы представление не всплыло позже.
            first_contact = not (
                (msg.action and msg.action != "menu")
                or is_farewell(text)
                or is_urgent(text)
                or needs_escalation(text)
                or wants_human(text)
            )
        replies = self._route(msg, first_contact)
        # Представление уже могло прийти из приветствия — тогда не дублируем.
        if first_contact and replies and not replies[0].text.startswith(self._INTRO[0]):
            replies = self._with_intro(replies)
        return replies

    def _with_intro(self, replies: list[Reply]) -> list[Reply]:
        """Добавляет представление к первому ответу нового пользователя."""
        if not replies:
            return replies
        replies[0] = replace(replies[0], text=f"{self._INTRO[0]}\n\n{replies[0].text}")
        return replies

    def _route(self, msg: Message, first_contact: bool = False) -> list[Reply]:
        user = msg.from_id
        text = clean_text(msg.text, self.cfg.max_text_len)
        action = msg.action
        args = msg.args
        attachments = attachment_summary(msg.attachments)

        # 1) Если участник ждал ответа организатора — автоответ выключен.
        state, step, data, muted, attempts = self.store.get_state(user)
        if muted and not action:
            if not text:
                return []
            # Явный отказ ждать дальше — снимаем ожидание.
            if text.strip().lower() in CANCEL_WAIT_WORDS:
                self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
                return [Reply(
                    "Хорошо, больше не жду ответа 🙂 Можем вернуться к фестивалю, "
                    "заявке или просто поболтать.",
                    self._main_menu(user),
                )]
            # Прощание и короткая реакция — не продолжение вопроса. Раньше
            # «здорово, спасибо» уходило организаторам как дополнение к вопросу,
            # а человек получал «дописала и передала»: организаторам лишнее
            # сообщение, человеку — невпопад. Ожидание ответа сохраняем.
            if is_farewell(text) or is_pure_farewell(text):
                return [Reply(
                    "Пока! 🙂 Как только придёт ответ, я сообщу здесь.",
                    kb.after_escalation(),
                    mute=True,
                )]
            if is_reaction_only(text) or is_emoji_only(text):
                return [Reply("Принято 🙂", kb.after_escalation(), mute=True)]
            # Сообщение надо действительно передать. Раньше здесь текст просто
            # исчезал, а бот отвечал «передала координатору» — человек думал,
            # что вопрос ушёл, но организаторам ничего не приходило.
            self._forward_to_organizers(user, text, follow_up=True, peer=msg.peer_id)
            return [Reply(
                "Дописала к вопросу и передала координатору 🙂",
                kb.after_escalation(),
                mute=True,
            )]

        # 2) Служебные команды организатора (только для VK_ADMIN_USER_IDS).
        #    Проверяются раньше пользовательских команд, иначе «!статус» перехватит
        #    кнопка «Мой статус».
        lowered = text.lower().strip()
        is_service = text.startswith("!") or (text.startswith("/") and not lowered.startswith("/start"))
        if is_service:
            admin_reply = self._admin_command(msg, text, args, state, step, data, attempts)
            if admin_reply is not None:
                return admin_reply

        # 3) Текстовые команды доступны в любой момент.
        for keyword, action_name in (
            ("/start", "menu"), ("начать", "menu"), ("старт", "menu"), ("начать заново", "menu"),
            ("отмена", "cancel"), ("назад", "back"), ("продолжить", "resume"),
            ("мои заявки", "status"), ("мой статус", "status"), ("статус", "status"),
            ("о фестивале", "about"), ("подать заявку", "apply"), ("хочу участвовать", "apply"),
            ("вопрос организатору", "ask"),
        ):
            if keyword in lowered and len(lowered) <= 44:
                forced = self._handle_action(
                    msg, action_name, {}, state, step, data, attempts, first_contact
                )
                if forced is not None:
                    return forced

        # 4) Кнопки.
        if action:
            replies = self._handle_action(
                msg, action, args, state, step, data, attempts, first_contact
            )
            if replies is not None:
                return replies
            # Молчание на кнопку опаснее: в VK над кнопкой крутится «колёсико»,
            # пока бот не ответит. Поэтому неизвестная кнопка тоже получает ответ.
            if not text:
                log.info("Неизвестная кнопка %r от %s — показываю меню", action, user)
                return self._to_menu(user, first_contact)

        # 5) Сценарии по состоянию.
        if state == STATE_ASK:
            return self._ask_org_finish(msg, text or attachments)

        if state == STATE_CATEGORY:
            return self._category_step(msg, text, args, data, attempts, attachments)

        if state == STATE_STEP:
            return self._form_step(msg, step, text, args, data, attempts, attachments)

        if state == STATE_REVIEW:
            return self._review_step(msg, text, args, data)

        # 6) Участник ответил на вопрос куратора — заявка возвращается в работу.
        taken = self._take_expected_answer(msg, text)
        if taken is not None:
            return taken

        # 7) Свободный диалог в состоянии меню.
        return self._free_dialog(msg, text, state, step, data, first_contact)

    # ================================================================== #
    # Действия кнопок
    # ================================================================== #
    def _handle_action(
        self, msg: Message, action: str, args: dict, state: str, step: int,
        data: dict, attempts: dict, first_contact: bool = False,
    ) -> list[Reply] | None:
        user = msg.from_id

        # Кнопки «Жду ответа» больше нет, но в старых сообщениях VK она
        # остаётся кликабельной навсегда. Раньше нажатие снова включало mute
        # без отправленного вопроса, поэтому просто возвращаем в меню.
        if action == "human":
            return self._to_menu(user)

        if action == "menu":
            self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
            self.store.clear_context(user)
            return [Reply(self._menu_text(user, first_contact), self._main_menu(user))]

        if action == "about":
            self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
            return [Reply(ABOUT_TEXT, self._main_menu(user))]

        if action == "apply":
            return self._start_apply(msg, args, state)

        if action == "status":
            return self._show_status(user)

        if action == "ask":
            self.store.set_state(user, STATE_ASK, 0, {}, muted=False)
            return [Reply(
                "Конечно. Напишите вопрос одним сообщением или несколькими — я всё соберу "
                "и передам координатору.",
                kb.human_handoff(),
            )]

        if action == "cancel_question":
            return self._cancel_question(user, msg)

        if action == "unmute":
            # Человек передумал ждать ответа: снимаем ожидание, чтобы обычные
            # сообщения снова доходили до бота.
            self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
            return [Reply(
                "Хорошо, больше не жду ответа 🙂 Можем вернуться к фестивалю, "
                "заявке или просто поболтать.",
                self._main_menu(user),
            )]

        if action == "resume":
            return self._resume(user, state, step, data)

        if action == "cancel":
            self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
            self.store.clear_context(user)
            return [Reply(
                "Хорошо, бросаем 🙂 Начать заново — кнопка «🔄 Начать заново» "
                "или просто напишите мне.",
                kb.start_over(),
            )]

        if action == "back":
            return self._back(user, state, step, data)

        if action == "skip":
            field = FIELDS[step - 1] if 0 < step <= len(FIELDS) else None
            if field is None or not field.optional:
                return [Reply("Этот пункт обязательный, пропустить не получится 🙂", kb.nav())]
            data[field.key] = ""
            return self._advance(user, data, step)

        if action == "edit":
            if state != STATE_REVIEW:
                return [Reply("Давайте вернёмся к анкете 🙂", kb.confirm())]
            return self._enter_edit(user, data)

        if action == "confirm_send":
            if state != STATE_REVIEW:
                return [Reply("Сначала соберём анкету 🙂", self._main_menu(user))]
            return self._submit(msg, data)

        if action.startswith("day_"):
            if state == STATE_STEP and 0 < step <= len(FIELDS) and FIELDS[step - 1].key == "day":
                value = {"day_sat": "Суббота", "day_sun": "Воскресенье", "day_both": "Оба дня"}[action]
                data["day"] = value
                return self._advance(user, data, step)
            return None

        return None

    # ------------------------------------------------------------------ #
    # Вопрос организатору: отмена и возврат ответа
    # ------------------------------------------------------------------ #
    def _cancel_question(self, user: int, msg: Message) -> list[Reply]:
        """Человек передумал ждать: гасим сам вопрос, а не только ожидание.

        Раньше кнопка просто снимала ожидание, а вопрос оставался в базе и
        организаторам: им потом приходилось отвечать тому, кто ответа не ждёт.
        """
        row = self.store.pending_for_user(user)
        self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
        if row is None:
            return [Reply(
                "Хорошо, больше не жду ответа 🙂 Можем вернуться к фестивалю, "
                "заявке или просто поболтать.",
                self._main_menu(user),
            )]
        qid = int(row["id"])
        self.store.mark_cancelled(qid)
        self.store.audit("bot", "question.cancelled", user, f"#{qid}")
        log.info("Вопрос #%s от %s отменён человеком", qid, user)
        # Организаторам полезно знать, что ответ уже не нужен.
        self._notify_organizers_raw(
            f"↩️ Вопрос [вопрос {qid}] от участника {self._display_name(user)} (id {user}) "
            "отменён, отвечать не нужно."
        )
        return [Reply(
            "Хорошо, отменила 🙂 Если вопрос ещё понадобится — напишите, "
            "и я передам снова.",
            self._main_menu(user),
        )]

    def answer_question(self, row, answer: str, admin_id: int) -> bool:
        """Возвращает ответ организатора человеку и закрывает вопрос.

        Возвращает ``False``, если вопрос уже закрыт: два ответа подряд или
        повторная доставка события не должны слать человеку второе сообщение.
        """
        qid = int(row["id"])
        answer = clean_text(answer, 3000)
        if not answer:
            return False
        # Строку перечитываем, а не доверяем пришедшей: между поиском вопроса и
        # ответом его могли закрыть (второй организатор, повтор события), и по
        # устаревшей копии человек получил бы два ответа на один вопрос.
        row = self.store.question_by_id(qid)
        if row is None:
            log.info("Вопрос #%s уже закрыт, ответ от %s не отправляю", qid, admin_id)
            return False
        user = int(row["user_id"])
        self.store.mark_answered(qid, answer, admin_id)
        self.store.audit("bot", "question.answered", user, f"#{qid}")

        # Снимаем ожидание: вопрос закрыт, диалог можно продолжать.
        self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
        peer = int(row["user_peer"]) or user
        apps = self.store.list_for_user(user)
        suffix = ""
        if apps and apps[0].number:
            suffix = f"\n\nЗаявка {apps[0].number}, статус можно посмотреть кнопкой ниже."
        self._send(
            peer,
            f"Ответ от организаторов на ваш вопрос:\n\n{answer}{suffix}",
            self._main_menu(user),
        )
        # Кнопка «Отменить вопрос» на старом сообщении больше не нужна: вопрос
        # уже не ждёт ответа, и её нажатие путало бы человека.
        bot_message = int(row["bot_message"] or 0)
        if bot_message and self.vk is not None:
            self._clear_question_buttons(peer, bot_message)
        log.info("Вопрос #%s закрыт ответом от %s, отправлен человеку %s", qid, admin_id, user)
        return True

    def _clear_question_buttons(self, peer: int, message_id: int) -> None:
        """Убирает «Отменить вопрос» с сообщения, на котором она висит."""
        from .vk_api import VKError  # noqa: PLC0415

        try:
            self.vk.clear_keyboard(peer, message_id, "Вопрос передан организаторам.")
        except VKError as exc:
            log.info("Не удалось снять кнопку «Отменить вопрос» в peer %s: %s", peer, exc)

    # ================================================================== #
    # Сценарий 1. Подача заявки
    # ================================================================== #
    def _start_apply(self, msg: Message, args: dict, state: str) -> list[Reply]:
        user = msg.from_id
        category_key = str(args.get("category") or "")
        if category_key in self.category_keys:
            return self._begin_form(
                user, category_key, confirmed=bool(args.get("confirmed"))
            )

        # Категория пришла текстом — определим сами.
        if msg.text and msg.text.strip() in self.category_keys:
            return self._begin_form(user, msg.text.strip())

        # Заявка с аккаунта одна: любая не отклонённая блокирует новую.
        existing = self.store.find_active_any(user)
        if existing is not None:
            return [Reply(self._duplicate_text(existing), kb.status_actions(has_application=True))]

        self.store.set_state(user, STATE_CATEGORY, 0, {}, muted=False)
        self.store.clear_context(user)
        return [Reply(CATEGORY_QUESTION, kb.category_menu(self.cfg.categories))]

    def _begin_form(self, user: int, category_key: str, *, confirmed: bool = False) -> list[Reply]:
        cat = self.cfg.category(category_key)
        if cat is None:
            return [Reply("Такой категории у нас нет 🙂", kb.category_menu(self.cfg.categories))]

        active = self.store.find_active_any(user)
        if active is not None:
            return [Reply(
                self._duplicate_text(active),
                kb.status_actions(has_application=True),
            )]
        last = self.store.find_last_any(user)
        if last is not None and last.status == STATUS_REJECTED:
            if not confirmed:
                return [Reply(
                    f"К сожалению, заявка {last.number} не была одобрена, но подать новую "
                    "вполне можно. Начать заново?",
                    kb.build([[kb._btn("Да, начинаем", "apply", {"category": category_key, "confirmed": True}, color="positive")],
                              [kb._btn("⬅️ Назад", "back"), kb._btn("✖️ Отмена", "cancel")]]),
                )]
            self.store.set_state(user, STATE_STEP, 1, {"category": category_key}, muted=False)
            return [Reply(self._question(FIELDS[0], category_key), kb.nav())]

        self.store.set_state(user, STATE_STEP, 1, {"category": category_key}, muted=False)
        return [Reply(self._question(FIELDS[0], category_key), kb.nav())]

    # ------------------------------------------------------------------ #
    def _duplicate_text(self, app: Application) -> str:
        """Ответ, когда заявка с аккаунта уже подана.

        Тон уважительный: человек ничего не сделал неправильно, просто правило
        одно — заявка с аккаунта принимается один раз.
        """
        cat = self.cfg.category(app.category)
        return (
            f"Здравствуйте! У вас уже есть заявка — номер **{app.number}**, "
            f"категория «{cat.title if cat else app.category}», "
            f"статус **{app.status}**.\n\n"
            "Мы принимаем одну заявку с аккаунта, поэтому вторую оформить не получится — "
            "иначе кураторам пришлось бы выбирать между вашими заявками. "
            "Посмотреть, что с заявкой, можно кнопкой «📄 Мой статус». "
            "Если нужно что-то изменить или отозвать её — напишите через "
            "«✉️ Вопрос организатору»."
        )

    def _question(self, field: Field, category_key: str) -> str:
        if field.key == "description":
            cat = self.cfg.category(category_key)
            if cat is None:
                return "Расскажите пару предложений 🙂"
            question = cat.prompt_question or "Расскажите пару предложений 🙂"
            if cat.prompt_hint:
                return f"{question}\nПригодится: {cat.prompt_hint}."
            return question
        if field.key == "full_name" and category_key == "volunteer":
            # Волонтёр — всегда один человек, группа или проект ему не нужны.
            return "Как вас зовут?"
        return field.question

    def _question_with_value(self, field: Field, data: dict) -> str:
        """Вопрос + уже введённое значение — чтобы человек видел, что возвращается назад.

        Контакты маскируются: чат группы публичный, полный телефон или почту
        второй раз выводить нельзя.
        """
        current = data.get(field.key)
        base = self._question(field, data.get("category", ""))
        if current:
            return f"{base}\nСейчас записано: {mask_contact(field.key, str(current))}"
        return base

    def _category_step(self, msg: Message, text: str, args: dict, data: dict,
                       attempts: dict, attachments: str) -> list[Reply] | None:
        user = msg.from_id
        key = str(args.get("category") or "")
        if key not in self.category_keys:
            key = self._guess_category(text or attachments)
        if key not in self.category_keys:
            if text.strip() and _is_scenario_keyword(text):
                # Похоже на вопрос про фестиваль, а не на попытку выбрать
                # категорию: отвечаем, но возможность подать заявку оставляем.
                replies = self._free_dialog(msg, text, STATE_CATEGORY, 0, data)
                replies.append(Reply("Если всё же хотите подать заявку — выберите категорию:",
                                     kb.category_menu(self.cfg.categories)))
                return replies
            return [Reply(
                "Не поняла, в какую категорию 🙂 Выберите, пожалуйста, кнопкой ниже.",
                kb.category_menu(self.cfg.categories),
            )]
        return self._begin_form(user, key)

    def _guess_category(self, text: str) -> str:
        from .config import CATEGORY_KEYWORDS

        low = text.lower()
        best, score = "none", 0
        for key, words in CATEGORY_KEYWORDS.items():
            if key not in self.category_keys:
                continue
            hits = sum(1 for w in words if w in low)
            if hits > score:
                best, score = key, hits
        return best if score >= 1 else ""

    def _field_nav(self, field: Field) -> dict:
        """Клавиатура для вопроса поля: у необязательных — кнопка «Пропустить»."""
        if field.key == "day":
            return kb.day_picker()
        return kb.nav(optional=field.optional)

    # ------------------------------------------------------------------ #
    def _form_step(self, msg: Message, step: int, text: str, args: dict, data: dict,
                   attempts: dict, attachments: str) -> list[Reply]:
        user = msg.from_id
        # Режим «Изменить»: ждём выбор поля по номеру или названию.
        if data.get("_edit_pick"):
            return self._handle_edit_pick(msg, text, data)
        if not 1 <= step <= len(FIELDS):
            return self._to_menu(user)
        field = FIELDS[step - 1]
        category_key = data.get("category", "")
        raw = attachments or text

        # Приватность в групповом чате — одна реплика перед персональными полями.
        hint_key = f"_hint_{field.key}"
        replies: list[Reply] = []
        if field.key in ("phone", "email") and self.cfg.ask_private_chat_hint and not data.get(hint_key):
            data[hint_key] = 1
            replies.append(Reply(
                "Слушайте, тут чат общий — телефон и почту лучше скинуть мне в личку, "
                "чтобы никто не видел 🙂 Если публичности не страшно — пишите здесь.",
                kb.nav(),
            ))

        if not raw:
            if replies:
                self.store.set_state(user, STATE_STEP, step, data, attempts=attempts)
            return replies + [Reply("Тут нужно ответить 🙂", self._field_nav(field))]

        # «Пропустить» для необязательных полей.
        if field.optional and clean_text(raw, 40).lower() in SKIP_WORDS:
            data[field.key] = ""
            return replies + self._advance_replies(user, data, step)

        result = self._validate(field, raw)
        if not result.ok:
            count = int(attempts.get(field.key, 0)) + 1
            attempts[field.key] = count
            self.store.set_state(user, STATE_STEP, step, data, attempts=attempts)
            if count >= MAX_ATTEMPTS and field.optional:
                data[field.key] = ""
                return replies + [Reply("Ладно, этот пункт оставим пустым 🙂")] \
                    + self._advance_replies(user, data, step)
            return replies + [Reply(self._error_text(field, result.error), self._field_nav(field))]

        attempts.pop(field.key, None)
        data[field.key] = result.value

        # Телефон и почту показываем в нормализованном виде — человек должен
        # видеть, что именно попадёт в заявку.
        echo = ""
        if field.key in ("phone", "email") and clean_text(raw, 60) != result.value:
            echo = f"Записала: {result.value}\n"

        if field.key == "phone":
            duplicate = self._phone_duplicate(user, result.value)
            if duplicate is not None:
                return replies + [Reply(
                    f"{echo}Похоже, номер уже есть в заявке {duplicate.number} 🙂 "
                    "Чтобы не было дублей, давайте оставим один. Всё так?",
                    kb.build([
                        [kb._btn("Всё так", "confirm_send", color="positive")],
                        [kb._btn("⬅️ Назад", "back"), kb._btn("✖️ Отмена", "cancel")],
                    ]),
                )]

        next_replies = self._advance_replies(user, data, step)
        if echo and next_replies:
            next_replies[0] = Reply(echo + next_replies[0].text, next_replies[0].keyboard)
        return replies + next_replies

    # ------------------------------------------------------------------ #
    # Режим «Изменить»: выбор поля номером или названием
    # ------------------------------------------------------------------ #
    def _enter_edit(self, user: int, data: dict, text: str = "") -> list[Reply]:
        """Входит в режим правки: сразу к полю, если текст распознан, иначе — запрос.

        ``data["_edit_mode"]`` оставляет до конца правки: как только поле
        заполнено (или пропущено), _advance_replies возвращает человека к
        сводке, а не к следующему полю анкеты.
        """
        data["_edit_mode"] = True
        field = self._pick_field_for_edit(text) if text and text.strip() else None
        if field is None:
            data["_edit_pick"] = True
            self.store.set_state(user, STATE_STEP, 1, data, muted=False)
            return [Reply(EDIT_PROMPT, kb.nav())]
        step = STEP_OF[field.key]
        self.store.set_state(user, STATE_STEP, step, data, muted=False)
        return [Reply(self._question_with_value(field, data),
                      self._field_nav(field))]

    def _handle_edit_pick(self, msg: Message, text: str, data: dict) -> list[Reply]:
        field = self._pick_field_for_edit(text)
        if field is None:
            return [Reply(
                "Не разобралась, какое поле 🙂 Напишите номер (1–8) или название: "
                "«имя», «город», «телефон», «почта», «описание», «ссылка», «фото», «день».",
                kb.nav(),
            )]
        data.pop("_edit_pick", None)
        step = STEP_OF[field.key]
        self.store.set_state(msg.from_id, STATE_STEP, step, data, muted=False)
        return [Reply(self._question_with_value(field, data),
                      self._field_nav(field))]

    def _pick_field_for_edit(self, raw: str) -> Field | None:
        """Распознаёт номер поля (1..len(FIELDS)) или название/синоним."""
        text = re.sub(r"[^\w\s@.-]", " ", str(raw or "").lower())
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
        if re.fullmatch(r"\d{1,2}", text):
            index = int(text)
            return FIELDS[index - 1] if 1 <= index <= len(FIELDS) else None
        best, best_score = None, 0
        for field in FIELDS:
            title = re.sub(r"\s+", " ", field.title.lower().replace("/", " ")).strip()
            if title in text or (len(text) >= 3 and text in title):
                return field
            for word in FIELD_SYNONYMS.get(field.key, ()):
                score = len(word) if word in text else (len(word) if len(text) >= 3 and text in word else 0)
                if score > best_score:
                    best, best_score = field, score
        return best

    def _phone_duplicate(self, user: int, phone: str) -> Application | None:
        if not self.cfg.store_pii_locally:
            return None
        from .security import blind_index

        digest = blind_index(phone, getattr(self.store, "_salt", ""))
        for app in self.store.find_by_phone_hash(digest):
            if app.user_id != user:
                return app
        return None

    def _validate(self, field: Field, raw: str) -> ValidationResult:
        if field.key == "full_name":
            return validate_name(raw)
        if field.key == "city":
            return validate_city(raw)
        if field.key == "phone":
            return validate_phone(raw)
        if field.key == "email":
            return validate_email(raw)
        if field.key == "description":
            return validate_description(raw, self.cfg.max_description_len)
        if field.key == "link":
            return validate_link(raw)
        if field.key == "attachment":
            if raw.startswith(("http", "www", "vk.", "t.me", "youtu")):
                return validate_link(raw)
            return ValidationResult(True, clean_text(raw, 500))
        if field.key == "day":
            value, _ = validate_day(raw)
            if value is None:
                return ValidationResult(False, error="day")
            return ValidationResult(True, value)
        return ValidationResult(True, clean_text(raw, 500))

    def _error_text(self, field: Field, error: str) -> str:
        table = {
            ("phone", "length"): "Хм, кажется, номер неполный 🙂 Напишите его ещё раз целиком, можно со знаком +.",
            ("phone", "prefix"): "Такого номера у нас в России не бывает 🙂 Перепроверьте, пожалуйста.",
            ("phone", "not_phone"): "Похоже, это не телефон 🙂 Напишите цифрами, например +7 916 123-45-67.",
            ("phone", "empty"): "Телефон нужен обязательно 🙂",
            ("email", "format"): "Похоже, в почте опечатка — глянь ещё раз 🙂",
            ("email", "empty"): "Почта нужна обязательно 🙂",
            ("full_name", "symbols"): "Без лишних символов — просто имя или название 🙂",
            ("full_name", "too_short"): "Напишите, пожалуйста, имя целиком 🙂",
            ("city", "symbols"): "Город можно просто назвать словом 🙂",
            ("link", "scheme"): "Ссылка не похожа на обычный адрес — пришлите, пожалуйста, адрес страницы целиком",
            ("link", "host"): "Не получилось разобрать адрес. Пришлите ссылку целиком 🙂",
            ("link", "format"): "Похоже, это не ссылка. Скопируйте её целиком 🙂",
            ("description", "too_short"): "Пара слов — и уже понятно 🙂",
        }
        if field.key == "day":
            return "Выберите день кнопкой ниже 🙂"
        return table.get((field.key, error), "Не поняла, можете чуть иначе сформулировать? 🙂")

    def _advance(self, user: int, data: dict, step: int) -> list[Reply]:
        return self._advance_replies(user, data, step)

    def _advance_replies(self, user: int, data: dict, step: int) -> list[Reply]:
        category_key = data.get("category", "")
        # В режиме «Изменить» после одного поля возвращаемся к сводке, а не
        # переспрашиваем остальные поля анкеты.
        if data.get("_edit_mode"):
            data.pop("_edit_mode", None)
            self.store.set_state(user, STATE_REVIEW, 0, data, muted=False)
            return [Reply(self._summary(data), kb.confirm())]
        if step >= len(FIELDS):
            self.store.set_state(user, STATE_REVIEW, 0, data, muted=False)
            return [Reply(self._summary(data), kb.confirm())]
        next_step = step + 1
        field = FIELDS[next_step - 1]
        self.store.set_state(user, STATE_STEP, next_step, data, muted=False)
        return [Reply(self._question(field, category_key), self._field_nav(field))]

    def _back(self, user: int, state: str, step: int, data: dict) -> list[Reply] | None:
        if state == STATE_CATEGORY:
            return [Reply("Куда дальше?", self._main_menu(user))]
        # «Назад» в режиме правки — вернуться к сводке без изменений.
        if state == STATE_STEP and data.get("_edit_mode"):
            data.pop("_edit_pick", None)
            data.pop("_edit_mode", None)
            self.store.set_state(user, STATE_REVIEW, 0, data, muted=False)
            return [Reply(self._summary(data), kb.confirm())]
        if state == STATE_STEP and step > 1:
            target = step - 1
            data.pop(f"_hint_{FIELDS[target - 1].key}", None)
            field = FIELDS[target - 1]
            self.store.set_state(user, STATE_STEP, target, data, muted=False)
            return [Reply(self._question_with_value(field, data),
                          self._field_nav(field))]
        if state == STATE_REVIEW:
            self.store.set_state(user, STATE_STEP, len(FIELDS), data, muted=False)
            field = FIELDS[-1]
            return [Reply(self._question_with_value(field, data),
                          self._field_nav(field))]
        return None

    def _resume(self, user: int, state: str, step: int, data: dict) -> list[Reply] | None:
        # Заявка с аккаунта одна. Если черновик остался, а живая заявка уже есть,
        # продолжать её бессмысленно: при отправке всё равно будет дубль.
        live = self.store.find_active_any(user)
        if live is not None:
            self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
            return [Reply(
                self._duplicate_text(live),
                kb.status_actions(has_application=True),
            )]
        if state == STATE_STEP and 1 <= step <= len(FIELDS):
            field = FIELDS[step - 1]
            filled = "\n".join(
                f"• {f.title} — {data[f.key]}"
                for f in FIELDS[:step - 1] if data.get(f.key)
            )
            head = f"Продолжаем. Мы остановились на шаге «{field.title}».\n"
            if filled:
                head += "Уже есть:\n" + filled + "\n"
            return [Reply(head + "\n" + self._question_with_value(field, data),
                          self._field_nav(field))]
        if state == STATE_REVIEW:
            return [Reply(self._summary(data), kb.confirm())]
        return None

    def _to_menu(self, user: int, first_contact: bool = False) -> list[Reply]:
        self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
        return [Reply(self._menu_text(user, first_contact), self._main_menu(user))]

    # ------------------------------------------------------------------ #
    def _summary(self, data: dict) -> str:
        cat = self.cfg.category(data.get("category", ""))
        lines = ["Посмотрите, всё так?", ""]
        lines.append(f"Категория — {cat.title if cat else data.get('category', '')}")
        for number, field in enumerate(FIELDS, start=1):
            value = data.get(field.key) or "—"
            lines.append(f"{number}. {field.title} — {value}")
        lines += [
            "",
            "Если что-то поправить — напишите номер поля (например, 4) "
            "или его название: «телефон», «почта», «город»… Всё верно — «Отправить».",
        ]
        return "\n".join(lines)

    def _review_step(self, msg: Message, text: str, args: dict, data: dict) -> list[Reply]:
        if not text:
            return [Reply("Всё так? Нажмите «Отправить» или «Изменить» 🙂", kb.confirm())]
        if is_affirmative(text):
            return self._submit(msg, data)
        # Попытка что-то поправить: «нет», «хочу изменить», номер поля или
        # название/синоним («почта», «город»…) — переходим в режим правки.
        if is_negative(text) or self._pick_field_for_edit(text) is not None:
            return self._enter_edit(msg.from_id, data, text)
        # Непонятный ответ на сводке: показываем её ещё раз, а не отмахиваемся —
        # человек почти всегда хочет ещё раз сверить, что вписано.
        return [Reply(
            self._summary(data) + "\n\nНе поняла, что нажать 🙂 «Отправить» — принять, "
            "«Изменить» — поправить поле.",
            kb.confirm(),
        )]

    # ------------------------------------------------------------------ #
    def _submit(self, msg: Message, data: dict) -> list[Reply]:
        user = msg.from_id
        category_key = data.get("category", "")
        missing = [
            f.title for f in FIELDS
            if not f.optional and f.key != "day" and not data.get(f.key)
        ]
        if not data.get("day"):
            missing.append(FIELDS[-1].title)
        if missing:
            self.store.set_state(user, STATE_STEP, 1, data, muted=False)
            return [Reply("Кажется, анкета не до конца заполнена. Начнём сначала 🙂", kb.nav())]

        payload = dict(data)
        payload["user_id"] = user
        payload["category"] = category_key
        try:
            app = self.store.create_application(payload, store_pii=self.cfg.store_pii_locally)
        except DuplicateApplication as exc:
            existing = self.store.get_by_number(exc.existing_number)
            if existing is not None:
                return [Reply(
                    self._duplicate_text(existing),
                    kb.status_actions(has_application=True),
                )]
            return [Reply(
                "Похоже, заявка с этого аккаунта уже подана. "
                "Посмотреть её можно кнопкой «📄 Мой статус».",
                kb.status_actions(has_application=True),
            )]

        self.store.audit("bot", "application.created", user, f"{app.number} {category_key}")

        # Режим STORE_PII_LOCALLY=false: в SQLite лежит только хеш телефона.
        # Реальные контакты нужны в Таблице и уведомлении организатору —
        # добавляем их в копию, которая не покидает этот вызов и не пишется в базу.
        contact = app
        if not self.cfg.store_pii_locally:
            contact = replace(
                app,
                phone=clean_text(payload.get("phone", ""), 40),
                email=clean_text(payload.get("email", ""), 120),
            )

        self._to_sheets(contact, msg.peer_id)
        self._notify_organizers(contact, msg.peer_id)

        cat = self.cfg.category(category_key)
        return [Reply(
            f"Готово, заявка у куратора 🙂\n"
            f"Номер заявки — **{app.number}**. Сохраните его, по нему можно спросить статус.\n"
            f"Что дальше: куратор «{cat.title if cat else category_key}» посмотрит анкету и "
            "напишет на указанную почту или позвонит. Обычно отвечаем в течение "
            "3–5 дней. Если одобрят — пришлём райдер, схему площадки и время слота.\n"
            "Проверить статус — кнопка «📄 Мой статус». Что-нибудь ещё подсказать?",
            kb.after_apply(),
        )]

    def _to_sheets(self, app: Application, peer_id: int) -> None:
        name = self._display_name(app.user_id)
        if self.sheets.ready:
            try:
                row = self.sheets.append(app, name)
                if row:
                    self.store.set_sheet_row(app.number, row)
                self.store.mark_notified(app.number)
                return
            except SheetsError as exc:
                self.store.audit("sheets", "error", app.user_id, str(exc)[:300])
                self._notify_organizers_raw(
                    f"⚠️ Не удалось записать заявку {app.number} в Google Таблицу: {exc}. "
                    "Заявка сохранена локально, продублируйте вручную."
                )
        self.csv.append(app, name)
        self.store.mark_notified(app.number)

    def _display_name(self, user_id: int) -> str:
        """Имя для таблицы и уведомлений; при пустом кэше — берём живое с VK.

        Раньше кэш наполнялся только по текстовым сообщениям не-админов, и в
        колонку «Имя аккаунта» падал голый ID: достаточно было отвечать
        кнопками или быть админом. Достаём один раз и кэшируем.
        """
        name = self.store.get_screen_name(user_id)
        if name:
            return name
        if self.vk is None:
            return str(user_id)
        try:
            data = self.vk.get_user(user_id)
            name = " ".join(filter(None, [
                str(data.get("first_name") or ""),
                str(data.get("last_name") or ""),
            ])).strip()
        except VKError:
            return str(user_id)
        if not name:
            return str(user_id)
        self.store.set_screen_name(user_id, name)
        return name

    def _notify_organizers(self, app: Application, peer_id: int) -> None:
        cat = self.cfg.category(app.category)
        lines = [
            "📨 Новая заявка",
            f"Номер: {app.number}",
            f"Категория: {cat.title if cat else app.category}",
            f"Имя: {app.full_name}",
            f"Город: {app.city}",
            f"Телефон: {app.phone or 'не указан'}",
            f"E-mail: {app.email or 'не указан'}",
            f"День: {app.day}",
        ]
        if app.description:
            lines.append(f"О себе: {app.description[:400]}")
        if app.link:
            lines.append(f"Ссылка: {app.link[:200]}")
        if app.attachment:
            lines.append(f"Файл: {app.attachment[:200]}")
        lines.append(
            "Ответьте «в ответ» на это сообщение: «Одобрена», "
            "«Отклонена причина» или просто комментарий.\n"
            "То же самое командой: !статус " + app.number + " Одобрена|Отклонена|На рассмотрении"
        )
        self._notify_organizers_raw("\n".join(lines))

    def _notify_organizers_raw(
        self, text: str, *, also_personal: bool = False, question_id: int = 0
    ) -> None:
        """Отправляет уведомление организаторам: сначала в беседу, лички — запасной.

        Основной канал — беседы из ``VK_ADMIN_PEER_IDS``. Если хотя бы в одну из
        них уведомление дошло, личные сообщения не трогаем: иначе организаторы
        получают одно и то же трижды. В лички ``VK_ADMIN_USER_IDS`` уходим
        только когда беседа недоступна сообществу (ошибка 917 «You don't have
        access to this chat» — сообщество не добавлено в беседу) или не задана.

        ``also_personal=True`` отключает это правило: сообщение уходит и в
        беседу, и в лички независимо от результата. Так отправляются
        экстренные оповещения — беседу организаторы могут не смотреть часами, а
        в личке человек увидит сообщение сразу.

        ``DRY_RUN=1`` не отправляет уведомления организаторам: без этого
        локальная проверка сценария уходит в боевую беседу и пугает людей
        сообщением «ЭКСТРЕННАЯ СИТУАЦИЯ». Ответы самому пользователю при этом
        отправляются как обычно.

        ``question_id`` — если задан, номер сообщения с вопросом в первой же
        удачной беседе запоминается. По нему узнаётся ответ: организатор пишет
        «в ответ» на это сообщение, и бот понимает, кому возвращать текст.
        """
        if self.cfg.dry_run:
            log.info("DRY_RUN: уведомление организаторам не отправлено: %s", text[:120])
            self.store.audit("bot", "notification.dry_run", 0, text[:200])
            return

        if not self.cfg.vk_admin_peers:
            log.warning(
                "Не задан VK_ADMIN_PEER_IDS — уведомление уйдёт только в лички "
                "организаторам: %s", text[:80],
            )

        chats: list[int] = []
        failed_chats: list[int] = []
        for peer in self.cfg.vk_admin_peers:
            sent_id = self._send(peer, text, None)
            if sent_id is not None:
                chats.append(peer)
                if question_id:
                    self.store.attach_admin_message(question_id, peer, sent_id)
            else:
                failed_chats.append(peer)
        if failed_chats:
            log.warning(
                "Не удалось отправить уведомление в беседу %s "
                "(917 — сообщество не состоит в беседе или чат запрещает писать; "
                "проверьте VK_ADMIN_PEER_IDS): %s",
                failed_chats, text[:80],
            )

        chats_done = bool(chats)
        # Беседа приняла уведомление — лишних копий в личках не нужно. Исключение
        # — экстренное оповещение, ему личная копия нужна всегда.
        if chats_done and not also_personal:
            log.info("Уведомление организаторам доставлено в беседу: %s", chats)
            self.store.audit("bot", "notification.delivered", 0, text[:200])
            return

        if not self.cfg.vk_admin_users:
            if chats_done:
                log.info(
                    "Уведомление организаторам доставлено в беседу: %s "
                    "(личных каналов нет, дублировать некуда)", chats,
                )
                self.store.audit("bot", "notification.delivered", 0, text[:200])
                return
            log.error(
                "Уведомление организаторам НИКУДА не доставлено: беседы недоступны, "
                "а VK_ADMIN_USER_IDS пуст: %s", text[:80],
            )
            self.store.audit("bot", "notification.failed", 0, text[:200])
            return

        personal: list[int] = []
        for user in self.cfg.vk_admin_users:
            if self._send(user, text, None) is not None:
                personal.append(user)
            else:
                log.warning(
                    "Личка %s недоступна (ошибка 901 — человек должен сначала "
                    "написать сообществу). Уберите его из VK_ADMIN_USER_IDS, "
                    "если он не должен получать уведомления.", user
                )
        if not personal:
            log.error(
                "Уведомление организаторам НИКУДА не доставлено: %s", text[:80]
            )
            self.store.audit("bot", "notification.failed", 0, text[:200])
            return
        if chats_done:
            log.info(
                "Экстренное уведомление доставлено в беседу %s и в лички %s",
                chats, personal,
            )
            self.store.audit("bot", "notification.delivered.urgent", 0, text[:200])
        else:
            log.info(
                "Беседа организаторов недоступна, уведомление доставлено в лички: %s",
                personal,
            )
            self.store.audit("bot", "notification.delivered.personal", 0, text[:200])

    # ================================================================== #
    # Сценарий 2. Статус
    # ================================================================== #
    def _main_menu(self, user: int) -> dict:
        """Главное меню с учётом того, что у человека уже есть заявка.

        Правило одной активной заявки видно сразу: кнопка «Подать заявку»
        исчезает, чтобы человеку не пришлось натыкаться на отказ.
        """
        apps = self.store.list_for_user(user) if self.store else []
        live = any(a.status != STATUS_REJECTED for a in apps)
        return kb.main_menu(self.cfg.categories, has_application=live)

    def _show_status(self, user: int) -> list[Reply]:
        apps = self.store.list_for_user(user)
        live = [a for a in apps if a.status != STATUS_REJECTED]
        if not apps:
            return [Reply(
                "Заявок пока нет 🙂 Если хотите поучаствовать — нажмите «🎸 Подать заявку», "
                "и мы заполним анкету вместе 🙂",
                kb.status_actions(),
            )]
        blocks: list[str] = []
        for app in apps[:5]:
            cat = self.cfg.category(app.category)
            hint = {
                STATUS_NEW: "Куратор посмотрит заявку и ответит на почту или позвонит.",
                STATUS_REVIEW: "Заявка у куратора, ждём решения.",
                STATUS_APPROVED: "Скоро пришлём детали: время слота, схему площадки, райдер.",
                STATUS_REJECTED: "Можно попробовать в следующем сезоне или приехать как гость.",
                STATUS_NEEDS_ANSWER: "Куратор ждёт ваш ответ — напишите его прямо здесь, "
                                     "я передам 🙂",
            }.get(app.status, "В работе.")
            blocks.append(
                f"**{app.number}** — {cat.title if cat else app.category}\n"
                f"Статус: **{app.status}**\n{hint}"
            )
        body = "\n\n".join(blocks)
        if len(apps) > 5:
            body += f"\n\nИ ещё {len(apps) - 5} заявок — показываю по пять последних."
        # Кнопку «Подать заявку» убираем, если заявка уже есть: иначе она
        # приведёт к отказу и только расстроит. «Мой статус» тоже не дублируем —
        # статус уже на экране.
        return [Reply(body, kb.status_actions(
            has_application=bool(live), show_status=False,
        ))]

    def set_status(self, actor: int, number: str, status: str, comment: str, peer_id: int) -> str:
        app = self.store.get_by_number(number)
        if app is None:
            return f"Заявка {number} не найдена."
        updated = self.store.set_status(app.number, status, comment)
        assert updated is not None
        self.store.audit("admin", "status.changed", app.user_id, f"{app.number} -> {status}")
        self._push_to_sheet(updated)
        for peer in self._participant_peers(app.user_id):
            self._send(peer, self._status_message(updated, comment), None)
        return f"Готово: {updated.number} → {updated.status}"

    def send_comment(self, actor: int, number: str, comment: str) -> str:
        """Комментарий куратора БЕЗ смены статуса.

        Отдельная команда нужна куратору, когда статус не меняется, а сказать
        участнику надо: раньше это было возможно только через ``!статус`` с
        повтором текущего статуса, и статус при этом переписывался заново.
        """
        app = self.store.get_by_number(number)
        if app is None:
            return f"Заявка {number} не найдена."
        # Статус не трогаем: меняется только комментарий.
        updated = self.store.set_status(app.number, app.status, comment)
        assert updated is not None
        self.store.audit("admin", "comment.sent", app.user_id, app.number)
        self._push_to_sheet(updated)
        for peer in self._participant_peers(app.user_id):
            self._send(peer, self._comment_message(updated, comment), None)
        return f"Комментарий отправлен: {updated.number} ({updated.status} не менялся)"

    def _take_expected_answer(self, msg: Message, text: str) -> list[Reply] | None:
        """Участник ответил на вопрос куратора — заявка возвращается в работу.

        Без этого шага статус «Нужен ответ» оставался мёртвой меткой: бот ждал
        ответа, но никто не узнавал, что он пришёл, и заявка навсегда висела
        «Нужен ответ». Теперь ответ сам закрывает ожидание, уходит координаторам
        и попадает в таблицу.

        Перехватываем только обычный текст в состоянии меню: кнопки и служебные
        команды — не ответ, а шаги заполнения анкеты относятся к своему сценарию.
        """
        if not text or msg.action or self.store is None:
            return None
        expected = [
            a for a in self.store.list_for_user(msg.from_id)
            if a.status == STATUS_NEEDS_ANSWER
        ]
        if not expected:
            return None
        app = expected[0]
        self.store.audit("participant", "answer.received", app.user_id, app.number)
        # Комментарий куратора остаётся: в нём сам вопрос, без него ответ
        # организаторам не к чему привязать.
        updated = self.store.set_status(app.number, STATUS_REVIEW, app.curator_comment)
        if updated is None:
            return None
        self._push_to_sheet(updated)
        asked = f"Вопрос куратора: {app.curator_comment}\n\n" if app.curator_comment else ""
        # Строка «Номер: ЗБ-NNNN» — та же метка, что и в карточке новой заявки,
        # поэтому ответ «в ответ» на это уведомление меняет статус заявки.
        self._notify_organizers_raw(
            f"↩️ Ответ участника по заявке\n"
            f"Номер: {updated.number}\n"
            f"Автор: {self._display_name(msg.from_id)} (id {msg.from_id})\n\n"
            f"{asked}{text}\n\n"
            f"Статус снова «На рассмотрении». Ответить: ответьте «в ответ» "
            f"на это сообщение — статус и комментарий уйдут участнику.",
        )
        return [Reply(
            "Приняла ответ и передала куратору 🙂 Если нужно что-то ещё — "
            "напишите, я рядом.",
            kb.status_actions(),
        )]

    def _push_to_sheet(self, app: Application) -> None:
        """Обновляет строку заявки в таблице, если строка известна."""
        if not (app.sheet_row and self.sheets.ready):
            return
        try:
            self.sheets.update_row(app, app.sheet_row)
        except SheetsError as exc:
            log.error("Не удалось обновить строку в таблице: %s", exc)

    #: Статусы, с которых начинается фраза куратора. Идут от длинных к коротким,
    #: иначе «на рассмотрении» распалось бы на «на» + мусор.
    _REPLY_STATUSES = (
        "на рассмотрении", "нужен ответ", "одобрена", "отклонена", "новая",
    )

    def apply_admin_text(self, actor: int, number: str, text: str) -> str:
        """Применяет свободный текст куратора к заявке.

        Куратор отвечает «в ответ» на уведомление о новой заявке и пишет
        обычной фразой: «Одобрена», «Отклонена площадка занята» или просто
        «перезвоним в пятницу». Если фраза начинается со статуса — статус
        меняется, остаток идёт комментарием; если нет — это просто комментарий,
        и статус остаётся прежним.
        """
        from .storage import normalize_status

        app = self.store.get_by_number(number)
        if app is None:
            return f"Заявка {number} не найдена."

        body = (text or "").strip()
        lowered = body.lower()
        for known in self._REPLY_STATUSES:
            if lowered.startswith(known):
                comment = body[len(known):].strip(" ,—–-")
                status = normalize_status(known)
                assert status is not None
                return self.set_status(actor, number, status, comment, 0)
        if not body:
            return (
                f"Поняла, что это ответ по заявке {app.number}, но текста нет 🙂 "
                f"Напишите статус или комментарий, например «Одобрена»."
            )
        return self.send_comment(actor, number, body)

    def _status_message(self, app: Application, comment: str) -> str:
        cat = self.cfg.category(app.category)
        who = cat.title if cat else app.category
        if app.status == STATUS_APPROVED:
            return self._with_comment(
                f"Здравствуйте! Ваша заявка **{app.number}** на участие одобрена 🎉\n"
                "Мы берём вас в программу. Напишу в ближайшие пару дней по деталям: "
                "время слота, схему площадки, требования к технике. "
                "Если что-то срочное — пишите сюда.",
                app, comment,
            )
        if app.status == STATUS_REJECTED:
            return self._with_comment(
                f"Спасибо, что подали заявку **{app.number}**. В этот раз куратор не смог "
                "включить её в программу — площадка заполнена.\n"
                "Это не отказ навсегда: можно попробовать в следующем сезоне или приехать "
                "как гость — вход свободный, а на «Мастер-береге» работает открытый "
                "микрофон, запись прямо на месте 🙂",
                app, comment,
            )
        if comment:
            return self._comment_message(app, comment)
        return f"Заявка **{app.number}** ({who}) сейчас в статусе «{app.status}». Ждём дальше 🙂"

    @staticmethod
    def _comment_message(app: Application, comment: str) -> str:
        return (
            f"Куратор оставил комментарий по заявке **{app.number}**: «{comment}». "
            "Ответьте, пожалуйста, в этом чате — я передам."
        )

    def _with_comment(self, text: str, app: Application, comment: str) -> str:
        """Дописывает комментарий куратора ПОСЛЕ основного текста.

        Раньше комментарий доходил только при промежуточных статусах: ветки
        «Одобрена» и «Отклонена» возвращали готовый текст и про ``comment``
        не знали. Куратор писал пояснение в таблице, а участник его не видел.
        """
        if not comment:
            return text
        return f"{text}\n\n{self._comment_message(app, comment)}"


    def withdraw_question(self, actor: int, raw_id: str) -> str:
        """Организатор отзывает вопрос: тема снята, отвечать не нужно.

        Отмена участником (кнопка «Отменить вопрос») означала «я передумал
        ждать», а здесь — «всё решили, вопрос закрыт». Раньше у организатора
        не было ни одной команды отмены: у участника кнопка есть, а у куратора
        вопрос можно было только закрыть ответом, даже когда тема снята.
        """
        raw = clean_text(raw_id, 20).strip(" №#Noo.-")
        if not raw.isdigit():
            return "Формат: !отмена N — N это номер вопроса из оповещения."
        qid = int(raw)
        row = self.store.question_by_id(qid, only_pending=True)
        if row is None:
            return f"Вопрос №{qid} не найден или уже закрыт."
        user = int(row["user_id"])
        self.store.mark_cancelled(qid)
        self.store.audit("admin", "question.withdrawn", user, f"#{qid} by {actor}")
        log.info("Вопрос #%s отозван организатором %s", qid, actor)
        # Участник ждал ответа и видел кнопку отмены — снимаем ожидание, иначе
        # он так и останется в «жду ответа» после того, как тема снята.
        self.store.set_state(user, STATE_MENU, 0, {}, muted=False)
        for peer in self._participant_peers(user):
            self._send(peer, "Тема снята — координатор её закрыл. "
                             "Если появятся вопросы, пишите, я рядом 🙂", None)
        self._notify_organizers_raw(
            f"↩️ Вопрос [вопрос {qid}] отозван координатором — отвечать не нужно.",
            question_id=qid,
        )
        return f"Вопрос №{qid} отозван, участник предупреждён."

    def _participant_peers(self, user_id: int) -> tuple[int, ...]:
        """Куда писать участнику: личные сообщения и беседа, где он общался."""
        peers: list[int] = [int(user_id)]
        last = self.store.last_peer(user_id)
        if last:
            peers.append(last)
        return tuple(dict.fromkeys(peers))

    # ================================================================== #
    # Сценарий 3. Вопрос организатору
    # ================================================================== #
    def _ask_org_finish(self, msg: Message, text: str) -> list[Reply]:
        user = msg.from_id
        if not text:
            return [Reply("Напишите вопрос, и я передам 🙂", kb.human_handoff())]
        self.store.set_state(user, STATE_HUMAN, 0, {}, muted=True)
        self.store.clear_context(user)
        # peer_id обязателен: без него ответ ушёл бы в личку, а человек,
        # спросивший в беседе, ждал бы его в беседе и не дождался бы.
        qid = self._forward_to_organizers(user, text, peer=msg.peer_id)
        return [Reply(
            "Записала и передала координатору. Как только придёт ответ, я напишу "
            "сюда сама 🙂 Пока можно вернуться к фестивалю или заявке.",
            kb.after_escalation(),
            mute=True,
            question_id=qid,
        )]

    def _forward_to_organizers(
        self, user: int, text: str, *, follow_up: bool = False, peer: int = 0
    ) -> int:
        """Отправляет вопрос организаторам и возвращает его номер.

        Номер попадает в текст уведомления как «Вопрос #N». Это и есть тот самый
        признак ответа: организатор может просто ответить на сообщение бота
        (тогда сработает и маркер, и id), а может написать «!ответ N текст», если
        вопросов много. Без номера ответ не к чему привязать.
        """
        action = "question.followup" if follow_up else "question.forwarded"
        self.store.audit("bot", action, user, text[:200])
        qid = self.store.create_question(user, peer or user, text)
        apps = self.store.list_for_user(user)
        context = ""
        if apps:
            app = apps[0]
            context = f"Участник с заявкой {app.number} ({app.category}), телефон {app.phone or '—'}"
        # Метка вопроса идёт первой строкой и в квадратных скобках: clean_text
        # не трогает такие символы, поэтому по ней всегда можно узнать, на что
        # пришёл ответ, даже если id сообщения в беседе не совпал.
        marker = f"[вопрос {qid}]"
        label = (
            f"✉️ Дополнение {marker}" if follow_up else f"✉️ Вопрос от участника {marker}"
        )
        self._notify_organizers_raw(
            f"{label}\n"
            f"Автор: {self._display_name(user)} (id {user})\n{context}\n\n{text}\n\n"
            f"Ответить: ответьте на это сообщение или напишите «!ответ {qid} текст».",
            question_id=qid,
        )
        return qid

    # ================================================================== #
    # Сценарий 4. Свободный диалог
    # ================================================================== #
    def _emoji_reply(self, user: int, text: str) -> list[Reply] | None:
        """Отвечает на голый смайлик, не звая модель.

        Смайлик — не мусор и не опечатка: человек отреагировал, и раньше он
        получал «похоже, сообщение получилось случайным», то есть отказ. Здесь
        короткая реплика по характеру и понятный следующий шаг. Модель не
        зовём: на одном знаке она тратит токены впустую, а ответ всё равно
        должен быть коротким.
        """
        from .security import is_emoji_only

        if not is_emoji_only(text):
            return None
        self.store.audit("bot", "text.emoji_only", user, text[:120])
        # Выбор варианта зависит только от самого смайлика, поэтому один и тот
        # же знак всегда получает одну и ту же реплику.
        index = sum(ord(ch) for ch in text.strip()) % len(EMOJI_REPLIES)
        return [Reply(EMOJI_REPLIES[index], self._main_menu(user))]

    def _meaningless_reply(self, user: int, text: str) -> list[Reply] | None:
        """Отвечает на бессмысленный ввод, не звая модель.

        Возвращает ``None``, если ввод осмысленный и его надо обрабатывать
        дальше. Модель на мусоре не просто отвечает медленно — она тратит
        токены впустую: замерено 15,5 с и все 400 токенов классификатора на
        сообщении, набранном в чужой раскладке, без единого ответа. Проверка
        локальная, поэтому токенов не стоит ничего.

        Отдельный случай — забытая раскладка. Это не мусор, а настоящий
        русский текст, поэтому вместо отказа бот показывает, как прочитал
        сообщение, и просит подтвердить: человеку полезно, а токены всё
        равно не тратятся.
        """
        from .security import is_meaningless, reads_as_russian

        decoded = reads_as_russian(text)
        if decoded:
            self.store.audit("bot", "text.wrong_layout", user, text[:120])
            log.info(
                "Сообщение от %s прочитано как русский текст в чужой раскладке: %r -> %r",
                user, text[:60], decoded[:60],
            )
            return [Reply(
                f"Кажется, сообщение набрано в другой раскладке 🙂 "
                f"Я прочитала так: «{decoded.strip()}».\n"
                f"Если я поняла верно — просто отправьте его ещё раз по-русски, "
                f"и я отвечу.",
                self._main_menu(user),
            )]
        if not is_meaningless(text):
            return None
        self.store.audit("bot", "text.meaningless", user, text[:120])
        log.info("Бессмысленное сообщение от %s, модель не вызываю: %r", user, text[:60])
        return [Reply(
            "Похоже, сообщение получилось случайным 🙂 "
            "Напишите, пожалуйста, вопрос ещё раз — или выберите, что вас "
            "интересует, кнопкой ниже.",
            self._main_menu(user),
        )]

    def _free_dialog(
        self, msg: Message, text: str, state: str, step: int, data: dict,
        first_contact: bool = False,
    ) -> list[Reply]:
        user = msg.from_id

        if not text:
            return [Reply(
                "Я тут 🙂 Рассказать о фестивале, принять заявку "
                "или передать вопрос организаторам?",
                self._main_menu(user),
            )]

        if text.strip().lower() in SKIP_WORDS:
            return [Reply("Ок 🙂", self._main_menu(user))]

        # Смайлик — это реакция, а не мусор: раньше человек на «😄» получал
        # отказ «сообщение получилось случайным».
        reaction = self._emoji_reply(user, text)
        if reaction is not None:
            return reaction

        # Мусор и забытая раскладка отсекаются локально, до модели: на таком
        # вводе она тратила до 15 с и все токены впустую, а ответа не давала.
        junk = self._meaningless_reply(user, text)
        if junk is not None:
            return junk

        intent, category = self._intent(text)

        if intent == "urgent":
            return self._emergency(user, text)

        if intent == "escalation" or needs_escalation(text):
            return self._escalate(msg, text)

        if intent == "apply":
            return self._start_apply(msg, {"category": category} if category in self.category_keys else {}, state)

        if intent == "status":
            return self._show_status(user)

        if intent == "ask_org":
            self.store.set_state(user, STATE_ASK, 0, {}, muted=False)
            return [Reply(
                "Конечно. Напишите вопрос одним сообщением или несколькими — соберу и передам.",
                kb.human_handoff(),
            )]

        if intent == "farewell":
            return self._farewell(user, offer=not is_pure_farewell(text))

        if intent == "greeting":
            return self._greeting(user, text, first_contact)

        if intent == "thanks":
            return self._farewell(user, offer=not is_pure_farewell(text))

        if intent == "chitchat":
            # Раньше на болтовню уходил голый смайлик: человек спрашивал, какой
            # у Ирины характер, и получал «😄» без единого слова. Теперь ответ
            # пишет модель — характер для этого и подключён.
            return self._llm_reply(msg, text, intent)

        # FAQ и всё остальное — через модель, со сверкой фактов.
        return self._llm_reply(msg, text, intent)

    # Тёплые прощания для двусмысленных фраз: человек вроде бы прощается, но
    # начал с приветствия — можно ненавязчиво оставить дверь открытой.
    _FAREWELLS_WARM = (
        "Рада, что помогла! Если появится ещё вопрос — пишите, я на связи 🙂",
        "Всегда пожалуйста 🙂 Если что-то ещё понадобится — обращайтесь!",
        "Пожалуйста! Остались вопросы про фестиваль — спрашивайте, я помогу 🙂",
        "Обращайтесь 🙂 Я на связи и помогу с заявкой или с вопросом команде.",
    )

    # Однозначное прощание: только попрощаться, ничего не предлагая.
    _FAREWELLS_SHORT = (
        "До связи! Хорошего дня 🙂",
        "Пока! Всего доброго 🙂",
        "Спасибо, что написали! До свидания 🙂",
        "Хорошо, до встречи 🙂",
    )

    #: Представление показывается один раз — при первом обращении.
    _INTRO = (
        "Привет! Я Ирина, помощник фестиваля «Звонкий берег» 🙂\n"
        "Рассказать о фестивале, принять заявку или передать вопрос организаторам?",
    )

    #: Повторные приветствия — без имени и роли, человек уже знает, кто перед ним.
    _WELCOME_BACK = (
        "С возвращением! 🙂 Чем займёмся?",
        "Рада снова видеть! Что подсказать?",
        "Здравствуйте! С возвращением 🙂 Что будем делать?",
    )

    #: Просьба не выдавать «Я Ирина» при возвращении — представление уже было.
    _NO_INTRO_RE = re.compile(r"я\s+ирина|меня\s+зовут|это\s+ирина|я\s+координатор")

    #: Приветствие в начале ответа модели. В середине разговора оно лишнее:
    #: человек не здоровался, и «Привет! Вам подходит...» выглядит так, будто
    #: переписку начали заново.
    _GREETING_OPENER_RE = re.compile(
        r"^\s*(?:привет|здравствуй\w*|добр(?:ое|ого)\s+(?:день|утро|вечер)|"
        r"добрый\s+день|хай|hello|hi)\b[^.!?\n]{0,24}"
        r"[!,.…—\-–]?\s*(?:\U0001F000-\U0001FAFF|[\U00002600-\U000027BF])?[\s!,.…]*",
        re.IGNORECASE,
    )

    def _strip_greeting(self, text: str) -> str:
        """Убирает приветствие, вклиненное моделью в начало ответа.

        Приветствие уместно при первом обращении и после перерыва. Если человек
        уже в разговоре, а модель всё равно начала с «Привет» или
        «Здравствуйте», это сбивает с толку, поэтому убираем. Ответ, который
        после этого остался пустым, не возвращаем: пустоты в чате хуже.
        """
        stripped = self._GREETING_OPENER_RE.sub("", text, count=1).strip()
        return stripped

    #: Представление, вклиненное моделью в ответ, — убираем.
    #: «Я Ирина, помощник фестиваля.», «Меня зовут Ирина…», «Я координатор…».
    _INTRO_PATTERNS = (
        re.compile(
            r"меня\s+зовут\s+ирина[^.!?\n]{0,30}[,.]?\s*", re.IGNORECASE
        ),
        re.compile(
            r"(?:это\s+)?(?:я\s+)?ирина\s*[,—:-]?\s*(?:ваш\s+|моя?\s+)?"
            r"(?:помощник\w*|координатор\w*|ассистент\w*|бот)\b"
            r"[^.!?\n]{0,40}[,.]\s*",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bя\s+(?:ваш\s+)?(?:помощник\w*|координатор\w*|ассистент\w*)\b"
            r"[^.!?\n]{0,30}[,.]\s*",
            re.IGNORECASE,
        ),
    )

    def _strip_intro(self, text: str) -> str:
        """Убирает самостоятельное представление из ответа модели.

        Представляться можно один раз — в начале общения. Если модель
        вклинила «Я Ирина, помощник фестиваля» в ответ на вопрос, это
        противоречит правилам и сбивает человека с толку.
        """
        if not self._NO_INTRO_RE.search(text.lower()):
            return text
        cleaned = text
        for _ in range(3):  # представлений бывает несколько подряд
            before = cleaned
            for pattern in self._INTRO_PATTERNS:
                cleaned = pattern.sub("", cleaned)
            if cleaned == before:
                break
        # Остатки вроде «Я , помощник» или «Привет! Я —» тоже убираем.
        cleaned = re.sub(r"\bя\s*[,—:-]\s*", "", cleaned)
        cleaned = re.sub(r"^\s*[,—:-]+\s*", "", cleaned).strip()
        if len(cleaned) < 8:
            return "Уточните, пожалуйста, что именно вас интересует 🙂"
        return cleaned[0].upper() + cleaned[1:]

    def _menu_text(self, user: int, first_contact: bool = False) -> str:
        """Текст при возврате в меню.

        Представление здесь неуместно: человек уже общался с ботом. Новому
        пользователю представление добавляется к первому ответу — в ``handle``.
        Первое нажатие «Начать» — как раз начало общения, поэтому в этом
        случае представление уместно, а «с возвращением» противоречиво.
        """
        if first_contact:
            return self._INTRO[0]
        return random.choice(self._WELCOME_BACK)

    def _farewell(self, user: int, offer: bool = True) -> list[Reply]:
        """Прощание.

        ``offer=True`` для двусмысленных фраз вроде «Здорово, спасибо»: там
        уместно ненавязчиво предложить рассказать что-то ещё. На однозначное
        «спасибо, до связи» новых услуг не предлагаем — человек закончил
        разговор.
        """
        if offer:
            text = f"{random.choice(self._FAREWELLS_WARM)}\n\nЧем ещё могу помочь? 🙂"
        else:
            text = random.choice(self._FAREWELLS_SHORT)
        return [Reply(text, self._main_menu(user))]

    def _greeting(self, user: int, text: str, first_contact: bool = False) -> list[Reply]:
        """Приветствие уже знакомого человека — формулировку пишет модель.

        Имя не называется: представление было в начале общения, и повторять его
        здесь неуместно. Если модель снова представилась или недоступна, берём
        готовый короткий вариант.
        """
        if first_contact:
            # Первое обращение: представление и есть ответ, «с возвращением»
            # тут противоречиво — человек вас ещё не видел.
            return [Reply(self._INTRO[0], self._main_menu(user))]
        if self.llm.available:
            messages = self.prompts.messages(
                [], text,
                extra_context="[СЛУЖЕБНО] Этот человек уже общался с тобой, ты уже "
                              "представилась. Поздоровайся коротко и спроси, чем помочь. "
                              "Не называй своего имени и не представляйся заново. Без ссылок.",
            )
            try:
                answer = self._guard_facts(self._ask_llm(messages=messages).text, text)
                if answer and not self._NO_INTRO_RE.search(answer.lower()):
                    return [Reply(answer, self._main_menu(user))]
                if answer:
                    log.info("Модель представилась повторно, беру готовый вариант")
            except LLMError as exc:
                log.warning("Модель недоступна, приветствую шаблоном: %s", exc)
        return [Reply(random.choice(self._WELCOME_BACK), self._main_menu(user))]

    def _intent(self, text: str) -> tuple[str, str]:
        from .security import is_urgent, wants_human

        low = text.lower()
        if is_urgent(text):
            return "urgent", "none"
        # Просьба передать — всегда передача, даже если в тексте «привет»
        # или «уточните возраст»: иначе модель принимает её за знакомство
        # и человек получает стартовую фразу вместо передачи.
        if wants_human(text):
            return "escalation", "none"
        # Прощание проверяем до модели: «здорово, спасибо» модель относит
        # к знакомству, и человек получает представление вместо прощания.
        if is_farewell(text):
            return "farewell", "none"
        if not self.llm.available:
            return self._intent_keywords(low)
        try:
            if self.throttle is not None:
                self.throttle.wait()
            result = self.llm.classify(text, self.prompts.classify_system())
            intent = result.get("intent", "other")
            allowed = {"apply", "about", "status", "ask_org", "greeting", "thanks",
                       "faq", "chitchat", "urgent", "escalation", "other"}
            return (intent if intent in allowed else "other"), result.get("category", "none")
        except LLMError:
            return self._intent_keywords(low)

    def _intent_keywords(self, low: str) -> tuple[str, str]:
        from .security import needs_escalation, wants_human

        if needs_escalation(low) or wants_human(low):
            return "escalation", "none"
        if any(w in low for w in ("заявк", "выступ", "поуч", "участ", "примут", "подать")):
            return "apply", "none"
        if any(w in low for w in ("статус", "одобри", "заявка на", "номер")):
            return "status", "none"
        if any(w in low for w in ("вопрос", "спросить", "написать орг", "связаться")):
            return "ask_org", "none"
        if any(w in low for w in ("привет", "здравству", "хай", "добрый")):
            return "greeting", "none"
        if any(w in low for w in ("спасибо", "благодар", "пока", "до связи")):
            return "thanks", "none"
        return "faq", "none"

    def _ask_llm(self, **kwargs):
        if self.throttle is not None:
            self.throttle.wait()
        return self.llm.chat(**kwargs)

    def _llm_reply(self, msg: Message, text: str, intent: str) -> list[Reply]:
        user = msg.from_id
        history = self.store.get_context(user, self.cfg.context_messages)
        messages = self.prompts.messages(
            history, text,
            extra_context="[СЛУЖЕБНО] Сейчас пользователь в состоянии главного меню, "
                          "анкета не заполняется. Отвечай по существу, в конце предложи "
                          "кнопку «Подать заявку», если речь об участии.",
        )
        try:
            reply = self._ask_llm(messages=messages)
        except LLMError as exc:
            # Пользователю нельзя говорить «через модель»: это внутреннее слово,
            # он не знает, что за ней стоит, и думает, что его сообщение ушло
            # в пустоту. Вместо извинения за устройство бота предлагаем то,
            # что реально сработает: спросить ещё раз или позвать человека.
            log.warning("Модель недоступна, отвечаю шаблоном: %s", exc)
            return [Reply(
                "Что-то не получается быстро ответить на этот вопрос 🙂 "
                "Попробуйте переспросить через минуту — или нажмите «Вопрос организатору», "
                "тогда мы передадим его организаторам, и они ответят лично.",
                self._main_menu(user),
            )]

        answer = self._guard_facts(reply.text, text)
        # Переписка уже идёт, а модель поздоровалась — «Привет» в начале ответа
        # выглядит так, будто человек заново начал диалог. Убираем. В путь
        # приветствия (_greeting) это не применяется: там человек поздоровался
        # сам, и ответ на приветствие уместен.
        answer = self._strip_greeting(answer)
        if not answer:
            log.info("Модель ответила одним приветствием, беру заготовку по теме")
            answer = ABOUT_TEXT.split("\n", 1)[-1].strip() if intent == "about" else (
                "Отвечаю по существу: программу и условия выкладываем в соцсетях "
                "фестиваля за две недели до мероприятия. Что ещё подсказать?"
            )
        menu = self._main_menu(user)
        # Сверяем названные кнопки с реальными: модель иногда предлагает
        # «Начать заново», которой в боте нет, и человек её не находит.
        answer = self._guard_buttons(answer, menu)
        self.store.add_context(user, "user", text, self.cfg.context_messages)
        self.store.add_context(user, "assistant", answer, self.cfg.context_messages)
        if intent == "about":
            return [Reply(answer + "\n\n" + ABOUT_TEXT.split("\n", 1)[-1], menu)]
        return [Reply(answer, menu)]

    # Слова, которые выдают выдуманный факт: конкретные имена артистов и даты.
    _BANNED = re.compile(
        r"\b(хедлайнер|выступит|заявят|гостем будет|спецгостем)[^.!?]{0,40}"
        r"\b\d{1,2}\s*(январ|феврал|март|апрел|мая|июн|июл|август|сентябр|октябр|ноябр|декабр)",
        re.IGNORECASE,
    )

    def _guard_facts(self, answer: str, question: str) -> str:
        """Убираем из ответа явно выдуманные артистов/даты и обещания одобрения."""
        text = answer
        for pattern, replacement in (
            (r"(?i)\b(вы будете (точно )?приняты|вас примут|гарантируем участие|одобрено|ваша заявка одобрена)\b",
             "заявку посмотрит куратор"),
            (r"(?i)\b(точные даты|фестиваль пройдёт)\s+\d{1,2}[.\-]\d{1,2}", "точные даты"),
        ):
            text = re.sub(pattern, replacement, text)
        if self._BANNED.search(text):
            text += (
                "\n\nТочную программу и состав участников публикуем в соцсетях "
                "фестиваля за две недели до мероприятия."
            )
        return strip_links(self._strip_intro(text.strip()))

    # Названия кнопок, которых в боте нет. Модель их сочиняет, человек потом
    # ищет кнопку, которой нет, и решает, что бот сломался.
    _INVENTED_BUTTON = re.compile(
        r"(?i)^(?:нач\w*(?:\s+заново|\s+сначала)?|заново|сначала|на\s+старт|старт\w*"
        r"|главн\w+|вернуться|вернитесь|назад|повторить"
        r"|заново\s+заполнить|заполнить\s+заново|подать\s+заявку\s+заново"
        r"|подать\s+заявку\s+сначала|начать\s+заново)$"
    )
    # Ссылка на кнопку: либо прямо названа как кнопка, либо стоит глагол нажатия.
    _BUTTON_REF = re.compile(
        r"(?i)(?P<lead>кнопк[аеуи]{0,2}\s+|нажм[ие]те\s+|нажима?й(?:те)?\s+|жмите\s+|"
        r"выберите\s+|откройте\s+)?«(?P<label>[^»]{2,40})»"
    )
    #: Подпись, которую предлагаем вместо выдуманной.
    _BUTTON_FALLBACK = "Подать заявку"

    @staticmethod
    def _plain(text: str) -> str:
        """Подпись без эмодзи, в нижнем регистре: «🎸 Подать заявку» → «подать заявку»."""
        without_emoji = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE)
        return re.sub(r"\s+", " ", without_emoji).strip().lower()

    @staticmethod
    def _keyboard_labels(keyboard: dict | None) -> list[str]:
        """Настоящие подписи кнопок — в том порядке, в каком идут на экране.

        Подпись лежит внутри ``action``: у VK-кнопки сверху ``action`` с
        ``type``/``label``/``payload``, а сверху только ``color``.
        """
        labels: list[str] = []
        for row in (keyboard or {}).get("buttons") or []:
            for button in row or []:
                action = button.get("action") or {}
                raw = str(action.get("label") or "").strip()
                clean = BotEngine._plain(raw)
                if clean:
                    labels.append(clean)
        return labels

    def _guard_buttons(self, answer: str, keyboard: dict | None) -> str:
        """Заменяет выдуманные кнопки на настоящую подпись из клавиатуры.

        Модель знает, что у бота есть меню, и охотно пишет «нажмите „Начать
        заново“», но такой кнопки нет — человек её не найдёт. Правило в промпте
        помогает, но не гарантирует: в длинном диалоге модель правила
        забывает. Поэтому сверяем ответ по факту — с тем, что реально
        приклеится к сообщению.

        Переписываются только два вида ссылок: те, что названы кнопкой или
        глаголом нажатия, и те, что совпали со словарём выдуманных кнопок.
        Остальные «ёлочки» не трогаем: в базе знаний площадки тоже в кавычках
        — «Колокол», «Теза», — и подменять их было бы грубой ошибкой.
        """
        real = self._keyboard_labels(keyboard)
        if not real:
            return answer
        # Основное действие — первая кнопка: в главном меню это «Подать заявку».
        primary = real[0].capitalize()
        known = set(real)

        def fix(match: re.Match[str]) -> str:
            label = match.group("label").strip()
            bare = self._plain(label)
            if not bare or bare in known:
                return match.group(0)
            # Просто цитата, а не кнопка: «Колокол» — это площадка, а не кнопка.
            if not match.group("lead") and not self._INVENTED_BUTTON.match(bare):
                return match.group(0)
            log.info(
                "Модель сослалась на несуществующую кнопку %r, заменяю на %r",
                label, primary,
            )
            # Глагол перед кнопкой («нажмите», «жмите») сохраняем: он объясняет
            # человеку, что делать, и без него фраза рассыпается.
            lead = match.group("lead") or ""
            return f"{lead}«{primary}»"

        return self._BUTTON_REF.sub(fix, answer)

    # ================================================================== #
    # Сценарий 5. Эскалация и ЧП
    # ================================================================== #
    def _escalate(self, msg: Message, text: str) -> list[Reply]:
        user = msg.from_id
        self.store.set_state(user, STATE_HUMAN, 0, {}, muted=True)
        self.store.audit("bot", "escalated", user, text[:200])
        qid = self.store.create_question(user, msg.peer_id, text)
        self._notify_organizers_raw(
            f"🚨 Передаю живому человеку [вопрос {qid}]\n"
            f"Автор: {self._display_name(user)} (id {user}), беседа {msg.peer_id}\n\n{text}\n\n"
            f"Ответить: ответьте на это сообщение или напишите «!ответ {qid} текст».",
            question_id=qid,
        )
        return [Reply(
            "Передала организаторам. Как только придёт ответ, я напишу здесь 🙂",
            kb.after_escalation(),
            mute=True,
            question_id=qid,
        )]

    def _convo_link(self, user_id: int) -> str:
        """Прямая ссылка на переписку с участником.

        Открывает диалог именно с этим человеком, минуя поиск по беседам: при
        ЧП счёт идёт на минуты, а прокликивать список диалогов руками — это
        потерянное время. Пустая строка, если id сообщества неизвестен: слать
        в оповещении «Беседа: https://vk.ru/gim0/…» хуже, чем не слать
        ничего.
        """
        if not self.cfg.vk_group_id:
            return ""
        return f"https://vk.ru/gim{self.cfg.vk_group_id}/convo/{user_id}"

    def _emergency(self, user: int, trigger: str = "") -> list[Reply]:
        """Срочное оповещение организаторам.

        В сообщении обязательно есть повод: кто написал, что именно и ссылка
        на профиль. Раньше сюда попадала только инструкция для посетителя, и
        организаторы получали «🚨🚨 ЭКСТРЕННАЯ СИТУАЦИЯ» без единого слова о
        том, что случилось — это выглядело как сбой бота, а не как ЧП.
        """
        text = (
            "Это важно. Пишите сюда и параллельно подойдите к инфо-столу у входа.\n"
            "1) Найдите волонтёра в жилете с логотипом фестиваля — он передаст сигнал по рации.\n"
            "2) Инфо-стол: там протокол поиска и описание.\n"
            "3) Команда оповещает все посты.\n"
            "Подключаю координатора."
        )
        if self.cfg.emergency_contacts:
            text += "\n\nЭкстренные контакты: " + ", ".join(self.cfg.emergency_contacts)

        apps = self.store.list_for_user(user)
        # Вопрос создаём здесь, а не только в _escalate: иначе у ЧП не было номера,
        # и организатор не мог ни ответить участнику («в ответ», «!ответ N»), ни
        # снять тему. Метка [вопрос N] — та же самая, что и у обычного вопроса.
        qid = self.store.create_question(user, 0, trigger.strip())
        lines = [
            f"🚨🚨 ЭКСТРЕННАЯ СИТУАЦИЯ [вопрос {qid}]",
            f"От кого: {self._display_name(user)} (id {user})",
            f"Профиль: https://vk.ru/id{user}",
        ]
        convo = self._convo_link(user)
        if convo:
            lines.append(f"Беседа: {convo}")
        if apps:
            app = apps[0]
            lines.append(f"Заявка: {app.number} ({app.category}), телефон {app.phone or '—'}")
        if trigger.strip():
            lines.append(f"Написал: {trigger.strip()[:300]}")
        else:
            lines.append("Написал: (сообщение не зафиксировано)")
        lines.append(
            f"Ответить участнику: ответьте «в ответ» на это сообщение "
            f"или напишите «!ответ {qid} текст». Отозвать: «!отмена {qid}»."
        )
        lines.append("")
        lines.append(text)
        # Экстренное оповещение идёт и в беседу, и в лички: беседу могут не
        # смотреть часами, а в личке организатор увидит сразу.
        self._notify_organizers_raw("\n".join(lines), also_personal=True, question_id=qid)
        self.store.audit("bot", "emergency", user, trigger[:200])
        # Гасим ожидание, иначе следующее сообщение участника уйдёт в обычный
        # диалог, а организаторы не получат «участник ответил» с номером.
        self.store.set_state(user, STATE_HUMAN, 0, {}, muted=True)
        return [Reply(text, kb.after_escalation(), mute=True)]

    # ================================================================== #
    # Команды организатора
    # ================================================================== #
    def _admin_command(self, msg: Message, text: str, args: dict, state: str, step: int,
                       data: dict, attempts: dict) -> list[Reply] | None:
        user = msg.from_id
        if not self.cfg.is_admin(user):
            # Не подтверждаем существование служебных команд.
            return [Reply("Не понимаю 🙂", self._main_menu(user))]
        self.store.audit("admin", "command", user, text[:200])
        parts = text.strip().lstrip("!/").split(maxsplit=2)
        if not parts:
            return [Reply("?", None)]
        command = parts[0].lower()
        if msg.args.get("action"):
            return None

        if command in {"статус", "status"}:
            from .storage import normalize_status

            # «!статус ЗБ-0001 Одобрена комментарий» — 4 части: команда, номер,
            # статус, комментарий (комментарий может содержать пробелы).
            rest = parts[1:] if len(parts) > 1 else []
            if len(rest) < 2:
                return [Reply("Формат: !статус ЗБ-0001 Одобрена [комментарий]", None)]
            number = rest[0].upper()
            status_part = " ".join(rest[1:])
            comment = ""
            for known in ("на рассмотрении", "нужен ответ", "одобрена",
                          "отклонена", "новая"):
                if status_part.lower().startswith(known):
                    # Комментарий читаем ДО обрезки status_part, иначе после
                    # status_part[:len(known)] комментарий всегда пустой.
                    comment = status_part[len(known):].strip(" ,—–-")
                    status_part = status_part[:len(known)]
                    break
            else:
                comment = status_part if len(rest) > 2 else ""
                status_part = rest[1]
            status = normalize_status(status_part)
            if status is None:
                return [Reply("Статусы: Новая, На рассмотрении, Одобрена, Отклонена, Нужен ответ.", None)]
            answer = self.set_status(user, number, status, comment, msg.peer_id)
            return [Reply(answer, None)]

        if command in {"коммент", "комментарий", "comment"}:
            # «!коммент ЗБ-0001 текст» — текст может содержать пробелы,
            # поэтому режем только после номера.
            rest = parts[1:] if len(parts) > 1 else []
            if len(rest) < 2 or not rest[1].strip():
                return [Reply("Формат: !коммент ЗБ-0001 текст комментария", None)]
            number = rest[0].upper()
            comment = " ".join(rest[1:]).strip()
            return [Reply(self.send_comment(user, number, comment), None)]

        if command in {"отмена", "отменить", "cancel"}:
            return [Reply(self.withdraw_question(user, parts[1] if len(parts) > 1 else ""), None)]

        if command in {"заявка", "заявки", "apps"}:
            stats = self.store.stats()
            return [Reply(
                "Статистика: " + ", ".join(f"{k}={v}" for k, v in stats.items()), None
            )]

        if command in {"помощь", "help"}:
            return [Reply(
                "Команды организатора:\n"
                "!статус ЗБ-0001 Одобрена|Отклонена|На рассмотрении|Нужен ответ [комментарий]\n"
                "!коммент ЗБ-0001 текст — написать участнику без смены статуса\n"
                "!ответ N текст — ответить участнику на вопрос N\n"
                "!отмена N — отозвать вопрос N: тема снята, отвечать не нужно\n"
                "!заявки — счётчики\n"
                "Можно и без команд: ответьте «в ответ» на карточку заявки "
                "или на вопрос — бот поймёт.",
                None,
            )]
        return [Reply("?", None)]

    # ================================================================== #
    # Отправка сообщений
    # ================================================================== #
    def _send(self, peer_id: int, text: str, keyboard: dict | None) -> int | None:
        if self.vk is None:
            log.info("[dry-run] → peer %s: %s", peer_id, text[:200])
            return None
        from .keyboards import sanitize_keyboard
        from .security import ALLOWED_ACTIONS

        safe = sanitize_keyboard(keyboard, ALLOWED_ACTIONS) if keyboard else None
        sent_id = self.vk.send(peer_id, text, keyboard=safe)
        # В переписке накапливаются меню, кнопки в которых пользователь так и не
        # нажал («Назад», «Отмена»). Держим только одно «живое» меню на диалог:
        # перед новым снимаем клавиатуру с прошлого (текст сохраняем).
        # Без клавиатуры следить нечего, поэтому такие сообщения не учитываем.
        if sent_id is not None and safe:
            self.retire_menu(peer_id)
            self._menu_msg[peer_id] = (int(sent_id), clean_text(text, 4000))
        # VK отдаёт id=0 для сообщений в беседу, и это успех, а не отказ:
        # проверять надо на None, а не на «ложивое» значение.
        return int(sent_id) if sent_id is not None else None

    def retire_menu(self, peer_id: int) -> None:
        """Убирает кнопки с прошлого сообщения-меню, не нажатого пользователем."""
        previous = self._menu_msg.pop(peer_id, None)
        if previous and self.vk is not None:
            self.vk.clear_keyboard(peer_id, previous[0], previous[1])

    @property
    def now(self) -> datetime:
        return now_dt()


# --------------------------------------------------------------------------- #
# Вспомогательное
# --------------------------------------------------------------------------- #
