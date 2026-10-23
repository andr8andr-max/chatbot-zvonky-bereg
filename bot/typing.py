"""Нативный индикатор «печатает…».

У сообщества в VK есть штатный способ показать собеседнику системный
значок набора текста — метод ``messages.setActivity``:

    messages.setActivity?group_id=<id>&peer_id=<id>&type=typing

VK держит статус 10 секунд и сам гасит его, когда сообщество отправляет
сообщение, поэтому ни заглушки в чате, ни её удаления не требуется.

Раньше здесь отправлялось обычное сообщение «✍️ Ирина печатает…», которое
редактировалось и удалялось. Это выглядело как фальшивое сообщение в
переписке, поэтому заменено на настоящий индикатор.

Если метод недоступен (старый токен, запрет сообщества), индикатор молча
выключается: лучше не показать ничего, чем показать лишнее.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

log = logging.getLogger("bot.typing")

#: VK держит статус 10 секунд. Обновляем чаще, чтобы он не погас, пока
#: модель думает, но не чаще, чем нужно.
REFRESH_INTERVAL = 6.0

#: Индикатор гаснет, как только бот отправит ответ, поэтому ждать перед его
#: стартом нельзя: модель отвечает за ~2 с, и индикатор с задержкой 0.8 с
#: прожил бы меньше секунды. Стартуем сразу, а для ответов без обращения к
#: модели (нажатие кнопки) он не показывается вовсе — там задержка не нужна.
START_DELAY = 0.0


class TypingIndicator:
    """Показывает системный «печатает…», пока бот ждёт модель.

    Использование как контекстный менеджер::

        with TypingIndicator(vk, peer_id):
            replies = engine.handle(msg)

    Ничего не отправляет в чат: только держит статус активности, который
    VK сам снимает после отправки ответа.
    """

    def __init__(
        self,
        vk: Any,
        peer_id: int,
        *,
        delay: float = START_DELAY,
        interval: float = REFRESH_INTERVAL,
    ) -> None:
        self._vk = vk
        self._peer_id = int(peer_id)
        self._delay = max(0.0, delay)
        self._interval = max(1.0, interval)
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        #: True, если VK принял хотя бы один вызов setActivity.
        self.shown = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    def __enter__(self) -> TypingIndicator:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------------ #
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="typing-indicator", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Останавливает обновление статуса.

        Сам статус снимать не нужно: он гаснет через 10 секунд либо сразу
        после того, как бот отправит ответ.
        """
        self._done.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    # ------------------------------------------------------------------ #
    def _run(self) -> None:
        if not self._wait(self._delay):
            return
        while not self._done.is_set():
            if not self._ping():
                return
            if not self._wait(self._interval):
                return

    def _ping(self) -> bool:
        """Один вызов ``messages.setActivity``. False — дальше повторять смысла нет."""
        setter = getattr(self._vk, "set_typing", None)
        if setter is None:
            log.info("Индикатор «печатает…» отключён: у клиента нет set_typing")
            return False
        try:
            ok = bool(setter(self._peer_id))
        except Exception as exc:  # noqa: BLE001
            log.info("Индикатор «печатает…» не отправлен: %s", exc)
            return False
        with self._lock:
            first = not self.shown
            self.shown = self.shown or ok
        if first:
            log.info(
                "Индикатор «печатает…»: messages.setActivity peer=%s принят=%s",
                self._peer_id, ok,
            )
        if not ok:
            log.info("VK не принял messages.setActivity для peer %s", self._peer_id)
        return ok

    def _wait(self, seconds: float) -> bool:
        """Ждёт, но просыпается раньше при ``stop()``.

        Возвращает False, если индикатор уже остановили.
        """
        return not self._done.wait(seconds)
