"""Запись заявок в Google Таблицу.

Модуль необязательный: работает, только если установлены google-api-python-client
и google-auth, а в окружении заданы GOOGLE_SPREADSHEET_ID и файл сервис-аккаунта.
Без него бот работает на локальной БД и экспортирует CSV, ничего не теряя.

Никаких секретов в коде: JSON ключа сервис-аккаунта читается из файла или переменной
окружения и сразу передаётся в библиотеку, в логи не попадает.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .config import Category, Config
from .security import clean_text, redact
from .storage import STATUS_NEW, Application

log = logging.getLogger("bot.sheets")

HEADERS = [
    "Номер", "Дата и время", "Категория", "ФИО / Название", "Город", "Телефон", "E-mail",
    "Описание", "Ссылка на работы", "Фото / файл", "Удобный день", "Статус",
    "Комментарий куратора", "Аккаунт (ID)", "Имя аккаунта",
]

#: Позиции колонок берём из HEADERS, а не пишем числами: при добавлении
#: колонки в список чтение статуса молча съезжало бы на соседнее поле.
COL_NUMBER, COL_STATUS, COL_COMMENT = (HEADERS.index(name) for name in
                                       ("Номер", "Статус", "Комментарий куратора"))
#: Последняя колонка: по ней строим диапазон строки при обновлении.
LAST_COLUMN = chr(ord("A") + len(HEADERS) - 1)
#: Колонки, которые бот имеет право переписывать сам: статус и комментарий.
#: Остальные он не владеет и не должен затирать.
STATUS_FIRST_COLUMN = chr(ord("A") + COL_STATUS)
STATUS_LAST_COLUMN = chr(ord("A") + COL_COMMENT)
STATUS_RANGE = f"{STATUS_FIRST_COLUMN}:{STATUS_LAST_COLUMN}"


class SheetsError(RuntimeError):
    pass


def is_available(cfg: Config) -> bool:
    return bool(cfg.sheets_enabled and _import_ok())


def _import_ok() -> bool:
    try:
        import googleapiclient  # noqa: F401
        import google.oauth2  # noqa: F401
        return True
    except ImportError:
        return False


def _reason(exc: Exception) -> str:
    """Короткая, но actionable причина отказа Google.

    Google возвращает в тексте ошибки и ссылку, и внутренние коды, и
    html-подобные метки. Всё это в лог не годится: там не найти причину.
    Оставляем номер и суть — «API выключен», «нет доступа к таблице».
    """
    text = redact(str(exc))
    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        return "Google Sheets API не включён в проекте сервисного аккаунта"
    if "not found" in text.lower() or "Requested entity was not found" in text:
        return "таблица не найдена — проверьте GOOGLE_SPREADSHEET_ID"
    if "PERMISSION_DENIED" in text or "403" in text:
        return (
            "нет доступа к таблице — откройте её для "
            "client_email из файла ключа с ролью «Редактор»"
        )
    if "404" in text:
        return "таблица не найдена — проверьте GOOGLE_SPREADSHEET_ID"
    if "credentials" in text.lower() or "401" in text:
        return "ключ отвергнут Google — файл ключа повреждён или отозван"
    return text[:160]


def _credentials(cfg: Config):
    from google.oauth2 import service_account  # type: ignore

    info: dict[str, Any] | None = None
    if cfg.google_credentials_file:
        path = Path(cfg.google_credentials_file)
        if not path.is_file():
            raise SheetsError(f"Файл ключа сервис-аккаунта не найден: {path}")
        try:
            os_mode = path.stat().st_mode & 0o777
            if os.name != "nt" and os_mode & 0o077:
                log.warning(
                    "У файла %s слишком широкие права (%o). Рекомендуется chmod 600.",
                    path.name, os_mode,
                )
        except OSError:
            pass
        info = json.loads(path.read_text(encoding="utf-8"))
    elif cfg.google_credentials_json:
        try:
            info = json.loads(cfg.google_credentials_json)
        except ValueError as exc:
            raise SheetsError("GOOGLE_CREDENTIALS_JSON не является корректным JSON") from exc

    if not info or info.get("type") != "service_account":
        raise SheetsError(
            "Ожидался JSON сервис-аккаунта Google (поле type = service_account)"
        )
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    return service_account.Credentials.from_service_account_info(info, scopes=scopes)


class SheetsWriter:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._service = None
        self._error = ""
        if not cfg.sheets_enabled:
            self._error = "SHEETS_ENABLED=false"
        elif not _import_ok():
            self._error = (
                "не установлены google-api-python-client и google-auth "
                "(pip install -r requirements-sheets.txt)"
            )
        elif cfg.google_credentials_file and not Path(cfg.google_credentials_file).is_file():
            # Раньше статус был «готова» при отсутствующем файле ключа: сбой
            # всплывал бы только на первой же заявке, уже после того, как
            # заявку приняли и пообещали организаторам.
            self._error = f"файл ключа сервис-аккаунта не найден: {cfg.google_credentials_file}"

    @property
    def ready(self) -> bool:
        return self._error == ""

    @property
    def status(self) -> str:
        return self._error or "готова"

    def verify(self) -> str:
        """Проверяет доступ к таблице и запоминает причину неудачи.

        Раньше статус был «готова» только потому, что нашёлся файл ключа.
        Настоящие права — выдан ли доступ сервисному аккаунту, включён ли
        Google Sheets API — отс��пались лишь на первой заявке, уже после
        того, как её приняли и пообещали организаторам. Один дешёвый запрос
        метаданных на старте честнее: видно сразу, что писать нельзя.
        """
        if not self.ready:
            return self.status
        try:
            self._service_or_none()
        except Exception as exc:  # noqa: BLE001
            self._error = f"доступ к таблице не работает: {_reason(exc)}"
        return self.status

    def _service_or_none(self):
        """Один запрос метаданных: проверяет и ключ, и права на таблицу."""
        service = self._get_service()
        service.spreadsheets().get(
            spreadsheetId=self.cfg.google_spreadsheet_id,
            fields="spreadsheetId,sheets.properties",
        ).execute()
        return service

    def _get_service(self):
        if self._service is None:
            from googleapiclient.discovery import build  # type: ignore

            self._service = build("sheets", "v4", credentials=_credentials(self.cfg), cache_discovery=False)
        return self._service

    # ------------------------------------------------------------------ #
    def _sheet_title(self, category_key: str) -> str:
        cat = self.cfg.category(category_key)
        return cat.sheet if cat else "Заявки"

    def _ensure_sheet(self, title: str) -> int | None:
        service = self._get_service()
        meta = service.spreadsheets().get(
            spreadsheetId=self.cfg.google_spreadsheet_id, fields="sheets.properties"
        ).execute()
        sheets = meta.get("sheets", [])
        for sheet in sheets:
            if sheet.get("properties", {}).get("title") == title:
                return int(sheet["properties"]["sheetId"])
        created = service.spreadsheets().batchUpdate(
            spreadsheetId=self.cfg.google_spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": title}}}]},
        ).execute()
        return int(created["replies"][0]["addSheet"]["properties"]["sheetId"])

    def _ensure_header(self, title: str) -> None:
        service = self._get_service()
        head = f"A1:{LAST_COLUMN}1"
        result = service.spreadsheets().values().get(
            spreadsheetId=self.cfg.google_spreadsheet_id, range=f"{title}!{head}"
        ).execute()
        if result.get("values"):
            return
        service.spreadsheets().values().update(
            spreadsheetId=self.cfg.google_spreadsheet_id,
            range=f"{title}!A1",
            valueInputOption="RAW",
            body={"values": [HEADERS]},
        ).execute()

    # ------------------------------------------------------------------ #
    def append(self, app: Application, account_name: str = "") -> int | None:
        """Дописывает заявку в лист её категории. Возвращает номер строки."""
        if not self.ready:
            raise SheetsError(self._error)
        title = self._sheet_title(app.category)
        try:
            self._ensure_sheet(title)
            self._ensure_header(title)
            service = self._get_service()
            service.spreadsheets().values().append(
                spreadsheetId=self.cfg.google_spreadsheet_id,
                range=f"{title}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": [self._row(app, account_name)]},
            ).execute()
            meta = service.spreadsheets().values().get(
                spreadsheetId=self.cfg.google_spreadsheet_id, range=f"{title}!A:A"
            ).execute()
            values = meta.get("values") or []
            for index, row in enumerate(values, start=1):
                if row and str(row[0]) == app.number:
                    return index
            return len(values) or None
        except SheetsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SheetsError(redact(str(exc))) from exc

    def update_row(self, app: Application, row: int) -> None:
        """Обновляет строку заявки (статус / комментарий).

        Пишем ТОЛЬКО колонки «Статус» и «Комментарий куратора». Раньше здесь
        переписывалась вся строка A..O значениями из базы, а бот хранит не всё:
        при ``STORE_PII=0`` телефон и e-mail в базе пустые, а «Имя аккаунта»
        при обновлении передаётся пустой строкой. Любая смена статуса стирала
        то, что куратор вписал вручную или что бот записал при создании.
        """
        if not self.ready:
            raise SheetsError(self._error)
        title = self._sheet_title(app.category)
        row_values = build_row(self.cfg, app, "")
        try:
            self._get_service().spreadsheets().values().update(
                spreadsheetId=self.cfg.google_spreadsheet_id,
                range=f"{title}!{STATUS_FIRST_COLUMN}{row}:{STATUS_LAST_COLUMN}{row}",
                valueInputOption="RAW",
                body={"values": [row_values[COL_STATUS:COL_COMMENT + 1]]},
            ).execute()
        except Exception as exc:  # noqa: BLE001
            raise SheetsError(redact(str(exc))) from exc

    def read_statuses(self) -> list[tuple[str, str, str]]:
        """Читает (номер, статус, комментарий) из всех листов заявок.

        Нужно, чтобы организатор мог менять статус прямо в таблице — тогда
        участник получит сообщение без команды ``!статус`` в чате.
        """
        if not self.ready:
            raise SheetsError(self._error)
        try:
            service = self._get_service()
            meta = service.spreadsheets().get(
                spreadsheetId=self.cfg.google_spreadsheet_id, fields="sheets.properties"
            ).execute()
            out: list[tuple[str, str, str]] = []
            for sheet in meta.get("sheets", []):
                title = str(sheet.get("properties", {}).get("title") or "")
                if not title:
                    continue
                result = service.spreadsheets().values().get(
                    spreadsheetId=self.cfg.google_spreadsheet_id,
                    range=f"{title}!A1:{LAST_COLUMN}",
                ).execute()
                for row in result.get("values") or []:
                    parsed = parse_row(row)
                    if parsed:
                        out.append(parsed)
            return out
        except SheetsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SheetsError(redact(str(exc))) from exc

    def _row(self, app: Application, account_name: str) -> list[str]:
        return build_row(self.cfg, app, account_name)


def _cell(value: object) -> str:
    return clean_text("" if value is None else str(value), 300)


def parse_row(row: list[Any]) -> tuple[str, str, str] | None:
    """Достаёт номер, статус и комментарий из строки листа.

    Колонки задаются ``HEADERS``, поэтому позиции фиксированы. Всё, что не
    распознано, игнорируется: неверная разметка не должна ломать опрос таблицы.
    """
    if len(row) < 2:
        return None
    number = _cell(row[COL_NUMBER])
    if not re.match(r"^[Зз][БбB]-\d{1,6}$", number):
        return None  # заголовок или мусор
    status = _cell(row[COL_STATUS] if len(row) > COL_STATUS else "")
    comment = _cell(row[COL_COMMENT] if len(row) > COL_COMMENT else "")
    return number.upper().replace("B", "Б"), status, comment


def build_row(cfg: Config, app: Application, account_name: str) -> list[str]:
    cat = cfg.category(app.category)
    return [
        clean_text(app.number, 20),
        clean_text(app.created_at, 40),
        clean_text(cat.title if cat else app.category, 40),
        clean_text(app.full_name, 120),
        clean_text(app.city, 80),
        clean_text(app.phone, 40),
        clean_text(app.email, 120),
        clean_text(app.description, 1500),
        clean_text(app.link, 512),
        clean_text(app.attachment, 300),
        clean_text(app.day, 20),
        clean_text(app.status, 30) or STATUS_NEW,
        clean_text(app.curator_comment, 500),
        str(app.user_id),
        clean_text(account_name, 120),
    ]


# --------------------------------------------------------------------------- #
# Резервный экспорт в CSV
# --------------------------------------------------------------------------- #

class CsvFallback:
    """Работает всегда: даёт организатору выгрузку, даже если Sheets недоступны."""

    def __init__(self, path: Path, cfg: Config) -> None:
        self.path = path
        self.cfg = cfg
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, app: Application, account_name: str = "") -> None:
        exists = self.path.exists()
        with self.path.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            if not exists:
                writer.writerow(HEADERS)
            writer.writerow(build_row(self.cfg, app, account_name))

    def remove(self, number: str) -> bool:
        """Убирает заявку из выгрузки.

        Файл открыт на дописывание, поэтому удалённая строка осталась бы в
        нём навсегда: организатор открывает CSV и видит заявку, которой в базе
        уже нет. Переписываем файл целиком.

        Заголовок ищется по совпадению с ``HEADERS``, а не по позиции: файл
        мог быть создан без него, и тогда первая строка — это данные. Считать
        её заголовком нельзя, иначе заявка не удалится, а шапка таблицы
        пропадёт.
        """
        if not self.path.exists():
            return False
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle, delimiter=";"))
        if not rows:
            return False
        body = rows[1:] if rows[0] == list(HEADERS) else rows
        kept = [r for r in body if not r or r[0].strip() != number]
        if len(kept) == len(body):
            return False
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";", quoting=csv.QUOTE_MINIMAL)
            # Заголовок пишем всегда, даже если его не было. ``append`` создаёт
            # шапку только в новом файле, а если оставить файл пустым, то
            # после следующей записи заголовка не будет в нём опять.
            writer.writerow(HEADERS)
            writer.writerows(kept)
        return True
