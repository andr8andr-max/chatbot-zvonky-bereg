"""Клиент ВКонтакте: Long Poll API + отправка сообщений.

* Авторизация — токен сообщества (community token), он же идёт в groups.getLongPollServer.
* Никаких пользовательских токенов и паролей в коде.
* Все идентификаторы приводятся к int, все тексты чистятся bot.security.
* Сообщения, адресованные боту в беседе, обрабатываются по правилам Long Poll:
  сообщение из беседы приходит с префиксом «[club123|user456] » в тексте.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import Config
from .security import clean_text, redact

log = logging.getLogger("bot.vk")

VK_API_BASE = "https://api.vk.com/method"

#: peer_id беседы в VK начинается с 2000000000. Всё меньше — личный диалог.
_CHAT_PEER_MIN = 2_000_000_000

#: События, на которые подписан бот.
LP_EVENTS = {"message_new", "message_reply", "message_event", "group_join"}


class VKError(RuntimeError):
    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class LPUnavailable(VKError):
    """Long Poll недоступен по сети — сессия и ``ts`` в порядке.

    Отдельный класс нужен, чтобы цикл НЕ переподключался: новый
    ``groups.getLongPollServer`` означает новый ``ts``, а события, пришедшие
    пока сеть моргала, теряются навсегда. Из-за этого терялись нажатия
    кнопок — над кнопкой навсегда оставалось «колёсико».
    """


@dataclass
class Message:
    peer_id: int
    from_id: int
    text: str
    payload: dict[str, Any] = field(default_factory=dict)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    message_id: int = 0
    conversation_message_id: int = 0
    is_mentioned: bool = False
    from_chat: bool = False
    chat_only_mentions: bool = True
    #: Нажатие кнопки (message_event), а не новое сообщение.
    is_callback: bool = False
    #: Ответ на другое сообщение. По нему узнаётся ответ организатора на
    #: переданный вопрос: он отвечает прямо на сообщение бота в беседе.
    has_reply: bool = False
    reply_to_id: int = 0
    reply_to_conversation_message_id: int = 0
    reply_text: str = ""
    #: Сообщение написано человеком ОТ ИМЕНИ СООБЩЕСТВА (from_id = -группа,
    #: admin_author_id = человек). В VK это выглядит как реплика самого
    #: бота, поэтому такие сообщения раньше уходили в фильтр «свои».
    on_behalf: bool = False
    #: Отправило сообщение САМО сообщество (from_id = -группа), независимо от
    #: наличия admin_author_id. Нужен отдельно от ``on_behalf``, потому что
    #: разбор переписывает from_id на автора-человека, и исходный признак
    #: «это писало сообщество» после разбора уже не виден. По нему личный
    #: чат отличает собеседника от сообщества.
    from_community: bool = False

    @property
    def action(self) -> str:
        return str(self.payload.get("action") or "")

    @property
    def args(self) -> dict[str, Any]:
        data = self.payload.get("data")
        return data if isinstance(data, dict) else {}


class VKClient:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._token = cfg.vk_group_token
        self._version = cfg.vk_api_version
        self._timeout = 20.0
        self._retries = 3
        self._call_index = 0
        #: Разрешённые id сообщений: (peer_id, conversation_message_id) -> id.
        self._message_ids: dict[tuple[int, int], tuple[int, str]] = {}
        #: Приходит из groups.getLongPollServer. None — настройка неизвестна,
        #: тогда считаем её включённой (в беседе отвечаем только на обращение).
        self.chat_only_mentions: bool | None = None

    # ------------------------------------------------------------------ #
    def _call(self, method: str, params: dict[str, Any] | None = None, *, retried: bool = False) -> Any:
        data = dict(params or {})
        data["access_token"] = self._token
        data["v"] = self._version
        self._call_index += 1
        # VK не любит частые запросы без пауз — держим вежливый темп.
        if self._call_index % 20 == 0:
            time.sleep(0.34)

        url = f"{VK_API_BASE}/{method}"
        try:
            request = urllib.request.Request(
                url,
                data=urllib.parse.urlencode(data, doseq=True).encode("utf-8"),
                method="POST",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                    "User-Agent": "zv-bereg-bot/1.0",
                },
            )
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = redact(exc.read(1024).decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                pass
            if exc.code in (429, 500, 502, 503) and not retried:
                time.sleep(1.0 + random.random())
                return self._call(method, params, retried=True)
            raise VKError(f"{method}: HTTP {exc.code} {detail[:200]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if not retried:
                time.sleep(1.0 + random.random())
                return self._call(method, params, retried=True)
            raise VKError(f"{method}: {redact(str(exc))}") from exc

        if "error" in payload:
            err = payload["error"]
            code = err.get("error_code") if isinstance(err, dict) else None
            # 5/6 — токен сообщества без нужных прав, 27 — сообщество заблокировано.
            fatal = code in (5, 6, 7, 27, 28)
            if fatal or retried:
                raise VKError(
                    f"{method}: [{code}] {redact(str(err.get('error_msg', err)))}",
                    code=code if isinstance(code, int) else None,
                )
            time.sleep(0.6)
            return self._call(method, params, retried=True)
        return payload.get("response")

    # ------------------------------------------------------------------ #
    # Long Poll
    # ------------------------------------------------------------------ #
    def get_longpoll_server(self) -> tuple[str, str, str]:
        data = self._call("groups.getLongPollServer", {"group_id": self.cfg.vk_group_id})
        if not isinstance(data, dict):
            raise VKError("groups.getLongPollServer вернул неожиданный ответ")
        # Настройка «отвечать только на упоминания» приходит именно здесь, а не
        # в событиях message_new. Без неё бот молча выкидывает сообщения из бесед.
        if "chat_only_mentions" in data:
            self.chat_only_mentions = bool(data["chat_only_mentions"])
        try:
            return str(data["server"]), str(data["key"]), str(data["ts"])
        except KeyError as exc:
            raise VKError("в ответе groups.getLongPollServer нет server/key/ts") from exc

    @staticmethod
    def _longpoll_url(server: str) -> str:
        """Собирает адрес опроса из значения ``server``.

        VK отдаёт ``server`` уже готовым абсолютным URL
        (``https://lp.vk.com/whp/<group_id>``), поэтому схему добавлять нельзя:
        иначе хостом станет «https» и getaddrinfo упадёт. На всякий случай
        приводим и голое имя хоста, и «//host/...» к одному виду.
        """
        value = str(server or "").strip()
        if not value:
            raise VKError("пустой адрес Long Poll")
        if "://" in value:
            return value
        if value.startswith("//"):
            return "https:" + value
        return "https://" + value

    def poll(self, server: str, key: str, ts: str, wait: int) -> tuple[str, list[dict[str, Any]]]:
        # ls перечисляет события явно. Без него набор событий определяют
        # настройки сообщества, и нажатия кнопок (message_event) могут просто
        # не приходить — кнопка тогда крутит колёсико и ничего не делает.
        url = (
            f"{self._longpoll_url(server)}?act=a_check"
            f"&key={urllib.parse.quote(key)}&ts={urllib.parse.quote(ts)}&wait={wait}"
            f"&ls={urllib.parse.quote(','.join(sorted(LP_EVENTS)))}"
        )
        request = urllib.request.Request(url, method="GET", headers={"User-Agent": "zv-bereg-bot/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=wait + 15) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx — ответ самого VK: сессия невалидна, нужен новый ключ.
            raise VKError(
                f"Long Poll ответил HTTP {exc.code}", exc.code
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Обрыв сети или таймаут рукопожатия. Сессия цела, поэтому цикл
            # повторит тот же запрос с тем же ts и события не потеряются.
            raise LPUnavailable(f"Long Poll недоступен: {redact(str(exc))}") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # Обрезанный ответ — тоже повод повторить тот же опрос.
            raise LPUnavailable(f"Long Poll вернул не-JSON: {redact(str(exc))}") from exc
        if not isinstance(payload, dict):
            raise LPUnavailable("Long Poll вернул неожиданный ответ")
        new_ts = str(payload.get("ts", ts))
        # failed: 1 — потеряна часть событий, продолжаем с нового ts;
        # failed: 2/3 — ключ Long Poll больше недействителен, события за 10 минут
        # и более ранние потеряны. Возвращаем пустой ts: вызывающий код обязан
        # заново получить server/key/ts, иначе бот молча перестанет получать события.
        failed = int(payload.get("failed") or 0)
        if failed in (2, 3):
            log.warning("Long Poll: failed=%s, нужен новый ключ (%d событий потеряно)", failed, failed)
            return "", []
        if failed == 1:
            log.warning("Long Poll: failed=1, часть событий потеряна, продолжаем с ts=%s", new_ts)
        updates = payload.get("updates") or []
        if not isinstance(updates, list):
            updates = []
        return new_ts, [u for u in updates if isinstance(u, dict)]

    # ------------------------------------------------------------------ #
    # Отправка
    # ------------------------------------------------------------------ #
    def get_history(self, peer_id: int, count: int = 20) -> list[dict[str, Any]]:
        """Читает историю беседы. Нужна там, где Long Poll молчит.

        VK не отдаёт сообществу в Long Poll сообщения, написанные от его
        имени (from_id = -группа, admin_author_id = человек): в чате они
        неотличимы от собственных реплик бота. Такие беседы приходится
        дочитывать историей — для беседы, в которой сообщество состоит,
        метод доступен (проверено на 2000000001).
        """
        payload = self._call(
            "messages.getHistory", {"peer_id": int(peer_id), "count": int(count)}
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return []
        return [m for m in items if isinstance(m, dict)]

    def send(
        self,
        peer_id: int,
        text: str,
        *,
        keyboard: dict[str, Any] | None = None,
        random_id: int | None = None,
        strict: bool = False,
        reply_to: int | None = None,
        reply_conversation_id: int | None = None,
    ) -> int | None:
        """Отправляет сообщение.

        Ошибку по умолчанию только логирует: в боте нельзя ронять сценарий
        из-за того, что VK не принял сообщение. ``strict=True`` нужен
        диагностике, где важно узнать причину.

        ``reply_to`` — id сообщения, на которое отвечаем, а
        ``reply_conversation_id`` — его же номер внутри беседы. В беседе VK
        принимает только второй, в личке — только первый.
        """
        body = clean_text(text, 4000)
        if not body:
            return None
        params: dict[str, Any] = {
            "peer_id": int(peer_id),
            "message": body,
            "random_id": random_id or int(time.time() * 1000) % (2**31),
        }
        if reply_to or reply_conversation_id:
            params.update(
                self._reply_param(
                    int(peer_id), int(reply_to or 0), int(reply_conversation_id or 0)
                )
            )
        if keyboard:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        # Три попытки: текст VK принимает всегда, отклоняет он только
        # необязательные «в ответ» и кнопки. Повтор с тем же random_id
        # дубля не создаст.
        for _ in range(3):
            try:
                result = self._call("messages.send", params)
                return self._sent_ids(result)[0]
            except VKError as exc:
                if strict:
                    raise
                drop: str | None = None
                if exc.code == 912 and "keyboard" in params:
                    self._warn_chat_bot_once()
                    drop = "keyboard"
                elif exc.code == 100 and "conversation_message_id" in params:
                    # Не найдено сообщение для «в ответ». Текст он примет,
                    # поэтому теряем привязку, а не уведомление.
                    log.debug("Ответ «в ответ» не привязался, отправляю без него")
                    drop = "reply"
                elif exc.code == 100 and "reply_to" in params:
                    log.debug("reply_to не принят, отправляю без привязки")
                    drop = "reply"
                if drop is None:
                    log.error(
                        "Не удалось отправить сообщение в peer %s: %s",
                        peer_id, redact(str(exc)),
                    )
                    return None
                if drop == "keyboard":
                    params.pop("keyboard", None)
                else:
                    params.pop("conversation_message_id", None)
                    params.pop("reply_to", None)
        return None

    @staticmethod
    def _sent_ids(result: Any) -> tuple[int, int]:
        """Достаёт ``(id, conversation_message_id)`` из ответа ``messages.send``.

        Раньше id брался только из целого числа или списка. Настоящий ответ VK
        — словарь ``{"items": [...]}``, и он попадал в ``return None``: сообщение
        доставлялось, а бот считал, что отправки не было. Из-за этого не
        работали ``_menu_msg`` и ``retire_menu`` — кнопки с прошлых меню
        оставались на экране. В беседе ``id`` может быть 0, тогда нужен
        ``conversation_message_id``.
        """
        if isinstance(result, int):
            return result, 0
        if isinstance(result, list) and result and isinstance(result[0], int):
            return int(result[0]), 0
        if isinstance(result, dict):
            items = result.get("items")
            first: dict[str, Any] = {}
            if isinstance(items, list) and items and isinstance(items[0], dict):
                first = items[0]
            msg_id = int(first.get("id") or 0)
            conv = int(first.get("conversation_message_id") or 0) or int(
                result.get("conversation_message_id") or 0
            )
            return msg_id, conv
        return 0, 0

    def _reply_param(
        self, peer_id: int, message_id: int, conversation_message_id: int = 0
    ) -> dict[str, Any]:
        """Параметры ответа на сообщение.

        В личной переписке VK ждёт ``reply_to`` с настоящим ``id``. В беседе
        ``reply_to`` не работает вовсе — проверено на живом API: с числовым
        id приходит ``[100] forwarded message not found``. Там нужен
        ``conversation_message_id``, и он у нас есть, когда на событие
        пришёл объект сообщения, на которое организатор ответил.

        Если номера нет, подставляем ``reply_to``: подтверждение придёт без
        визуальной привязки «в ответ», но само уйдёт.
        """
        if peer_id >= _CHAT_PEER_MIN and conversation_message_id > 0:
            return {"conversation_message_id": int(conversation_message_id)}
        return {"reply_to": int(message_id)}

    _CHAT_BOT_WARNED = False

    def _warn_chat_bot_once(self) -> None:
        if VKClient._CHAT_BOT_WARNED:
            return
        VKClient._CHAT_BOT_WARNED = True
        log.warning(
            "Сообщество в режиме «Чат-бот»: VK не разрешает кнопки в личных "
            "сообщениях, поэтому сообщения уходят без кнопок. Чтобы вернуть "
            "кнопки: Управление сообществом → Сообщения → Настройки."
        )

    def send_to_many(self, peer_ids: tuple[int, ...], text: str, keyboard=None) -> int:
        sent = 0
        for peer in peer_ids:
            if self.send(peer, text, keyboard=keyboard) is not None:
                sent += 1
        return sent

    def edit(self, peer_id: int, message_id: int, text: str) -> None:
        try:
            self._call("messages.edit", {
                "peer_id": int(peer_id),
                "message_id": int(message_id),
                "message": clean_text(text, 4000),
            })
        except VKError:
            pass  # редактирование — необязательная операция

    def resolve_message_id(self, peer_id: int, conversation_message_id: int) -> int:
        """Настоящий ``id`` сообщения по ``conversation_message_id``.

        В личной переписке эти номера разные (у нас ``id`` на единицу больше),
        и ``messages.edit`` требует именно ``id``. Ошибка 8 «Invalid request»
        из-за подстановки чужого номера оставляла нажатую кнопку «крутиться».
        """
        return self._resolve(peer_id, conversation_message_id)[0]

    def _resolve(self, peer_id: int, conversation_message_id: int) -> tuple[int, str]:
        """Возвращает ``(id, текст)`` сообщения по ``conversation_message_id``."""
        if not conversation_message_id:
            return 0, ""
        cache_key = (int(peer_id), int(conversation_message_id))
        cached = self._message_ids.get(cache_key)
        if cached:
            return cached
        try:
            data = self._call("messages.getByConversationMessageId", {
                "peer_id": int(peer_id),
                "conversation_message_ids": str(int(conversation_message_id)),
            })
        except VKError as exc:
            log.info(
                "Не удалось найти сообщение %s в peer %s: %s",
                conversation_message_id, peer_id, redact(str(exc)),
            )
            return 0, ""
        items = data.get("items") if isinstance(data, dict) else None
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                found = (
                    int(item["id"]),
                    clean_text(item.get("text", ""), 4000),
                )
                if len(self._message_ids) > 200:
                    self._message_ids.clear()
                self._message_ids[cache_key] = found
                return found
        log.info(
            "Сообщение %s в peer %s не найдено (удалено?) — править нечего",
            conversation_message_id, peer_id,
        )
        return 0, ""

    def edit_message(
        self,
        peer_id: int,
        conversation_message_id: int,
        text: str | None = None,
        keyboard: dict[str, Any] | None = None,
    ) -> bool:
        """Обновляет сообщение с кнопками вместе с её клавиатурой.

        Текст и клавиатуру VK требует либо вместе, либо текст обязателен:
        запрос только с ``keyboard`` (даже пустым) он отклоняет ошибкой 100.
        Поэтому для снятия кнопок подставляем текущий текст сообщения.

        Пустая клавиатура убирает кнопки — так с нажатой кнопки исчезает и
        «колёсико» VK.
        """
        if not conversation_message_id:
            return False
        message_id, current_text = self._resolve(peer_id, conversation_message_id)
        if not message_id:
            return False
        body = clean_text(text, 4000) if text is not None else current_text
        if not body:
            # Без текста VK такой запрос отвергнет — снять кнопки нечем.
            log.info(
                "У сообщения %s нет текста, снять кнопки не получится", message_id
            )
            return False
        params: dict[str, Any] = {
            "peer_id": int(peer_id),
            "message_id": message_id,
            "message": body,
            "keyboard": json.dumps(
                keyboard if keyboard else {"buttons": []}, ensure_ascii=False
            ),
        }
        try:
            self._call("messages.edit", params)
            return True
        except VKError as exc:
            log.info(
                "Не удалось обновить сообщение %s в peer %s: %s",
                message_id, peer_id, redact(str(exc)),
            )
            return False

    def clear_keyboard(self, peer_id: int, message_id: int, text: str) -> bool:
        """Снимает кнопки с сообщения по его настоящему ``id``, текст сохраняя.

        Нужно для сообщений-меню, в которых пользователь ничего не нажал:
        иначе «Назад»/«Отмена» остаются в переписке навсегда.

        Текст передаёт вызывающий — он его знает на момент отправки. Читать
        сообщение обратно нельзя: ``messages.getById`` требует ``peer_id``
        XOR ``cmids`` и с нашим ``id`` не работает. Текст обязателен в том же
        запросе — иначе VK отвечает ошибкой 100.
        """
        if not message_id or not text:
            return False
        return self._edit_by_id(peer_id, int(message_id), clean_text(text, 4000), None)

    def _edit_by_id(
        self,
        peer_id: int,
        message_id: int,
        text: str,
        keyboard: dict[str, Any] | None,
    ) -> bool:
        params: dict[str, Any] = {
            "peer_id": int(peer_id),
            "message_id": int(message_id),
            "message": text,
            "keyboard": json.dumps(
                keyboard if keyboard else {"buttons": []}, ensure_ascii=False
            ),
        }
        try:
            self._call("messages.edit", params)
            return True
        except VKError as exc:
            log.info(
                "Не удалось обновить сообщение %s в peer %s: %s",
                message_id, peer_id, redact(str(exc)),
            )
            return False

    def set_typing(self, peer_id: int) -> bool:
        """Включает нативный индикатор «печатает…» у собеседника.

        VK держит статус 10 секунд и сам гасит его, когда сообщество
        отправляет сообщение, поэтому отдельная очистка не нужна. Метод
        возвращает ``False``, если сообщество не может им пользоваться
        (старый токен или запрет) — вызывающий код тогда просто не
        показывает индикатор.
        """
        try:
            self._call("messages.setActivity", {
                "group_id": int(self.cfg.vk_group_id),
                "peer_id": int(peer_id),
                "type": "typing",
            })
            return True
        except VKError as exc:
            log.debug("Индикатор «печатает…» недоступен для peer %s: %s", peer_id, redact(str(exc)))
            return False

    def delete(self, peer_id: int, message_id: int) -> bool:
        """Удаляет своё сообщение."""
        try:
            self._call("messages.delete", {
                "peer_id": int(peer_id),
                "message_ids": str(int(message_id)),
            })
            return True
        except VKError as exc:
            log.debug("Не удалось удалить сообщение %s: %s", message_id, redact(str(exc)))
            return False

    def get_conversation_members(self, peer_id: int) -> list[int]:
        try:
            items = self._call("messages.getConversationMembers", {
                "peer_id": int(peer_id), "count": 200,
            })
        except VKError:
            return []
        if not isinstance(items, list):
            return []
        return [int(i["user_id"]) for i in items if isinstance(i, dict) and str(i.get("user_id", "")).lstrip("-").isdigit()]

    def get_user(self, user_id: int) -> dict[str, Any]:
        try:
            items = self._call("users.get", {"user_ids": int(user_id), "fields": "screen_name,first_name,last_name"})
        except VKError:
            return {}
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return {}

    def add_to_chat(self, chat_id: int, user_id: int) -> bool:
        """Приглашает пользователя в беседу с ботом (нужно для личных сообщений)."""
        try:
            self._call("messages.add", {
                "chat_id": int(chat_id), "user_id": int(user_id), "random_id": int(time.time() * 1000) % (2**31),
            })
            return True
        except VKError:
            return False


# --------------------------------------------------------------------------- #
# Разбор события message_new
# --------------------------------------------------------------------------- #

def parse_message_event(
    event: dict[str, Any],
    cfg: Config,
    chat_only_mentions: bool | None = None,
) -> Message | None:
    obj = event.get("object") or {}
    if not isinstance(obj, dict):
        return None
    # Long Poll v3 присылает сообщение обёрнутым: object.message + object.client_info.
    # В v2 поля лежат прямо в object. Поддерживаем оба формата, иначе событие
    # приходит, но парсится в пустоту и бот молчит.
    inner = obj.get("message")
    if isinstance(inner, dict):
        obj = inner
    # Нажатие кнопки (message_event) устроено иначе: вместо from_id в object
    # лежит user_id, текста нет вовсе, а payload — строка с действием.
    # Без этой ветки from_id был бы 0, событие отбрасывалось, и кнопка
    # «крутила колёсико» до бесконечности.
    from_id = int(obj.get("from_id") or obj.get("user_id") or 0)
    peer_id = int(obj.get("peer_id") or 0)
    if not from_id or not peer_id:
        return None
    # Ответ администратора, написанный ОТ ИМЕНИ СООБЩЕСТВА. from_id тут равен
    # сообществу, а настоящий автор лежит в admin_author_id. В чате это
    # неотличимо от реплики бота, поэтому такие сообщения уходили в фильтр
    # «свои» и терялись. Считаем их сообщениями человека.
    admin_author = int(obj.get("admin_author_id") or 0)
    on_behalf = False
    # Признак «писало сообщество» запоминаем ДО перезаписи from_id: ниже
    # from_id может стать автором-человеком, и в личном чате уже нельзя
    # отличить сообщение сообщества от сообщения его собеседника.
    from_community = from_id == -cfg.vk_group_id or from_id == cfg.vk_group_id
    # «От имени сообщества» — это наличие admin_author_id, и знак from_id
    # тут ни при чём. VK отдаёт по-разному, и на этом стоял баг: в БЕСЕДЕ
    # сообщение от лица сообщества приходит с from_id = -группа, а в ЛИЧКЕ —
    # с from_id = id автеля со знаком «+» (проверено на живых событиях
    # 15:49 и 15:50). Проверка «from_id == -группа» в личке не срабатывала
    # никогда, и бот отвечал на реплику сообщества.
    if admin_author > 0:
        on_behalf = True
        if from_community:
            # В беседе from_id отрицательный — без перезаписи автор потерялся бы.
            from_id = admin_author
    # Сообщения самого сообщества и системные события — не наши.
    if from_id == -cfg.vk_group_id or peer_id == cfg.vk_group_id:
        return None
    if from_id < 0:
        return None

    text = clean_text(obj.get("text", ""), cfg.max_text_len + 200)
    payload_raw = obj.get("payload")
    payload: dict[str, Any] = {}
    if isinstance(payload_raw, str) and payload_raw:
        try:
            parsed = json.loads(payload_raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            payload = {}
    elif isinstance(payload_raw, dict):
        payload = payload_raw
    # У нажатия кнопки payload = {"button": "..."} устаревшего формата, а у
    # наших кнопок = {"action": ..., "data": {...}}. VK не присылает action
    # сам, поэтому ставим метку «это кнопка» — по ней движок поймёт, что
    # действие пришло с клавиатуры, даже если payload разбирается иначе.
    if payload and "action" not in payload:
        button = payload.get("button")
        if isinstance(button, str) and button:
            payload = {"action": button, **payload}

    attachments = [a for a in (obj.get("attachments") or []) if isinstance(a, dict)]
    is_callback = str(event.get("type") or "") == "message_event"
    # Ответ на сообщение приходит в reply_message — так организатор отвечает
    # на переданный вопрос. Без этих полей нечем узнать, на что именно он ответил.
    reply = obj.get("reply_message")
    reply = reply if isinstance(reply, dict) else {}
    reply_text = clean_text(reply.get("text", ""), cfg.max_text_len)
    return Message(
        peer_id=peer_id,
        from_id=from_id,
        text=text,
        payload=payload,
        attachments=attachments,
        has_reply=bool(reply),
        reply_to_id=int(reply.get("id") or 0),
        reply_to_conversation_message_id=int(reply.get("conversation_message_id") or 0),
        reply_text=reply_text,
        # У нажатия кнопки в object нет поля id — только conversation_message_id.
        # В личной переписке эти значения равны, в беседе id иной, поэтому
        # берём то, что прислали, а VKClient сам выберет нужный параметр.
        message_id=int(obj.get("id") or 0) or int(obj.get("conversation_message_id") or 0),
        conversation_message_id=int(obj.get("conversation_message_id") or 0),
        is_callback=is_callback,
        on_behalf=on_behalf,
        from_community=from_community,
        # Нажатие кнопки — это прямое обращение к боту, поэтому в беседе
        # с «отвечать только на упоминания» оно не должно отбрасываться.
        is_mentioned=bool(
            obj.get("is_mentioned") or payload.get("mention") or payload
        ),
        # Беседа в VK — это peer_id от 2000000000. Сравнивать с group_id
        # нельзя: ID пользователя бывает больше ID сообщества, и личная
        # переписка тогда ошибочно считалась бы беседой (и молчала).
        from_chat=peer_id >= _CHAT_PEER_MIN,
        # В событии message_new этого поля нет — оно приходит из
        # groups.getLongPollServer. Если настройка неизвестна, считаем её
        # включённой: в беседе лучше промолчать, чем заспамить.
        chat_only_mentions=(
            True if chat_only_mentions is None
            else bool(chat_only_mentions or obj.get("chat_only_mentions"))
        ),
    )


def attachment_summary(attachments: list[dict[str, Any]]) -> str:
    """Человекочитаемая сводка вложений для анкеты (без скачивания файлов).

    В событиях VK вложение лежит в ``att[type]``: фото — в ``att["photo"]``,
    документ — в ``att["doc"]``. Раньше код искал ``sizes`` на самом
    вложении, поэтому скинутое фото давало пустую строку, и на шаге
    «Фото / файл» бот отвечал «Тут нужно ответить.».
    """
    parts: list[str] = []
    for att in attachments[:3]:
        atype = str(att.get("type", ""))
        obj = att.get(atype)
        if not isinstance(obj, dict):
            obj = {}  # неузнанный формат: оставляем пустой сводкой, а не крашимся
        if atype == "photo":
            sizes = obj.get("sizes") or att.get("sizes") or []
            biggest = max(
                (s for s in sizes if isinstance(s, dict) and s.get("url")),
                key=lambda s: int(s.get("width") or 0) * int(s.get("height") or 0),
                default={},
            )
            url = str(biggest.get("url") or "")
            if url:
                parts.append(url)
        elif atype in {"doc", "audio", "video"}:
            link = (
                obj.get("url") or obj.get("link") or obj.get("title")
                or att.get("url") or att.get("link") or att.get("title") or ""
            )
            if link:
                parts.append(str(link))
    return " | ".join(p for p in parts if p)[:500]
