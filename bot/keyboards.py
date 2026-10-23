"""Клавиатуры ВКонтакте (one_time, с payload для надёжной маршрутизации)."""

from __future__ import annotations

import json
from typing import Any, Iterable

from .config import Category
from .security import is_allowed_action


def _btn(label: str, action: str, data: dict[str, Any] | None = None, *, color: str = "default") -> dict[str, Any]:
    payload = {"action": action}
    if data:
        payload["data"] = data
    return {
        "action": {
            "type": "callback",
            "label": label[:40],
            "payload": json.dumps(payload, ensure_ascii=False),
        },
        "color": color,
    }


def build(rows: Iterable[Iterable[dict[str, Any]]], *, one_time: bool = True) -> dict[str, Any]:
    """Собирает inline-клавиатуру.

    Поле ``one_time`` нельзя сочетать с ``inline``: VK отвечает ошибкой 911
    «one_time field is not available for inline keyboard». Для inline оно и
    не нужно — кнопки живут в сообщении, а не в поле ввода.
    """
    keyboard: dict[str, Any] = {
        "inline": True,
        "buttons": [[btn_copy(b) for b in row] for row in rows if row],
    }
    if not keyboard["inline"]:
        keyboard["one_time"] = one_time
    return keyboard


def btn_copy(button: dict[str, Any]) -> dict[str, Any]:
    return dict(button)


def main_menu(
    categories: tuple[Category, ...] | None = None,
    *,
    has_application: bool = False,
) -> dict[str, Any]:
    """Главное меню — всегда ровно три кнопки.

    ``categories`` оставлен для совместимости вызовов и не используется: набор
    категорий показывается отдельной клавиатурой при подаче заявки.

    ``has_application`` меняет только первую кнопку — «Подать заявку» на
    «Мой статус». Правило одной активной заявки должно быть видно сразу, а не
    после попытки подать вторую.
    """
    first = (
        _btn("📄 Мой статус", "status", color="primary")
        if has_application
        else _btn("🎸 Подать заявку", "apply", color="primary")
    )
    return build([
        [first],
        [_btn("ℹ️ О фестивале", "about")],
        [_btn("✉️ Вопрос организатору", "ask")],
    ])


def category_menu(categories: tuple[Category, ...]) -> dict[str, Any]:
    rows = [[_btn(c.title, "apply", {"category": c.key}, color="primary")] for c in categories]
    rows.append([_btn("⬅️ Назад", "back"), _btn("✖️ Отмена", "cancel")])
    return build(rows)


def nav(extra: list[list[dict[str, Any]]] | None = None, *, back: bool = True,
        optional: bool = False) -> dict[str, Any]:
    rows: list[list[dict[str, Any]]] = list(extra or [])
    tail: list[dict[str, Any]] = []
    if optional:
        tail.append(_btn("⏭️ Пропустить", "skip"))
    if back:
        tail.append(_btn("⬅️ Назад", "back"))
    tail.append(_btn("✖️ Отмена", "cancel"))
    rows.append(tail)
    return build(rows)


def day_picker() -> dict[str, Any]:
    return build([
        [
            _btn("Суббота", "day_sat", color="primary"),
            _btn("Воскресенье", "day_sun", color="primary"),
        ],
        [_btn("Оба дня", "day_both", color="positive")],
        [_btn("⬅️ Назад", "back"), _btn("✖️ Отмена", "cancel")],
    ])


def confirm() -> dict[str, Any]:
    return build([
        [_btn("✅ Отправить", "confirm_send", color="positive")],
        [_btn("✏️ Изменить", "edit"), _btn("⬅️ Назад", "back"), _btn("✖️ Отмена", "cancel")],
    ])


def after_apply(*, has_application: bool = True) -> dict[str, Any]:
    """Меню после отправки заявки: то же главное меню, заявка уже есть."""
    return main_menu(has_application=has_application)


def status_actions(
    has_application: bool = False, *, show_status: bool = True
) -> dict[str, Any]:
    """Клавиатура для «Мой статус» и пустого состояния.

    Раньше здесь был свой набор кнопок, из-за чего в разных состояниях
    показывалось разное меню. Теперь это главное меню, поэтому количество
    кнопок везде одинаковое.
    """
    return main_menu(has_application=has_application)


def resume() -> dict[str, Any]:
    return build([
        [
            _btn("▶️ Продолжить", "resume", color="primary"),
            _btn("🔄 Начать заново", "menu"),
        ]
    ])


def start_over() -> dict[str, Any]:
    """После «Отмена» в анкете: одна кнопка — начать заново.

    Сообщение при отмене обещает кнопку «🔄 Начать заново», поэтому клавиатура
    должна её показывать, а не главное меню (там первая кнопка — «Мой статус»).
    """
    return build([
        [_btn("🔄 Начать заново", "menu", color="primary")],
    ])


def after_escalation(*, has_application: bool = False) -> dict[str, Any]:
    """Меню в состоянии «вопрос передан организаторам».

    Здесь не главное меню: пока вопрос у организаторов, ввод текста отключён,
    и без кнопки «Отменить вопрос» человек не сможет вернуться к диалогу.
    """
    return build([
        [_btn("↩️ Отменить вопрос", "cancel_question", color="primary")],
        [_btn("📄 Мой статус", "status")],
    ])


def human_handoff() -> dict[str, Any]:
    """Меню в состоянии ожидания ответа организаторов.

    Кнопка «Жду ответа» была удалена: она ничего не делала и только занимала
    место, а уведомление организаторам и так уходит сразу.
    """
    return main_menu()


def sanitize_keyboard(keyboard: dict[str, Any], allowed_actions: frozenset[str]) -> dict[str, Any] | None:
    """Отбрасывает клавиатуру, в которой оказались недопустимые действия.

    Клавиатура почти всегда словарь, но из-за неосторожного сложения ответов
    (список вместо клавиатуры) сюда мог попасть и ``list`` — такой в VK не
    уйдёт, поэтому просто отбрасываем его целиком.
    """
    if not isinstance(keyboard, dict):
        return None
    rows = keyboard.get("buttons")
    if not isinstance(rows, list):
        return None
    if keyboard.get("inline") and "one_time" in keyboard:
        # Страховка от 911: inline и one_time вместе VK не принимает.
        keyboard = {k: v for k, v in keyboard.items() if k != "one_time"}
    for row in rows:
        if not isinstance(row, list):
            return None
        for button in row:
            action = button.get("action") if isinstance(button, dict) else None
            payload = action.get("payload") if isinstance(action, dict) else None
            try:
                parsed = json.loads(payload) if isinstance(payload, str) else {}
            except ValueError:
                return None
            if not is_allowed_action(parsed.get("action")):
                return None
    return keyboard
