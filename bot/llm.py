"""Клиент ProxyAPI (OpenAI-совместимый формат).

Требования безопасности:
* только https, проверка TLS-сертификата;
* ключ передаётся исключительно в заголовке Authorization и никогда не логируется;
* все параметры запроса проходят через bot.security;
* таймауты, ограниченное число повторов, безопасный вывод ошибок.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import Config
from .security import clean_text, redact, sanitize_model_reply

log = logging.getLogger("bot.llm")

#: Коды, при которых повтор бессмысленен.
_FATAL_CODES = {400, 401, 402, 403, 404, 413, 422}

#: Ниже этого времени запрос делать нельзя: ответ не придёт раньше таймаута,
#: а человек всё равно не дождётся его вовремя. Лучше сразу ответить шаблоном.
_MIN_CALL_SEC = 1.0

#: Потолок для текста, который приходит от человека: в нём может быть мусор,
#: вставки и попытки подмены инструкций, и резать его нужно.
USER_MAX_LEN = 20000

#: Потолок для наших собственных системных промптов. Резать их нельзя: это
#: инструкции, а не пользовательский ввод. Раньше общий лимит 20000 срезал
#: половину промпта, и модель физически не видела правил, которые мы для неё
#: написали. Запас сверх реального размера — страховка от разумного роста файлов.
SYSTEM_MAX_LEN = 120000


class LLMError(RuntimeError):
    """Модель недоступна или ответил некорректно."""


class _Truncated(LLMError):
    """Модель потратила весь max_tokens на размышление и не дала текста.

    ling-3.0-flash — reasoning-модель. Короткий бюджет уходит на внутреннее
    «раздумье», и content приходит пустым с finish_reason == "length". Это не
    поломка модели, а нехватка места: повторяем запрос с увеличенным бюджетом.
    """


#: Потолок для повторного запроса. Выше смысла нет: длиннее ответ в чате
#: всё равно никто не прочитает, а время ожидания уже и так на пределе.
_MAX_TOKENS_CEILING = 4000

#: Во сколько раз подращиваем бюджет при обрезанном по длине ответе.
_GROWTH = 2

#: Сколько раз повторяем запрос с увеличенным бюджетом. Один повтор — осознанный
#: компромисс: на живом API пустой ответ из-за лимита встречается редко (лимит
#: 1500 перестал обрезать), а четыре попытки в худшем случае съедали бы до
#: 8500 токенов и 15 с ожидания на одно сообщение. Второй повтор уже не
#: окупается: если и 3000 токенов не хватило, лучше честно ответить шаблоном.
_TOKEN_RETRIES = 1


@dataclass
class LLMReply:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_finish: str = ""


class ProxyLLM:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._endpoint = cfg.llm_base_url + "/chat/completions"
        self._timeout = cfg.llm_timeout
        self._retries = cfg.llm_retries
        self._model = cfg.llm_model
        self._disabled = not cfg.llm_enabled
        self._last_error = ""
        self._ok_calls = 0
        self._fail_calls = 0
        # Бюджет времени на одно сообщение человека. На сообщение модель
        # зовётся несколько раз (определить намерение, потом ответить), и
        # каждая попытка отдельно живёт до llm_timeout с повторами. Суммарно
        # это превращалось в полминуты тишины, во время которых у человека
        # висел индикатор «печатает…». Теперь общий лимит один на сообщение.
        self._budget = max(_MIN_CALL_SEC, float(cfg.llm_budget_sec))
        self._deadline: float | None = None
        self._started = 0.0

    # ------------------------------------------------------------------ #
    def begin_message(self) -> None:
        """Открывает бюджет времени на обработку одного сообщения.

        Вызывается движком на входе в обработку сообщения. Дальше каждый
        запрос к модели берёт время из этого бюджета: и таймаут запроса, и
        паузу перед повтором. Когда бюджет исчерпан, запрос не отправляется
        вовсе — вызывающий код получает ``LLMError`` и отвечает шаблоном.
        """
        self._started = time.monotonic()
        self._deadline = self._started + self._budget

    def _left(self) -> float:
        """Сколько секунд ещё можно ждать."""
        if self._deadline is None:
            return self._timeout
        return self._deadline - time.monotonic()

    def _elapsed(self) -> float:
        return 0.0 if not self._started else time.monotonic() - self._started

    # ------------------------------------------------------------------ #
    @property
    def available(self) -> bool:
        return not self._disabled and bool(self.cfg.llm_api_key)

    @property
    def health(self) -> str:
        if self._disabled:
            return "выключен (LLM_ENABLED=false)"
        if self._fail_calls and self._ok_calls == 0:
            return f"ошибки: {self._last_error}"
        return f"ok (вызовов: {self._ok_calls}, ошибок: {self._fail_calls})"

    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        model: str | None = None,
    ) -> LLMReply:
        if not self.available:
            raise LLMError("LLM выключен или не задан LLM_API_KEY")

        body = {
            "model": model or self._model,
            "messages": [
                {
                    "role": m.get("role", "user"),
                    # Свой системный промпт не режем: обрезанные инструкции
                    # хуже, чем длинные, — модель их просто не увидит.
                    "content": clean_text(
                        m.get("content", ""),
                        SYSTEM_MAX_LEN if m.get("role") == "system" else USER_MAX_LEN,
                    ),
                }
                for m in messages
            ],
            "temperature": self.cfg.llm_temperature if temperature is None else temperature,
            "max_tokens": min(max_tokens or self.cfg.llm_max_tokens, _MAX_TOKENS_CEILING),
            "stream": False,
        }

        # Бюджет токенов, с которым делаем запрос. При обрезанном по длине
        # ответе он растёт: reasoning-съеденный бюджет — это пустой ответ,
        # а не ошибка, и повтор с тем же лимитом снова ничего не даст.
        token_budget = int(body["max_tokens"])
        # Счётчики раздельные: расширение бюджета токенов не должно отнимать
        # повторы, которые нужны для сети.
        token_retries = 0
        net_retries = 0

        last_error = "неизвестная ошибка"
        while True:
            body["max_tokens"] = token_budget
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            # Бюджет может кончиться между попытками: ждать дальше нельзя,
            # человек уже смотрит на индикатор «печатает…».
            left = self._left()
            if left < _MIN_CALL_SEC:
                last_error = self._budget_exhausted()
                log.warning("Бюджет времени на ответ исчерпан, запрос не отправляю: %s", last_error)
                break
            request = urllib.request.Request(
                self._endpoint,
                data=payload,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.cfg.llm_api_key}",
                    "User-Agent": "zv-bereg-bot/1.0",
                },
            )
            try:
                # Таймаут запроса никогда не больше остатка бюджета, иначе
                # один запрос съест всё время целиком.
                with urllib.request.urlopen(request, timeout=min(self._timeout, left)) as response:
                    raw = response.read()
                data = json.loads(raw.decode("utf-8"))
                return self._parse(data)
            except _Truncated as exc:
                # Весь бюджет ушёл на размышление. Растим его и повторяем:
                # иначе человек получит «не могу ответить» вместо ответа,
                # хотя модель доступна и отвечает.
                last_error = str(exc)
                if token_retries >= _TOKEN_RETRIES or token_budget >= _MAX_TOKENS_CEILING:
                    self._fail_calls += 1
                    log.error(
                        "Бюджет токенов исчерпан, повторяем не больше %s раз: %s",
                        _TOKEN_RETRIES, last_error,
                    )
                    raise LLMError(last_error) from exc
                token_retries += 1
                spent = token_budget
                token_budget = min(token_budget * _GROWTH, _MAX_TOKENS_CEILING)
                log.warning(
                    "Модель потратила весь бюджет %s токенов на размышление, "
                    "повторяю с бюджетом %s", spent, token_budget,
                )
                if self._left() < _MIN_CALL_SEC:
                    self._fail_calls += 1
                    raise LLMError(f"{last_error} (и времени на повтор не осталось)") from exc
                continue
            except urllib.error.HTTPError as exc:
                detail = self._safe_error(exc)
                last_error = f"HTTP {exc.code}: {detail}"
                self._fail_calls += 1
                if exc.code in _FATAL_CODES or net_retries >= self._retries:
                    log.error("ProxyAPI вернул ошибку: %s", redact(last_error))
                    raise LLMError(last_error) from exc
                net_retries += 1
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"сеть недоступна: {redact(str(exc))}"
                self._fail_calls += 1
                if net_retries >= self._retries:
                    log.error("Сеть недоступна: %s", last_error)
                    raise LLMError(last_error) from exc
                net_retries += 1
            except (ValueError, KeyError, TypeError) as exc:
                self._fail_calls += 1
                log.error("Некорректный ответ модели: %s", redact(str(exc)))
                raise LLMError("модель вернула нечитаемый ответ") from exc

            # Повтор, но не ценой тишины: пауза обрезается остатком бюджета.
            nap = min(8.0, (2 ** net_retries) + random.random())
            if nap > self._left() - _MIN_CALL_SEC:
                last_error = self._budget_exhausted()
                log.warning(
                    "Бюджет времени на ответ исчерпан, повтор не делаю: %s", last_error,
                )
                break
            time.sleep(nap)

        self._fail_calls += 1
        raise LLMError(last_error)

    def _budget_exhausted(self) -> str:
        """Причина отказа, когда время на ответ кончилось.

        В сообщении нужны обе цифры: сколько ждали и сколько разрешено, —
        иначе в логе это неотличимо от «модель молчит».
        """
        return (
            f"время на ответ истекло: ждали {self._elapsed():.0f} с, "
            f"лимит {self._budget:.0f} с"
        )

    # ------------------------------------------------------------------ #
    def _parse(self, data: dict[str, Any]) -> LLMReply:
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("модель вернула пустой ответ")
        choice = choices[0] or {}
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):  # некоторые модели отдают массив частей
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if not isinstance(content, str) or not content.strip():
            # ling-3.0-flash — reasoning-модель: при маленьком max_tokens весь
            # бюджет уходит на размышление и content приходит пустым.
            # Сообщаем причину, иначе диагностика выглядит как «модель сломалась».
            reason = self._empty_reason(choice, message)
            if str(choice.get("finish_reason") or "") == "length":
                # Повтор с бо́льшим бюджетом имеет шанс, поэтому отдаём
                # вызывающему особый тип ошибки, а не общий отказ.
                raise _Truncated(reason)
            raise LLMError(reason)
        usage = data.get("usage") or {}
        return LLMReply(
            text=sanitize_model_reply(content),
            model=str(data.get("model") or self._model),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            raw_finish=str(choice.get("finish_reason") or ""),
        )

    @staticmethod
    def _empty_reason(choice: dict[str, Any], message: dict[str, Any]) -> str:
        """Объясняет, почему не пришёл текст."""
        finish = str(choice.get("finish_reason") or "")
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if finish == "length":
            hint = " (бюджет max_tokens исчерпан на размышлении)"
            return f"модель вернула пустой текст: не хватило max_tokens{hint}"
        if isinstance(reasoning, str) and reasoning.strip():
            return "модель вернула только размышление без ответа"
        if message.get("refusal"):
            return "модель отказалась отвечать"
        return "модель вернула пустой текст"

    def _safe_error(self, exc: urllib.error.HTTPError) -> str:
        """Читаем тело ошибки, но никогда не выводим в него наш ключ."""
        try:
            body = exc.read(2048).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return exc.reason or "ошибка"
        try:
            parsed = json.loads(body)
            err = parsed.get("error") or parsed
            if isinstance(err, dict):
                return str(err.get("message") or err.get("type") or "ошибка")[:200]
            return str(err)[:200]
        except ValueError:
            return redact(body)[:200]

    # ------------------------------------------------------------------ #
    def classify(self, text: str, classify_system: str) -> dict[str, str]:
        """Определяет намерение. При любой ошибке — безопасный keyword-фолбэк
        выполняется на стороне вызывающего кода."""
        reply = self.chat(
            [
                {"role": "system", "content": classify_system},
                {"role": "user", "content": clean_text(text, 600)},
            ],
            # reasoning-модель: 120 токенов уходили на размышление, а не на JSON.
            max_tokens=400,
            temperature=0.0,
        )
        raw = reply.text.strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise LLMError("классификатор вернул не JSON")
        try:
            parsed = json.loads(raw[start:end + 1])
        except ValueError as exc:
            raise LLMError("классификатор вернул битый JSON") from exc
        if not isinstance(parsed, dict):
            raise LLMError("классификатор вернул не объект")
        return {
            "intent": str(parsed.get("intent", "other"))[:30],
            "category": str(parsed.get("category", "none"))[:30],
        }

    def check_connection(self) -> str:
        """Быстрая диагностика: один короткий запрос."""
        try:
            reply = self.chat(
                [
                    {"role": "system", "content": "Отвечай одним словом."},
                    {"role": "user", "content": "Скажи «готово»."},
                ],
                max_tokens=300,
                temperature=0.0,
            )
            return f"OK: модель {reply.model} ответила «{reply.text[:30]}»"
        except LLMError as exc:
            return f"ОШИБКА: {redact(str(exc))}"
