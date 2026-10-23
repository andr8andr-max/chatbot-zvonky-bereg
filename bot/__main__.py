"""Точка входа: python -m bot"""

from __future__ import annotations

import argparse
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import ROOT, ConfigError, load_config, loaded_env_keys, self_check
from .security import scan_tree_for_secrets
from .vk_api import _CHAT_PEER_MIN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m bot",
        description="Чат-бот фестиваля «Звонкий берег» (Ирина)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=("run", "check", "diag", "chats", "watch-chat", "test-message", "notify",
                 "sheets-test", "del"),
        help="что сделать (по умолчанию run)",
    )
    parser.add_argument(
        "text",
        nargs="?",
        default=None,
        help="текст сообщения для команды test-message или notify",
    )
    parser.add_argument("--check", action="store_true", help="проверить конфигурацию и безопасность и выйти")
    parser.add_argument("--diag", action="store_true", help="проверить соединение с ProxyAPI и ВКонтакте и выйти")
    parser.add_argument("--test-message", metavar="TEXT", help="прогнать текст через сценарии и показать ответ")
    parser.add_argument("--user-id", type=int, default=999999, help="условный id пользователя для --test-message")
    parser.add_argument(
        "--notify",
        action="store_true",
        help="отправить тестовое уведомление организаторам во все каналы",
    )
    parser.add_argument(
        "--seconds",
        type=int,
        default=120,
        help="сколько секунд ждать сообщение из беседы для watch-chat",
    )
    parser.add_argument(
        "--sheets-test",
        action="store_true",
        help="пошагово проверить связь с Google Sheets и записать тестовую строку",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="подтвердить удаление заявки (для команды del)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    finally:
        # Страховка для всех команд: Application закрывает базу и лог сам, но
        # если он не был создан (ошибка конфигурации) или команда упала,
        # файловые дескрипторы всё равно нужно отдать.
        from .app import shutdown_logging  # noqa: PLC0415

        shutdown_logging()


def _main(argv: list[str] | None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[ОШИБКА КОНФИГУРАЦИИ] {exc}", file=sys.stderr)
        return 2

    if args.check or args.command == "check":
        return _check(cfg)
    if args.sheets_test or args.command == "sheets-test":
        return _sheets_test(cfg)
    if args.command == "del":
        return _delete(cfg, args.text, args.yes)
    text = args.text or args.test_message
    if args.test_message or args.command == "test-message":
        if not text:
            print("Для test-message нужен текст: python -m bot test-message \"Привет\"",
                  file=sys.stderr)
            return 2
        return _test(cfg, text, args.user_id)
    if args.diag or args.command == "diag":
        return _diagnose(cfg)
    if args.notify or args.command == "notify":
        return _notify(cfg, text)
    if args.command == "chats":
        return _list_chats(cfg)
    if args.command == "watch-chat":
        return _watch_chat(cfg, max(10, args.seconds))

    from .app import Application

    with Application(cfg) as app:
        if not app.store.stats():
            app.diagnose()
        with _pid_file(cfg):
            return app.run()


@contextmanager
def _pid_file(cfg):
    """Пишет PID бота в data/bot.pid и убирает файл при остановке.

    Файл нужен, чтобы перезапускать бота и проверять, что старый процесс
    действительно завершён, а не остался висеть на старом коде.
    """
    path = Path(cfg.log_file).parent / "bot.pid"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError as exc:
        print(f"[!] не удалось записать {path}: {exc}", file=sys.stderr)
    try:
        yield
    finally:
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass


def _sheets_test(cfg) -> int:
    """Пошаговая проверка Google Sheets: библиотеки, ключ, доступ, запись.

    Настройка требует четырёх вещей, и каждая может не совпасть: пакеты,
    файл ключа, доступ к таблице и её идентификатор. Проверять их по одной
    команде дешевле, чем читать потом ошибку 404 из лога в три часа ночи.
    """
    from .sheets import HEADERS, LAST_COLUMN, SheetsError, SheetsWriter, _import_ok

    print("=" * 66)
    print("ПРОВЕРКА GOOGLE SHEETS")
    print("=" * 66)

    print(f"  SHEETS_ENABLED       : {'включено' if cfg.sheets_enabled else 'выключено'}")
    print(f"  Опрос статусов       : "
          f"{'включён' if cfg.sheets_poll_enabled else 'выключен'}"
          f" каждые {cfg.sheets_poll_interval_sec} с")
    print(f"  Лист-приёмник        : {len(HEADERS)} колонок, A..{LAST_COLUMN}")
    print(f"  Заголовки            : {', '.join(HEADERS)}")
    print("-" * 66)

    # Шаг 1: библиотеки.
    if not _import_ok():
        print("  [!!] Не установлены google-api-python-client и google-auth.")
        print("       python -m pip install -r requirements-sheets.txt")
        print("=" * 66)
        return 1
    print("  [OK] Библиотеки установлены")

    # Шаг 2: идентификатор таблицы. Заглушка из .env.example не пройдёт дальше.
    sid = (cfg.google_spreadsheet_id or "").strip()
    if not sid:
        print("  [!!] GOOGLE_SPREADSHEET_ID пуст.")
        print("       Возьмите ID из ссылки: docs.google.com/spreadsheets/d/<ID>/edit")
        print("=" * 66)
        return 1
    if _looks_like_placeholder(sid):
        print(f"  [!!] GOOGLE_SPREADSHEET_ID похож на заглушку из .env.example: {sid}")
        print("       Это не настоящий ID таблицы — замените в .env.")
        print("=" * 66)
        return 1
    print(f"  [OK] GOOGLE_SPREADSHEET_ID задан ({len(sid)} симв.)")

    # Шаг 3: ключ сервис-аккаунта.
    writer = SheetsWriter(cfg)
    if not writer.ready:
        print(f"  [!!] {writer.status}")
        print("=" * 66)
        return 1
    creds_file = cfg.google_credentials_file
    if creds_file:
        print(f"  [OK] Файл ключа найден: {creds_file}")
    else:
        print("  [OK] Ключ задан в GOOGLE_CREDENTIALS_JSON (в лог не попадает)")

    # Шаг 4: доступ к таблице. Здесь нужны и сеть, и права аккаунта.
    try:
        service = writer._get_service()
        meta = service.spreadsheets().get(
            spreadsheetId=sid, fields="spreadsheetId,spreadsheetUrl,sheets.properties"
        ).execute()
    except SheetsError as exc:
        print(f"  [!!] Ошибка доступа к таблице: {exc}")
        print("       Чаще всего таблица не открыта сервис-аккаунту:")
        print("       «Настройки таблицы → Доступ → Поделиться» → e-mail аккаунта,")
        print("       права «Редактор». Проверьте, что ID таблицы верный.")
        print("=" * 66)
        return 1
    except Exception as exc:  # noqa: BLE001
        from .security import redact

        print(f"  [!!] Не удалось обратиться к Google API: {redact(str(exc))}")
        print("=" * 66)
        return 1

    titles = [s.get("properties", {}).get("title", "") for s in meta.get("sheets", [])]
    print(f"  [OK] Листов в таблице: {len(titles)}"
          + (f" ({', '.join(titles)})" if titles else ""))

    # Шаг 5: право на запись. Только запись доказывает, что аккаунт не
    # «читатель». Пробу делаем в одноразовом листе, который удаляем целиком.
    # Раньше она писалась в A1 первого листа: при пустой таблице это проходило
    # незаметно, а стоило в листе появиться данным — и проверка стирала
    # заголовок или первую заявку. Чужие ячейки проверка не должна трогать
    # ни при каких обстоятельствах.
    target = _probe_sheet_title()
    probe = f"проверка бота, {now_stamp()}"
    try:
        service.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{"addSheet": {"properties": {"title": target}}}]},
        ).execute()
    except Exception as exc:  # noqa: BLE001
        from .security import redact

        print(f"  [!!] Не удалось создать временный лист: {redact(str(exc))}")
        print("=" * 66)
        return 1

    range_a1 = f"{target}!A1"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=sid, range=range_a1,
            valueInputOption="RAW", body={"values": [[probe]]},
        ).execute()
        back = service.spreadsheets().values().get(
            spreadsheetId=sid, range=range_a1
        ).execute()
        if (back.get("values") or [[None]])[0][0] != probe:
            print(f"  [!!] Записали в {range_a1}, но прочитали другое значение.")
            print("=" * 66)
            return 1
        print(f"  [OK] Запись работает: {range_a1} прочитана обратно")
    except Exception as exc:  # noqa: BLE001
        from .security import redact

        print(f"  [!!] Права только на чтение: {redact(str(exc))}")
        print("       Выдайте сервисному аккаунту права «Редактор», а не «Читатель».")
        print("=" * 66)
        return 1
    finally:
        # Удаляем временный лист целиком, а не чистим ячейку: так в таблице
        # не остаётся ни мусора, ни пустой строки, ни сдвинутых формул.
        try:
            _delete_sheet_by_title(service, sid, target)
            print(f"  [OK] Временный лист «{target}» удалён")
        except Exception:  # noqa: BLE001
            print(f"  [!] Не удалось удалить временный лист «{target}» — "
                  f"удалите его вручке, он остался пустым")


    print("-" * 66)
    print("  Итог: доступ есть, заявки будут уходить в таблицу.")
    print("=" * 66)
    return 0


def now_stamp() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y%m%d-%H%M%S")


# Google не пускает в имя листа эти символы, а длина ограничена 100 знаками.
_FORBIDDEN_IN_SHEET_TITLE = re.compile(r"[\[\]*?/\\]")


def _probe_sheet_title() -> str:
    """Имя одноразового листа для проверки записи.

    Метка времени нужна, чтобы запуски не спорили за одно имя: если лист с
    таким именем уже остался от прерванной проверки, следующая создаст
    новый, а не упадёт.
    """
    return _FORBIDDEN_IN_SHEET_TITLE.sub("", f"проба бота {now_stamp()}")[:100]


def _delete_sheet_by_title(service, spreadsheet_id: str, title: str) -> None:
    from .sheets import SheetsError

    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id, fields="sheets.properties"
    ).execute()
    for sheet in meta.get("sheets", []):
        if sheet.get("properties", {}).get("title") == title:
            service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"deleteSheet": {
                    "sheetId": sheet["properties"]["sheetId"]}}]},
            ).execute()
            return
    raise SheetsError(f"лист «{title}» не найден — удалите его вручную")


def _delete(cfg, number: str | None, confirmed: bool) -> int:
    """Удаляет заявку и подтягивает нумерацию, чтобы не осталось пропуска.

    Нужна, чтобы убрать заявку, созданную при проверке сценария. Счётчик
    номеров сам не уменьшается, поэтому после удаления последней заявки
    следующая получила бы номер с пропуском — организатор решил бы, что
    заявка потерялась.
    """
    if not number:
        print("Укажите номер заявки: python -m bot del ЗБ-0001 --yes",
              file=sys.stderr)
        return 2

    from .app import Application
    from .sheets import CsvFallback  # noqa: PLC0415

    with Application(cfg) as app:
        target = app.store.get_by_number(number)
        if target is None:
            print(f"Заявка {number} не найдена — ничего не удаляем.")
            return 1
        if not confirmed:
            print(f"Найдена заявка {target.number}: {target.full_name}, "
                  f"{target.city or '—'}, {target.category}.")
            print(f"Описание: {target.description[:70]}")
            print()
            print("Это удаление безвозвратно. Повторите с подтверждением:")
            print(f"  python -m bot del {target.number} --yes")
            return 2

        if app.store.delete_application(target.number):
            print(f"  [OK] Заявка {target.number} удалена из базы")
        else:
            print(f"  [!!] Не удалось удалить {target.number}")
            return 1

        # CSV открыт на дописывание, строка из него сама не исчезнет.
        csv_removed = CsvFallback(cfg.csv_export_path, cfg).remove(target.number)
        print(f"  [{'OK' if csv_removed else '!!'}] "
              f"Убрана из выгрузки {cfg.csv_export_path.name}")

        if target.sheet_row:
            print(f"  [!] В таблице строка {target.sheet_row} осталась — "
                  f"удалите её вручную, бот не знает о таблице без ключа")

        last = app.store.last_number()
        print(f"  [OK] Следующая заявка будет {last}")
    return 0


def _looks_like_placeholder(value: str) -> bool:
    """Отличает ID из .env.example от настоящего идентификатора таблицы."""
    lowered = value.lower()
    words = ("example", "your", "here", "spreadsheet-id", "changeme", "replace",
             "xxxx", "todo", "вставьте", "замените")
    if any(word in lowered for word in words) or value.startswith("1AbCdEfGhIjKlMnOp"):
        return True
    # Настоящий ID — 40-60 символов из [a-zA-Z0-9-_]. Короткий или
    # состоящий из цифр — учебный пример, а не таблица.
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return True
    return len(value.rstrip("0")) < 20


def _check(cfg) -> int:
    print("=" * 66)
    print("ПРОВЕРКА КОНФИГУРАЦИИ")
    print("=" * 66)
    print(f"  VK_GROUP_ID        : {cfg.vk_group_id}")
    print(f"  VK_GROUP_TOKEN     : задан ({len(cfg.vk_group_token)} симв.), в лог не попадает")
    print(f"  Администраторы     : {cfg.vk_admin_users or '— НЕ ЗАДАНЫ —'}")
    print(f"  Получатели уведомлений: {cfg.vk_admin_peers or '— НЕ ЗАДАНЫ —'}")
    print(f"  Модель             : {cfg.llm_model or '—'}")
    print(f"  ProxyAPI           : {cfg.llm_base_url}")
    print(f"  LLM_API_KEY        : {'задан' if cfg.llm_api_key else '— НЕ ЗАДАН —'}")
    print(f"  Google Sheets      : {'включено' if cfg.sheets_enabled else 'выключено (CSV-фолбэк)'}")
    if cfg.sheets_enabled:
        poll = ("включён" if cfg.sheets_poll_enabled else "выключен")
        print(f"  Опрос статусов     : {poll} каждые {cfg.sheets_poll_interval_sec} с")
    print(f"  Локальное хранение : {'телефон и почта сохраняются' if cfg.store_pii_locally else 'только хеши'}")
    print(f"  Режим отправки    : {'DRY_RUN — уведомления организаторам НЕ отправляются' if cfg.dry_run else 'боевой, уведомления идут'}")
    print(f"  Файл базы          : {cfg.db_path}")
    print(f"  CSV-фолбэк         : {cfg.csv_export_path}")
    print(f"  Переменные из .env : {len(loaded_env_keys())}")
    print("-" * 66)
    print("ПРЕДУПРЕЖДЕНИЯ")
    warnings = self_check(cfg)
    if warnings:
        for warning in warnings:
            print(f"  [!] {warning}")
    else:
        print("  нет")
    print("-" * 66)
    _check_admin_channels(cfg)
    print("-" * 66)
    print("ПОИСК УТЕЧЕК СЕКРЕТОВ")
    report = scan_tree_for_secrets(ROOT, cfg)
    for passed in report.passed:
        print(f"  [OK] {passed}")
    for finding in report.findings:
        print(f"  [!!] {finding}")
    print("=" * 66)
    return 1 if report.findings else 0


def _check_admin_channels(cfg) -> None:
    """Проверяет, что сообщество действительно может писать организаторам.

    Список участников — не доказательство доступа. ``messages.getConversationMembers``
    отдаёт неполные данные, а при ошибке 917 не отвечает вовсе, хотя отправка
    в беседу работает. Поэтому единственная честная проверка — фактическая
    отправка: ``python -m bot notify``.
    """
    print("КАНАЛЫ УВЕДОМЛЕНИЙ")
    from .vk_api import VKClient, VKError  # noqa: PLC0415

    vk = VKClient(cfg)
    for peer in cfg.vk_admin_peers:
        # В VK_ADMIN_PEER_IDS можно смешивать беседы и личные сообщения.
        # Список участников есть только у беседы, у лички его не бывает.
        if peer < _CHAT_PEER_MIN:
            print(f"  [OK] личка {peer}: это личное сообщение, проверяется отдельно")
            continue
        try:
            items = vk._call(
                f"messages.getConversationMembers?peer_id={peer}&count=200", ""
            )["items"]
            # В VK участники беседы лежат в member_id, а не в id, и сообщество
            # записано там с минусом: -241767034, а не 241767034. Проверка по id
            # всегда выдавала «сообщество не состоит», поэтому сверяем модуль.
            ours = [
                m for m in items
                if abs(m.get("member_id") or 0) == cfg.vk_group_id
                or abs(m.get("id") or 0) == cfg.vk_group_id
            ]
            if ours:
                print(f"  [OK] беседа {peer}: сообщество состоит, участников {len(items)}")
            else:
                # Список участников — не доказательство доступа: VK отдаёт его
                # неполно, и реальная отправка при этом может работать. Поэтому
                # здесь только сомнение, а не ошибка, и честная проверка — фактическая
                # отправка: python -m bot notify.
                print(
                    f"  [?] беседа {peer}: в списке участников сообщества "
                    f"{cfg.vk_group_id} нет (участников {len(items)}) — это ещё не "
                    f"значит, что отправка не работает: VK отдаёт список не полностью. "
                    f"Проверьте фактической отправкой: python -m bot notify"
                )
        except VKError as e:
            if e.code == 917:
                # Сообщество пишет в беседу, но не видит список участников —
                # так VK отвечает многим сообществам, на рассылку не влияет.
                print(
                    f"  [OK] беседа {peer}: отправка разрешена, список "
                    f"участников недоступен (917) — для рассылки это нормально"
                )
            elif e.code == 927:
                print(
                    f"  [!!] беседа {peer}: сообщество не состоит в беседе (927). "
                    f"В беседу добавляется бот сообщества, а не сообщество целиком — "
                    f"добавьте бота и повторите проверку."
                )
            else:
                print(
                    f"  [!!] беседа {peer}: сообщество не имеет доступа ({e}). "
                    f"Добавьте в беседу бота сообщества или уберите id из VK_ADMIN_PEER_IDS."
                )
    # Лички проверяем отдельно: в VK_ADMIN_PEER_IDS их быть не должно, иначе
    # организаторы получают одно и то же трижды. Это запасной канал — на случай,
    # когда беседы недоступны, и для срочных оповещений. Проверять его нужно
    # заранее: узнать о неработающем запасном канале можно лишь тогда, когда
    # основной уже отказал.
    for user in cfg.vk_admin_users:
        try:
            conv = vk._call(f"messages.getConversationsById?peer_ids={user}&count=1", "")
            allowed = (conv["items"][0].get("can_write") or {}).get("allowed")
            flag = "OK" if allowed else "!!"
            hint = "" if allowed else " — человек не писал сообществу, напишите ему"
            print(f"  [{flag}] запасной канал, личка {user}: писать можно={allowed}{hint}")
        except VKError as e:
            print(f"  [!!] запасной канал, личка {user}: {e}")


def _watch_chat(cfg, seconds: int) -> int:
    """Ждёт сообщение из беседы и печатает её настоящий peer_id.

    Номер беседы в адресной строке браузера не является peer_id для API, и
    сопоставления между ними VK не отдаёт. Единственный источник — события,
    которые VK присылает сообществу, поэтому достаточно один раз написать
    в беседе.
    """
    import time  # noqa: PLC0415

    from .vk_api import VKClient, VKError  # noqa: PLC0415

    print("=" * 66)
    print("ОПРЕДЕЛЕНИЕ peer_id БЕСЕДЫ")
    print("=" * 66)
    print(f"  Сейчас напишите в беседу, сообщество должно быть её участником.")
    print(f"  Жду до {seconds} с…")
    vk = VKClient(cfg)
    try:
        server, key, ts = vk.get_longpoll_server()
    except VKError as e:
        print(f"  [ОШИБКА] не получилось подключиться к Long Poll: {e}")
        return 1
    found: dict[int, str] = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            ts, updates = vk.poll(server, key, ts, min(25, remaining))
        except VKError as e:
            print(f"  [ОШИБКА] {e}")
            return 1
        for update in updates:
            if update.get("type") != "message_new":
                continue
            payload = (update.get("object") or {}).get("message") or {}
            if payload.get("peer_id") and payload["peer_id"] > 0:
                found[payload["peer_id"]] = str(
                    (payload.get("text") or "")[:50] or "(без текста)"
                )
        if found:
            break
    if not found:
        print("  Сообщений не пришло. Проверьте, что сообщество состоит в беседе")
        print("  и что вы написали в неё, а не в личку сообществу.")
        return 1
    print("-" * 66)
    for peer, text in found.items():
        print(f"  peer_id = {peer}   <- это значение для VK_ADMIN_PEER_IDS")
        print(f"      сообщение: {text!r}")
    print("-" * 66)
    print("  Впишите peer_id в .env: VK_ADMIN_PEER_IDS=<значение>")
    print("  Номер из адресной строки браузера не подходит.")
    print("=" * 66)
    return 0


def _list_chats(cfg) -> int:
    """Показывает peer_id бесед сообщества — те, что нужны для VK_ADMIN_PEER_IDS.

    Номер беседы в адресной строке браузера НЕ является peer_id для API,
    поэтому брать его оттуда нельзя: VK отдаёт другой идентификатор.
    """
    from .vk_api import VKClient, VKError  # noqa: PLC0415

    print("=" * 66)
    print(f"ДИАЛОГИ СООБЩЕСТВА {cfg.vk_group_id}")
    print("=" * 66)
    vk = VKClient(cfg)
    try:
        result = vk._call("messages.getConversations?count=200", "")
    except VKError as e:
        print(f"  [ОШИБКА] {e}")
        return 1
    for item in result.get("items", []):
        conv = item.get("conversation") or {}
        peer = conv.get("peer") or {}
        kind = peer.get("type")
        pid = peer.get("id")
        title = item.get("conversation_title") or ""
        if kind != "chat":
            print(f"  {kind:<5} {pid}")
            continue
        try:
            members = vk._call(
                f"messages.getConversationMembers?peer_id={pid}&count=200", ""
            )["items"]
            state = f"участников {len(members)}"
        except VKError as e:
            state = f"список участников недоступен ({e.code})"
        print(f"  чат   {pid}  {title}  — {state}")

    print("-" * 66)
    print("НАСТРОЕННЫЕ БЕСЕДЫ (VK_ADMIN_PEER_IDS)")
    for peer in cfg.vk_admin_peers:
        listed = any(
            ((c.get("conversation") or {}).get("peer") or {}).get("id") == peer
            and ((c.get("conversation") or {}).get("peer") or {}).get("type") == "chat"
            for c in result.get("items", [])
        )
        note = "" if listed else " — в списке диалогов её нет (это бывает и при рабочей беседе)"
        # VK часто не даёт читать историю и состав беседы, хотя отправка идёт.
        # Поэтому здесь только чтение, и вывод делать о нём нельзя.
        try:
            vk._call(f"messages.getHistory?peer_id={peer}&count=1", "")
            read = "чтение истории доступно"
        except VKError as e:
            read = f"чтение истории закрыто ({e.code}) — на отправку не влияет"
        print(f"  {peer}: {read}{note}")
    print("-" * 66)
    print("  В VK_ADMIN_PEER_IDS указывайте peer_id из VK, а не номер из адресной")
    print("  строки браузера — это разные идентификаторы.")
    print("  Если беседы нет в списке выше, её peer_id видно в data/bot.log:")
    print("  строка 'Входящее ... в peer N'. Либо проверьте python -m bot check.")
    print("=" * 66)
    return 0


def _diagnose(cfg) -> int:
    from .app import Application  # noqa: PLC0415

    with Application(cfg) as app:
        app.diagnose()
    return 0


def _notify(cfg, text: str | None) -> int:
    """Отправляет тестовое уведомление организаторам через боевой путь.

    Именно через ``_notify_organizers_raw``, а не напрямую через API: только так
    проверяется вся цепочка — беседа, оба личных сообщения и разбор ошибок
    доставки. Текст помечен как проверочный, чтобы его не приняли за заявку.
    """
    from .app import Application  # noqa: PLC0415

    app = Application(cfg)
    body = text.strip() if text and text.strip() else "Это проверка связи. Заявок нет, ничего отвечать не нужно."
    message = f"🧪 ТЕСТОВОЕ УВЕДОМЛЕНИЕ\n\n{body}"
    if cfg.dry_run:
        print("[!] DRY_RUN=1 — уведомление НЕ будет отправлено, только записано в лог.")
    print(f"Беседы (основной канал): {list(cfg.vk_admin_peers)}")
    print(f"Лички (запасной канал) : {list(cfg.vk_admin_users)} — только если беседа недоступна")
    with app:
        app.engine._notify_organizers_raw(message)
    print("Готово. Куда доставилось — смотри в логе: data/bot.log.")
    return 0


def _test(cfg, text: str, user_id: int) -> int:
    """Прогон текста через сценарии без обращения к ВКонтакте."""
    from .app import Application  # noqa: PLC0415
    from .vk_api import Message  # noqa: PLC0415

    app = Application(cfg)
    msg = Message(peer_id=1, from_id=user_id, text=text)
    with app:
        for reply in app.engine.handle(msg):
            print("—" * 40)
            print(reply.text)
            if reply.keyboard:
                print("[клавиатура]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
