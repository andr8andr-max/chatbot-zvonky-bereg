"""Сборка приложения и цикл Long Poll."""

from __future__ import annotations

import json
import logging
import re
import signal
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from . import keyboards as kb
from .config import Config, self_check
from .flows import STATE_HUMAN, STATE_MENU, BotEngine, Reply
from .llm import ProxyLLM
from .prompts import PromptBuilder
from .security import (
    RateLimiterRegistry,
    Throttle,
    clean_text,
    is_injection_attempt,
    redact,
    register_secret,
)
from .sheets import CsvFallback, SheetsError, SheetsWriter
from .storage import Store, normalize_status
from .typing import TypingIndicator
from .vk_api import _CHAT_PEER_MIN, LPUnavailable, VKClient, VKError, parse_message_event

log = logging.getLogger("bot.app")

IDLE_STATES = {"apply_category", "apply_step", "apply_review", "ask_org"}

#: Как бот понимает, что его позвали в беседе.
_ADDRESS_MARKERS = ("@", "ирина", "ирин", "бот", "zv-bereg", "звонкий берег")

#: Служебные команды, которые администратор шлёт в беседе без обращения к боту.
#: Держим список рядом с _ADDRESS_MARKERS, чтобы новые команды нельзя было
#: случайно открыть «всем подряд» — фильтр беседы строгий.
_ADMIN_COMMANDS = frozenset({
    "статус", "status",
    "коммент", "комментарий", "comment",
    "заявка", "заявки", "apps",
    "помощь", "help",
    # «ответ» сюда НЕ входит намеренно: его разбирает _try_admin_answer, который
    # вызывается раньше фильтра беседы, и в списке он только превращал бы в
    # тревогу реплику вида «!ответственный за съёмку».
    "отмена", "отменить", "cancel",
})


#: Пометка на хэндлерах, которые ставит setup_logging. Нужна, чтобы убрать их
#: при повторном вызове: иначе каждый новый Application дублировал бы каждую
#: строку лога и держал второй открытый дескриптор на тот же файл.
_OWN_HANDLER = "_zvonky_bereg_handler"


class _RedactingFilter(logging.Filter):
    """Маскирует секреты и персональные данные в любой записи лога."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage(), mask_pii=True)
            record.args = ()
        except Exception:  # noqa: BLE001
            pass
        return True


def shutdown_logging() -> None:
    """Снимает и закрывает хэндлеры, установленные setup_logging.

    Нужна перед удалением каталога с логом (на Windows открытый файл блокирует
    очистку) и при нескольких Application в одном процессе.
    """
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _OWN_HANDLER, False)]:
        root.removeHandler(handler)
        handler.close()
    for handler in root.handlers:
        handler.filters = [f for f in handler.filters if not isinstance(f, _RedactingFilter)]


def setup_logging(cfg: Config) -> None:
    log_path: Path = cfg.log_file
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Повторный вызов не должен дублировать вывод и открывать файл второй раз.
    shutdown_logging()
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)-10s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(cfg.log_level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Гарантируем маскирование секретов во всех логах, включая чужие хэндлеры
    # (pytest, вызывающая сторона). Их фильтры тоже чистим в shutdown_logging.
    for handler in root.handlers:
        handler.addFilter(_RedactingFilter())
    setattr(console, _OWN_HANDLER, True)
    setattr(file_handler, _OWN_HANDLER, True)


class Application:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        register_secret(cfg.vk_group_token)
        register_secret(cfg.llm_api_key)
        setup_logging(cfg)

        self.store = Store(cfg.db_path, pii_salt=cfg.vk_group_token)
        self.llm = ProxyLLM(cfg)
        self.prompts = PromptBuilder(cfg)
        self.sheets = SheetsWriter(cfg)
        self.csv = CsvFallback(cfg.csv_export_path, cfg)
        self.vk = VKClient(cfg)
        self.engine = BotEngine(cfg, self.store, self.llm, self.prompts, self.sheets, self.csv, self.vk)
        self.limiter = RateLimiterRegistry(cfg.rate_limit_per_min, cfg.rate_limit_global_per_min)
        self.llm_throttle = Throttle(cfg.llm_min_interval_sec)
        self.engine.throttle = self.llm_throttle
        self._running = True
        self._last_sweep = time.monotonic()
        self._idle_sweep_interval = max(60, self.cfg.vk_lp_wait * 4)
        if self.cfg.sheets_poll_enabled:
            self._idle_sweep_interval = max(
                self._idle_sweep_interval, self.cfg.sheets_poll_interval_sec
            )
        self._closed = False
        #: Последний прочитанный id сообщения по каждой беседе.
        self._chat_cursor: dict[int, int] = {}
        #: Беседы, из которых история недоступна — чтобы не долбить их.
        self._chat_blocked: set[int] = set()
        self._last_chat_poll = 0.0
        self._chat_poll_interval = 12.0

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        """Освобождает базу и файлы лога. Повторный вызов безопасен.

        Владельцем ресурсов является Application, а не вызывающая сторона: пока
        sqlite-соединение и файл лога открыты, на Windows нельзя ни удалить, ни
        переименовать log-файл, а `python -m bot` может выйти с незакрытым
        соединением.
        """
        if self._closed:
            return
        self._closed = True
        self.store.close()
        shutdown_logging()

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def stop(self, *_args) -> None:
        self._running = False
        log.info("Остановка по сигналу…")

    def run(self) -> int:
        log.info("Запуск бота «Звонкий берег»")
        log.info("Конфигурация: %s", self.cfg.redacted())
        log.info("Модель: %s", self.llm.health)
        # Проверяем доступ к таблице сразу, а не на первой заявке: к этому
        # моменту заявка уже принята и организаторам пообещали, что она есть.
        log.info("Google Sheets: %s", self.sheets.verify())
        for warning in self_check(self.cfg):
            log.warning("Безопасность: %s", warning)
        log.info("Администраторов: %s", self.cfg.vk_admin_users or "не заданы!")
        if not self.cfg.vk_admin_users:
            log.warning(
                "Без VK_ADMIN_USER_IDS смену статуса выполнить нельзя — команда !статус "
                "будет отклонена у всех."
            )
        if self.cfg.dry_run:
            log.warning(
                "⚠️ DRY_RUN=1: уведомления организаторам НЕ отправляются (ни заявки, "
                "ни вопросы, ни ЧП). Бот отвечает посетителям как обычно. Не забудьте "
                "вернуть DRY_RUN=0, иначе заявки будут приходить «в никуда»."
            )

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):
                pass

        ts = ""
        failures = 0
        quiet_polls = 0
        while self._running:
            try:
                if not ts:
                    server, key, ts = self.vk.get_longpoll_server()
                    log.info("Long Poll подключён: %s", server)
                new_ts, updates = self.vk.poll(server, key, ts, self.cfg.vk_lp_wait)
                # Пустой ts от poll() означает «ключ протух» — следующая итерация
                # заново возьмёт server/key/ts. Присваиваем без `or ts`, иначе
                # старый ts продолжит используться и события пропадут.
                ts = new_ts
                failures = 0
                for event in updates:
                    self._on_event(event)
                # Пульс: иначе «бот молчит» невозможно отличить от «цикл встал».
                # Без событий Long Poll опрашивает вхолостую, и в логе пусто.
                quiet_polls = 0 if updates else quiet_polls + 1
                if quiet_polls and (quiet_polls == 1 or quiet_polls % 20 == 0):
                    log.info(
                        "Long Poll жив: %d опросов подряд без событий "
                        "(если человек пишет, а здесь тихо — "
                        "дело в настройках сообщества)", quiet_polls,
                    )
                now = time.monotonic()
                if now - self._last_chat_poll >= self._chat_poll_interval:
                    self._poll_chats()
                    self._last_chat_poll = now
                if now - self._last_sweep >= self._idle_sweep_interval:
                    self._idle_sweep()
                    self._sync_sheet_statuses()
                    self._last_sweep = now
            except LPUnavailable as exc:
                # Сеть моргнула, но сессия Long Poll жива. Сбрасывать ts нельзя:
                # новый groups.getLongPollServer означает новый ts, и события,
                # пришедшие за время обрыва, потерялись бы навсегда.
                failures += 1
                delay = min(15, failures)
                log.warning(
                    "Long Poll: сеть недоступна (попытка %d, пауза %d с), повторяю тот же опрос: %s",
                    failures, delay, redact(str(exc)),
                )
                self._sleep_unless_stopped(delay)
            except VKError as exc:
                # Сессия Long Poll недействительна — нужен новый ключ.
                failures += 1
                delay = min(60, 3 * failures)
                log.error(
                    "Ошибка ВКонтакте (попытка %d, пауза %d с): %s",
                    failures, delay, redact(str(exc)),
                )
                ts = ""
                self._sleep_unless_stopped(delay)
            except KeyboardInterrupt:
                break
            except Exception:  # noqa: BLE001
                log.exception("Непредвиденная ошибка в цикле")
                time.sleep(3)
        self.store.close()
        log.info("Бот остановлен")
        return 0

    def _sleep_unless_stopped(self, seconds: float) -> None:
        """Ждёт короткими шагами, чтобы Ctrl+C срабатывал сразу.

        ``stop()`` вызывается обработчиком сигнала в основном потоке, поэтому
        цикл с ``time.sleep(0.2)`` даёт ему шанс отработать между итерациями.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline and self._running:
            time.sleep(0.2)
        return 0

    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_own_event(event: dict) -> bool:
        """Собственное сообщение сообщества: from_id отрицательный."""
        obj = event.get("object")
        if not isinstance(obj, dict):
            return False
        inner = obj.get("message")
        if isinstance(inner, dict):
            obj = inner
        try:
            from_id = int(obj.get("from_id") or 0)
        except (TypeError, ValueError):
            return False
        try:
            admin_author = int(obj.get("admin_author_id") or 0)
        except (TypeError, ValueError):
            admin_author = 0
        # Реплика человека ОТ ИМЕНИ СООБЩЕСТВА: from_id отрицательный, но
        # автор-человек есть в admin_author_id. Это не «своё» сообщение —
        # в беседе они неотличимы от реплик бота, и раньше такие ответы
        # организатора молча терялись здесь.
        if from_id < 0 and admin_author > 0:
            return False
        return from_id < 0

    def _poll_chats(self) -> None:
        """Дочитывает беседы, из которых Long Poll не присылает события.

        Зачем: VK не отдаёт сообществу сообщения, написанные ОТ ЕГО ИМЕНИ
        (from_id = -группа, admin_author_id = человек). В чате они неотли��имы
        от реплик бота, но для организатора это его собственное сообщение —
        ответ на вопрос. Long Poll их молча не отдаёт, поэтому такие беседы
        читаем через messages.getHistory: для беседы, где сообщество
        состоит, метод доступен.
        """
        if not hasattr(self.vk, "get_history"):
            return
        for peer in self.cfg.vk_admin_peers:
            if peer < _CHAT_PEER_MIN or peer in self._chat_blocked:
                continue
            try:
                items = self.vk.get_history(peer, 20)
            except VKError as exc:
                # История недоступна. Для бесед, где сообщество не состоит,
                # остаётся единственный рабочий способ поймать маску «!ответ» —
                # читать по последним сообщениям с поиском (search) не работает
                # без запроса, а вот getByConversationMessageId может быть
                # бесполезен. Но периодически можно обрабатывать сообщения,
                # пришедшие за последнее время, через getHistory не выйдет.
                # Чтобы не молчать в таких беседах (Long Poll может не отдавать
                # сообщения, написанные не «в ответ»), просто отмечаем беседу
                # как блокированную для истории, но НЕ блокируем Long Poll —
                # он может прислать события «в ответ» сам.
                if exc.code in (15, 917, 927):
                    self._chat_blocked.add(peer)
                    log.debug(
                        "Беседа %s: история недоступна (%s), Long Poll — отдельно",
                        peer, exc.code,
                    )
                else:
                    log.warning("Беседа %s: история не прочитана — %s", peer, exc)
                continue

            if not items:
                continue
            newest = max(int(m.get("id") or 0) for m in items)
            last = self._chat_cursor.get(peer)
            if last is None:
                # Первый раз: запоминаем текущий край и не отыгрываем историю
                # — иначе после перезапуска бот ответил бы на старые вопросы.
                self._chat_cursor[peer] = newest
                log.info(
                    "Беседа %s: слежу за сообщениями с id %s "
                    "(историю до этого номера не отыгрываю)", peer, newest,
                )
                continue
            group = -abs(self.cfg.vk_group_id)
            for raw in reversed(items):
                mid = int(raw.get("id") or 0)
                if mid <= last:
                    continue
                self._chat_cursor[peer] = mid
                from_id = int(raw.get("from_id") or 0)
                if from_id == group and not int(raw.get("admin_author_id") or 0):
                    continue  # это сообщение самого бота
                self._on_event({
                    "type": "message_new",
                    # Свой event_id: повторный опрос той же беседы не приводит
                    # к повторной обработке, дубль отсекается в mark_seen.
                    "event_id": f"chat{peer}:{mid}",
                    "object": {"message": raw},
                })

    def _on_event(self, event: dict) -> None:
        etype = str(event.get("type") or "")
        if etype not in {"message_new", "message_reply", "message_event"}:
            return

        # Свои сообщения VK возвращает эхом (message_reply с out=1). Их надо
        # отбрасывать до проверки дублей, иначе база забивается эхом бота.
        if self._is_own_event(event):
            log.debug("Событие %s: собственное сообщение сообщества, пропускаем", etype)
            return

        # У нажатия кнопки event_id лежит внутри object, а не на верхнем
        # уровне события. Без этого дубль не отсекался и кнопка срабатывала
        # дважды после переподключения Long Poll.
        event_id = str(event.get("event_id") or "")
        if not event_id:
            obj = event.get("object")
            if isinstance(obj, dict):
                event_id = str(obj.get("event_id") or "")
        if event_id and self.store.mark_seen(event_id):
            log.info("Событие %s уже обработано, пропускаем дубль", event_id)
            return  # дубль события

        msg = parse_message_event(event, self.cfg, self.vk.chat_only_mentions)
        if msg is None:
            # Частая причина «бот молчит» — VK прислал событие, которое мы не
            # разобрали. Сырой JSON в логе позволяет понять структуру сразу.
            log.info(
                "Событие %s проигнорировано, сырой вид: %s",
                etype, json.dumps(event, ensure_ascii=False)[:500],
            )
            return

        log.info(
            "Входящее от %s в peer %s: %r (чат=%s, упоминание=%s)",
            msg.from_id, msg.peer_id, msg.text[:60],
            msg.from_chat, msg.is_mentioned,
        )
        if msg.action:
            log.info("Нажата кнопка %r от %s", msg.action, msg.from_id)

        # В личной переписке реагируем ТОЛЬКО на сообщения собеседника.
        # Сообщение, написанное сообществом (организатором от его лица или
        # самим ботом), входящим от пользователя не является. Признак берём
        # из from_community, а не из on_behalf: разбор события переписывает
        # from_id на автора-человека, но если admin_author_id не придёт,
        # on_behalf останется ложным и фильтр пропустит реплику сообщества.
        #
        # Отдельно заметим: peer_id == from_id НЕ признак своих заметок. В
        # личной переписке пользователя с сообществом peer_id тоже равен его
        # from_id (bot.vk_api: собеседник), так что такое правило отсекло бы
        # весь диалог с каждым посетителем.
        if not msg.from_chat and (msg.on_behalf or msg.from_community):
            log.info(
                "Личка: пропускаем сообщение от имени сообщества "
                "(peer=%s, автор=%s, on_behalf=%s)",
                msg.peer_id, msg.from_id, msg.on_behalf,
            )
            return

        # Ответ организатора на переданный вопрос. Разбираем ДО фильтра беседы:
        # в беседе с «отвечать только на упоминания» такой ответ отбрасывался
        # как обычное сообщение, и человек оставался без ответа. Стоял этот
        # вызов ниже фильтра — и не сработал ни разу.
        if self._try_admin_answer(msg):
            return

        # В беседе отвечаем только на прямое обращение или кнопку — иначе
        # бот превратится в спам-бота.
        if msg.from_chat and msg.chat_only_mentions and not (msg.is_mentioned or msg.action):
            # Служебные команды администратора — исключение: он пишет боту
            # лично, а не болтает в беседе, и «@бот» в служебной команде
            # не нужен. Без этого «!статус ЗБ-0001 Одобрена» в беседе
            # отбрасывалось как обычное сообщение, и куратор не видел ответа.
            if not self._is_addressed(msg) and not self._is_admin_command(msg):
                log.info(
                    "Пропущено сообщение из беседы от %s: нет обращения к боту", msg.from_id,
                )
                return

        if is_injection_attempt(msg.text):
            log.info("Попытка инъекции в сообщении от %s", msg.from_id)
            self.store.audit("bot", "injection.blocked", msg.from_id, msg.text[:150])

        allowed, retry_after = self.limiter.check(msg.from_id)
        if not allowed:
            log.info("Rate limit для %s", msg.from_id)
            self.engine._send(
                msg.peer_id,
                f"Слишком много сообщений подряд 🙂 Подождите {retry_after} сек и напишите снова.",
                None,
            )
            return

        # Имя запоминаем у всех без исключения: раньше админы и люди, отвечавшие
        # только кнопками, попадали в таблицу как «62568203» вместо имени.
        if not msg.action:
            self._remember_name(msg.from_id)

        try:
            with self._typing(msg):
                replies = self.engine.handle(msg)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка при обработке сообщения от %s", msg.from_id)
            self.engine._send(
                msg.peer_id,
                "Что-то у меня сломалось 🙂 Напишите ещё раз — попробую снова.",
                self.engine._main_menu(msg.from_id),
            )
            return

        # Нажатая кнопка в VK навсегда оставляет «колёсико» на том сообщении,
        # где она нажата: проверено, что messages.edit (текст и новая клавиатура)
        # индикатор НЕ снимает. Единственное, что гарантированно убирает кнопку
        # с экрана, — убрать её с самого сообщения, поэтому редактируем старое
        # сообщение без клавиатуры, а ответ с новой клавиатурой шлём отдельно.
        if msg.is_callback:
            pressed = msg.conversation_message_id or msg.message_id
            if pressed and self.vk.edit_message(msg.peer_id, pressed):
                log.info(
                    "Кнопки сняты с сообщения %s в peer %s после нажатия",
                    pressed, msg.peer_id,
                )
            # Прошлое меню, в котором кнопку не нажали, тоже убираем.
            self.engine.retire_menu(msg.peer_id)

        for reply in replies:
            if reply.text.strip():
                sent_id = self.engine._send(msg.peer_id, reply.text, reply.keyboard)
                # Запоминаем, на каком сообщении висит «Отменить вопрос»:
                # когда организатор ответит, кнопку надо будет снять именно оттуда.
                if reply.question_id and sent_id is not None:
                    self.store.attach_bot_message(reply.question_id, sent_id)

    # ------------------------------------------------------------------ #
    # Ответ организатора на вопрос участника
    # ------------------------------------------------------------------ #
    #: Команда администратора: «!ответ 12 Текст ответа».
    #:
    #: Пишут её по-разному, и раньше любая из форм кроме ровно «!ответ N»
    #: молча уходила в фильтр беседы — организатор видел, что ничего не
    #: произошло, и не понимал почему. Терпим к регистру, пробелу после «!»
    #: и «/» вместо «!», знаку номера («№», а clean_text превращает его в
    #: «No») и двоеточию после номера. Пустой текст ответа разбирается
    #: отдельно: организатору отвечают подсказкой, а не молчат.
    _ANSWER_CMD = re.compile(
        # «No» — это две буквы, поэтому пишем alternation, а не класс символов:
        # класс [№No] съедал только «N» и маска «!ответ No6 да» не разбиралась.
        r"^\s*[!/]\s*ответ\s*(?:№|No?)?\s*(\d+)\s*[:\-—.]?\s*(.*)$",
        re.IGNORECASE | re.DOTALL,
    )

    @classmethod
    def _parse_answer_cmd(cls, text: str):
        """Разбирает маску ответа. Возвращает ``(номер, текст)`` или ``None``."""
        m = cls._ANSWER_CMD.match(text or "")
        return (m.group(1), m.group(2).strip()) if m else None
    #: Метка вопроса в тексте уведомления организаторам: «[вопрос 12]».
    #: Именно квадратные скобки, а не «№»: clean_text превращает «№» в «No»,
    #: и в беседе метка выглядела бы иначе, чем её ищет регулярное выражение —
    #: ответ «в ответ» перестал бы узнаваться по запасному пути.
    _QUESTION_MARK = re.compile(r"\[вопрос\s*(\d+)\]", re.IGNORECASE)

    def _try_admin_answer(self, msg) -> bool:
        """Пробует трактовать сообщение как ответ на вопрос участника.

        Два способа ответить, оба работают:

        1. Ответить на сообщение бота с вопросом — обычный способ в беседе.
        2. Написать «!ответ N текст» — нужно, когда вопросов много или человек
           отвечает не «в ответ», а отдельным сообщением.

        Возвращает ``True``, если сообщение разобрано как ответ.
        """
        if not self.cfg.is_admin(msg.from_id) or msg.is_callback:
            return False
        if not msg.text.strip():
            return False

        looks_like_answer = msg.has_reply or self._parse_answer_cmd(msg.text) is not None
        if looks_like_answer:
            log.info(
                "Разбор ответа администратора %s в peer %s: reply=%s, "
                "id=%s, conv_id=%s, метка в тексте=%s",
                msg.from_id, msg.peer_id, msg.has_reply, msg.reply_to_id,
                msg.reply_to_conversation_message_id,
                self._QUESTION_MARK.search(msg.reply_text or "") is not None,
            )

        row, answer = self._match_question(msg)
        if row is None:
            # Ответ «в ответ» может относиться не к вопросу, а к уведомлению
            # о новой заявке: организатор пишет «Одобрена» прямо на нём.
            return self._try_admin_application_reply(msg)
        if not answer:
            # Маска без текста: вопрос найден, но ответить нечем. Молчать
            # здесь нельзя — организатор решит, что бот сломался.
            self.vk.send(
                msg.peer_id,
                f"Понял, что это ответ на вопрос №{int(row['id'])}, "
                f"но текста ответа в сообщении нет 🙂 "
                f"Напишите «!ответ {int(row['id'])} текст ответа» "
                f"или ответьте прямо на сообщение с вопросом.",
            )
            return True
        if not self.engine.answer_question(row, answer, msg.from_id):
            return False
        # Подтверждаем всем организаторам, что вопрос закрыт: уведомление о
        # вопросе уходило во все беседы и лички, значит и закрытие должно
        # быть видно везде — иначе в беседе, из которой не отвечали, висит
        # вопрос, на который уже дали ответ, и организаторы отвечают повторно.
        # Ответ участнику к этому моменту уже ушёл, поэтому сбой подтверждения
        # не должен его рушить.
        confirmation = (
            f"✅ Вопрос №{int(row['id'])} закрыт, ответ отправлен участнику.\n"
            f"Ответ: {clean_text(answer, 500)}"
        )
        peers = list(self.cfg.vk_admin_peers) or [msg.peer_id]
        for peer in peers:
            try:
                # Привязку «в ответ» ставим только там, откуда пришёл ответ:
                # в другой беседе она указывала бы на чужое сообщение.
                if peer == msg.peer_id and msg.has_reply and (
                    msg.reply_to_id or msg.reply_to_conversation_message_id
                ):
                    self.vk.send(
                        peer,
                        confirmation,
                        reply_to=msg.reply_to_id or None,
                        reply_conversation_id=msg.reply_to_conversation_message_id or None,
                    )
                else:
                    self.vk.send(peer, confirmation)
            except VKError:
                log.warning(
                    "Не удалось подтвердить закрытие вопроса №%s в peer %s",
                    row["id"], peer,
                )
        return True

    #: Метка в уведомлении о новой заявке: «Номер: ЗБ-0001». По ней узнаём,
    #: на какую заявку организатор ответил «в ответ», — сопоставлять по id
    #: сообщения в беседе нельзя (VK для сообщений сообщества его не отдаёт).
    _APPLICATION_MARK = re.compile(r"Номер:\s*(ЗБ-\d+)", re.IGNORECASE)

    def _try_admin_application_reply(self, msg) -> bool:
        """Ответ «в ответ» на уведомление о новой заявке.

        Организатор пишет на карточке заявки обычную фразу — «Одобрена»,
        «Отклонена площадка занята» или просто пояснение. Раньше такой ответ
        попадал в разбор ответа на вопрос, не находил метки и молчал: человек
        видел, что ничего не произошло, и статус оставался прежним.
        """
        if not msg.has_reply or not self.cfg.is_admin(msg.from_id):
            return False
        mark = self._APPLICATION_MARK.search(msg.reply_text or "")
        if not mark:
            return False
        number = mark.group(1).upper().replace("B", "Б")
        try:
            answer = self.engine.apply_admin_text(msg.from_id, number, msg.text)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось применить ответ по заявке %s", number)
            return False
        log.info(
            "Ответ «в ответ» по заявке %s от %s: %s", number, msg.from_id, msg.text[:120],
        )
        if msg.reply_to_id or msg.reply_to_conversation_message_id:
            self.vk.send(
                msg.peer_id, answer,
                reply_to=msg.reply_to_id or None,
                reply_conversation_id=msg.reply_to_conversation_message_id or None,
            )
        else:
            self.vk.send(msg.peer_id, answer)
        return True

    def _match_question(self, msg):
        """Ищет вопрос, на который пришёл ответ. Возвращает ``(вопрос, текст)``."""
        cmd = self._parse_answer_cmd(msg.text)
        if cmd is not None:
            number, answer_text = cmd
            row = self.store.question_by_id(int(number))
            if row is None:
                log.info(
                    "Маска «%s» от %s: вопрос №%s не найден в базе",
                    msg.text[:40], msg.from_id, number,
                )
                return None, ""
            # Пустой текст — не повод молчать: вопрос найден, вызывающий код
            # подскажет организатору, как дописать ответ.
            return row, answer_text

        if not msg.has_reply:
            return None, ""
        # Основной путь — метка «[вопрос N]» в тексте сообщения, на которое
        # ответили. Сопоставление по id в беседе ненадёжно: на messages.send
        # в беседу VK то отдаёт 0 вместо номера, то нормальный id, поэтому
        # опереться на него нельзя. По id ищем как запасной путь.
        mark = self._QUESTION_MARK.search(msg.reply_text or "")
        if mark:
            row = self.store.question_by_id(int(mark.group(1)))
            if row is not None:
                return row, msg.text
        for candidate in (msg.reply_to_id, msg.reply_to_conversation_message_id):
            if not candidate:
                continue
            row = self.store.question_by_admin_message(candidate)
            if row is not None:
                return row, msg.text
        log.info(
            "Ответ администратора %s в peer %s не связан с вопросом: метки нет",
            msg.from_id, msg.peer_id,
        )
        return None, ""

    # ------------------------------------------------------------------ #
    def _typing(self, msg) -> TypingIndicator | nullcontext:
        """Показывает системный индикатор «печатает…», пока бот думает.

        Это статус набора текста от VK (``messages.setActivity``), а не
        сообщение в чате, поэтому в переписке ничего не лишнее.

        Только для обычного текста: нажатие кнопки обрабатывается мгновенно,
        и мигание «печатает…» перед готовым ответом выглядит нелепо.
        """
        if msg.action or not msg.text.strip() or not self.cfg.typing_enabled:
            return nullcontext()
        return TypingIndicator(
            self.vk, msg.peer_id, delay=self.cfg.typing_delay_sec
        )

    # ------------------------------------------------------------------ #
    def _is_addressed(self, msg) -> bool:
        low = msg.text.lower()
        return any(marker in low for marker in _ADDRESS_MARKERS)

    def _is_admin_command(self, msg) -> bool:
        """Служебная команда администратора, даже без обращения к боту.

        Только для тех, кто есть в VK_ADMIN_USER_IDS, и только по известным
        командам: чужие «!статус …» в беседе по-прежнему молча игнорируются,
        а обычная реплика администратора с «!» не должна получать ответ.
        """
        if not self.cfg.is_admin(msg.from_id):
            return False
        parts = msg.text.strip().lstrip("!/").split(maxsplit=1)
        if not parts:
            return False
        return parts[0].lower() in _ADMIN_COMMANDS

    def _remember_name(self, user_id: int) -> None:
        try:
            data = self.vk.get_user(user_id)
        except VKError:
            return
        name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")])) or str(data.get("screen_name") or "")
        if name:
            self.store.set_screen_name(user_id, name)

    # ------------------------------------------------------------------ #
    def _sync_sheet_statuses(self) -> int:
        """Опрашивает Google Таблицу: статус мог поменять организатор.

        Таблица — основной способ работы куратора, поэтому бот сам забирает
        изменения и уведомляет участника. Повторов нет: строка в базе уже
        содержит применённый статус.
        """
        if not self.sheets.ready or not self.cfg.sheets_poll_enabled:
            return 0
        try:
            rows = self.sheets.read_statuses()
        except SheetsError as exc:
            log.warning("Не удалось прочитать статусы из таблицы: %s", exc)
            return 0
        applied = 0
        for number, raw_status, comment in rows:
            status = normalize_status(raw_status)
            if status is None:
                continue
            app = self.store.get_by_number(number)
            if app is None or app.status == status:
                continue
            try:
                self.engine.set_status(0, number, status, comment, app.user_id)
            except Exception:  # noqa: BLE001
                log.exception("Не удалось применить статус %s из таблицы", number)
                continue
            applied += 1
        if applied:
            log.info("Из таблицы применено новых статусов: %d", applied)
        return applied

    def _idle_sweep(self) -> int:
        """Напоминает о брошенных анкетах. Вызывается каждый цикл опроса."""
        sent = 0
        for row in self.store.idle_candidates(self.cfg.idle_timeout_min, IDLE_STATES, limit=20):
            user_id, step = int(row["user_id"]), int(row["step"])
            peer = int(row["last_peer"]) or user_id
            # Черновик не может стать заявкой, пока с аккаунта есть живая:
            # при отправке будет дубль. Напоминать не о чем — просто чистим.
            if self.store.find_active_any(user_id) is not None:
                self.store.set_state(user_id, STATE_MENU, 0, {}, False)
                self.store.audit(
                    "bot", "reminder.drop_stale", user_id,
                    "черновик сброшен: живая заявка уже есть",
                )
                continue
            field_name = _step_label(step)
            head = (
                f"Похоже, мы остановились на шаге «{field_name}» 🙂 "
                "Нажмите «Продолжить» — вернусь к тому же месту, "
                "или «Начать заново», чтобы заполнить анкету с начала."
                if field_name else
                "Мы остановились на середине 🙂 Нажмите «Продолжить» — вернусь к тому же месту, "
                "или «Начать заново», чтобы заполнить анкету с начала."
            )
            if self.engine._send(peer, head, kb.resume()) is None:
                # Чат недоступен: сообщество исключено, беседа удалена или человек
                # заблокировал бота. Раньше здесь стоял mark_reminded в любом случае,
                # и напоминание повторялось каждые 15 минут бесконечно — в лог шла
                # ошибка каждые 15 минут до скончания века. Дальше пробовать
                # бессмысленно, поэтому возвращаем человека в меню.
                log.warning(
                    "Напоминание в peer %s не доставлено — чат недоступен, "
                    "состояние возвращено в меню", peer,
                )
                self.store.set_state(user_id, STATE_MENU, 0, {}, False)
                self.store.audit("bot", "reminder.unreachable", user_id, f"peer {peer}")
                continue
            self.store.mark_reminded(user_id)
            sent += 1
        return sent

    def diagnose(self) -> None:
        log.info("=" * 60)
        log.info("Диагностика")
        log.info("  ProxyAPI: %s", self.llm.check_connection())
        log.info("  Sheets:   %s", self.sheets.verify())
        try:
            server, _, _ = self.vk.get_longpoll_server()
            log.info("  Long Poll: сервер %s доступен", server)
        except VKError as exc:
            log.info("  Long Poll: ОШИБКА %s", redact(str(exc)))
        self._diagnose_write_access()
        log.info("  Категорий: %d", len(self.cfg.categories))
        log.info("  Статистика: %s", self.store.stats())
        log.info("=" * 60)

    def _diagnose_write_access(self) -> None:
        """Проверяет, что токен умеет писать и беседы организаторов доступны.

        Long Poll работает и без права на сообщения, поэтому без этой проверки
        бот выглядит исправным, пока не попытается что-то ответить.

        Проверяем реальные адресаты (личку и беседы), а не сообщество: для
        сообщества ``peer_id`` положительный — это пользователь, поэтому
        «проверка прав» по своему же ID даёт ложную ошибку 901.
        """
        targets: list[tuple[str, int]] = [
            (f"личный чат с {uid}", uid) for uid in self.cfg.vk_admin_users
        ]
        targets += [
            (f"беседа {peer}" if peer >= _CHAT_PEER_MIN else f"личка {peer}", peer)
            for peer in self.cfg.vk_admin_peers
        ]
        if not targets:
            log.error(
                "  Отправка: не задано ни VK_ADMIN_USER_IDS, ни VK_ADMIN_PEER_IDS — "
                "уведомлять организаторов некуда."
            )
            return

        for label, peer in targets:
            try:
                sent = self.vk.send(
                    peer, "Проверка связи: бот Ирина на связи 🙂", strict=True
                )
            except VKError as exc:
                text = redact(str(exc))
                if "[917]" in text:
                    log.error(
                        "  %s: НЕДОСТУПЕН — сообщество не состоит в беседе. "
                        "Уведомления сюда не дойдут.", label,
                    )
                else:
                    log.error("  %s: ОШИБКА отправки — %s", label, text)
                continue
            if sent is None:
                log.error("  %s: сообщение не прошло (см. лог выше).", label)
            else:
                log.info("  %s: доступен для уведомлений (тест отправлен)", label)


def _step_label(step: int) -> str:
    from .flows import FIELDS

    if 1 <= step <= len(FIELDS):
        return FIELDS[step - 1].title
    return ""
