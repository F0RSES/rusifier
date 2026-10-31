"""Загрузка и валидация глоссария и списка исключений.

Схемы файлов
------------
``glossary.json`` — плоский объект «термин → замена»::

    {"feedback": "обратная связь", "roadmap": "дорожная карта"}

``exceptions.json`` — плоский объект, значения игнорируются::

    {"TLS": null, "KPI": null}

Дополнительно принимается устаревшая схема этапа 1 с полем ``entries``
(``[{"source": ..., "target": ...}]`` для глоссария и ``[{"token": ...}]`` для
исключений): она разворачивается в тот же плоский вид, а поля вроде
``alternatives``/``notes``/``category`` игнорируются.

Поведение
---------
* Исключения сравниваются без учёта регистра: ``KPI``, ``kpi`` и ``Kpi`` — одно
  и то же исключение.
* Термины ищутся как целые слова (границы ``\\b`` задаёт движок замены).
* :meth:`GlossaryManager.reload` перечитывает оба файла атомарно: при ошибке
  разбора прежние данные остаются в силе.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = ["GlossaryError", "GlossaryManager"]

LOGGER = logging.getLogger(__name__)


class GlossaryError(Exception):
    """Ошибка загрузки или валидации файлов глоссария/исключений.

    Attributes:
        path: Путь к проблемному файлу либо ``None``, если ошибка не привязана
            к конкретному файлу.
    """

    def __init__(self, message: str, path: str | Path | None = None) -> None:
        """Создать исключение о проблеме с файлом глоссария.

        Args:
            message: Описание проблемы.
            path: Путь к файлу, к которому относится ошибка.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.path = str(path) if path is not None else None
        super().__init__(f"{message}: {self.path}" if self.path else message)


class GlossaryManager:
    """Глоссарий замен и список исключений с поиском по тексту.

    Пример:
        >>> manager = GlossaryManager("data/glossary.json", "data/exceptions.json")
        >>> manager.get_replacements("Our feedback is ready")
        {'feedback': 'обратная связь'}
        >>> manager.is_exception("kpi")
        True

    Args:
        glossary_path: Путь к JSON-глоссарию «термин → замена».
        exceptions_path: Путь к JSON-списку исключений (значения игнорируются).
        log_callback: Необязательный ``callable(level: str, msg: str)`` для CLI.
    """

    def __init__(
        self,
        glossary_path: str,
        exceptions_path: str,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """Загрузить и провалидировать оба JSON-файла.

        Args:
            glossary_path: Путь к глоссарию.
            exceptions_path: Путь к списку исключений.
            log_callback: Необязательный обработчик сообщений.

        Returns:
            None

        Raises:
            GlossaryError: файл отсутствует, не читается, содержит некорректный
                JSON, не является объектом или нарушает схему.
        """
        self._glossary_path = Path(glossary_path)
        self._exceptions_path = Path(exceptions_path)
        self._log_callback = log_callback

        self._glossary: dict[str, str] = {}
        self._exceptions: dict[str, None] = {}
        self._exceptions_lower: frozenset[str] = frozenset()
        self._patterns: dict[str, re.Pattern[str]] = {}
        self._terms_by_length: tuple[str, ...] = ()

        self.reload()

    # -- публичное состояние ------------------------------------------------

    @property
    def glossary(self) -> Mapping[str, str]:
        """Только для чтения: ``{термин: замена}`` в порядке файла.

        Returns:
            Mapping[str, str]: неизменяемое представление глоссария.

        Raises:
            Ничего не бросает.
        """
        return MappingProxyType(self._glossary)

    @property
    def exceptions(self) -> frozenset[str]:
        """Только для чтения: исходные написания исключений.

        Returns:
            frozenset[str]: исключения в том виде, как они записаны в файле.

        Raises:
            Ничего не бросает.
        """
        return frozenset(self._exceptions)

    @property
    def terms(self) -> tuple[str, ...]:
        """Термины глоссария, отсортированные по длине по убыванию.

        Returns:
            tuple[str, ...]: порядок приоритета для движка замены; при равной
            длине — лексикографически (без учёта регистра).

        Raises:
            Ничего не бросает.
        """
        return self._terms_by_length


    # -- перезагрузка --------------------------------------------------------

    def reload(self) -> None:
        """Перечитать оба JSON-файла с диска (hot-reload для CLI).

        Новые данные применяются только после успешного разбора обоих файлов:
        при ошибке прежние глоссарий и исключения остаются в силе.

        Args:
            Нет аргументов.

        Returns:
            None

        Raises:
            GlossaryError: файл отсутствует, не читается, содержит некорректный
                JSON или нарушает схему.
        """
        glossary = self._load_glossary()
        exceptions = self._load_exceptions()

        patterns = {term: self._build_pattern(term) for term in glossary}
        ordered = tuple(sorted(glossary, key=lambda term: (-len(term), term.lower())))

        self._glossary = glossary
        self._exceptions = exceptions
        self._exceptions_lower = frozenset(name.lower() for name in exceptions)
        self._patterns = patterns
        self._terms_by_length = ordered

        self._log(
            "info",
            f"Глоссарий: {len(glossary)} терминов, исключений: {len(exceptions)}",
        )

    # -- загрузка ------------------------------------------------------------

    def _load_glossary(self) -> dict[str, str]:
        """Прочитать и провалидировать глоссарий.

        Returns:
            dict[str, str]: ``{термин: замена}`` без лишних пробелов по краям.

        Raises:
            GlossaryError: файл некорректен или нарушает схему (пустой термин,
                нестроковая/пустая замена, термины, различающиеся только регистром).
        """
        data = self._read_json(self._glossary_path)
        raw = self._flatten_glossary(data, self._glossary_path)

        glossary: dict[str, str] = {}
        seen: dict[str, str] = {}
        for term, value in raw.items():
            if not isinstance(term, str) or not term.strip():
                raise GlossaryError(f"Пустой или нестроковый термин: {term!r}", self._glossary_path)
            if not isinstance(value, str) or not value.strip():
                raise GlossaryError(
                    f"Замена для термина {term!r} должна быть непустой строкой",
                    self._glossary_path,
                )
            clean_term = term.strip()
            key = clean_term.lower()
            if key in seen:
                raise GlossaryError(
                    f"Термины {seen[key]!r} и {clean_term!r} различаются только регистром",
                    self._glossary_path,
                )
            seen[key] = clean_term
            if not self._has_word_edges(clean_term):
                self._log(
                    "warning",
                    f"Термин {clean_term!r} начинается или заканчивается не словесным "
                    "символом: границы \\b не сработают, замена может не найтись",
                )
            glossary[clean_term] = value.strip()
        return glossary

    def _load_exceptions(self) -> dict[str, None]:
        """Прочитать список исключений (значения игнорируются).

        Returns:
            dict[str, None]: исключения в исходном написании.

        Raises:
            GlossaryError: файл некорректен или содержит пустой/нестроковый токен.
        """
        data = self._read_json(self._exceptions_path)
        tokens = self._flatten_exceptions(data, self._exceptions_path)

        exceptions: dict[str, None] = {}
        seen: dict[str, str] = {}
        for token in tokens:
            if not isinstance(token, str) or not token.strip():
                raise GlossaryError(
                    f"Пустой или нестроковый токен исключения: {token!r}",
                    self._exceptions_path,
                )
            clean = token.strip()
            key = clean.lower()
            if key in seen:
                self._log(
                    "warning",
                    f"Дубликат исключения (без учёта регистра): {seen[key]!r} / {clean!r}",
                )
                continue
            seen[key] = clean
            exceptions[clean] = None
        return exceptions


    def _read_json(self, path: Path) -> dict[str, Any]:
        """Прочитать JSON-объект из файла.

        Args:
            path: Путь к файлу.

        Returns:
            dict[str, Any]: разобранный JSON-объект (может быть пустым).

        Raises:
            GlossaryError: файл отсутствует, не является файлом, не читается,
                содержит некорректный JSON или содержит не объект.
        """
        if not path.exists():
            raise GlossaryError("Файл не найден", path)
        if not path.is_file():
            raise GlossaryError("Путь не является файлом", path)
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GlossaryError(f"Не удалось прочитать файл: {exc}", path) from exc
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise GlossaryError(
                f"Некорректный JSON: {exc.msg} (строка {exc.lineno}, колонка {exc.colno})",
                path,
            ) from exc
        if not isinstance(data, dict):
            raise GlossaryError(
                f"Ожидался JSON-объект, получено {type(data).__name__}", path
            )
        if not data:
            self._log("warning", f"{path.name}: файл пуст")
        return data

    def _flatten_glossary(self, data: dict[str, Any], path: Path) -> dict[str, Any]:
        """Привести глоссарий к плоскому виду ``{термин: замена}``.

        Args:
            data: Разобранный JSON-объект.
            path: Путь к файлу (для сообщений об ошибках).

        Returns:
            dict[str, Any]: плоское отображение терминов.

        Raises:
            GlossaryError: устаревшая схема ``entries`` заполнена некорректно.
        """
        entries = data.get("entries")
        if isinstance(entries, list):
            result: dict[str, Any] = {}
            for index, item in enumerate(entries):
                if not isinstance(item, dict):
                    raise GlossaryError(f"entries[{index}] должен быть объектом", path)
                source = item.get("source")
                target = item.get("target")
                if not isinstance(source, str) or not isinstance(target, str):
                    raise GlossaryError(
                        f"entries[{index}]: нужны строковые поля source и target", path
                    )
                result[source] = target
            self._log(
                "info",
                f"{path.name}: использована устаревшая схема entries (приведена к dict)",
            )
            return result
        return dict(data)

    def _flatten_exceptions(self, data: dict[str, Any], path: Path) -> list[Any]:
        """Привести список исключений к списку токенов.

        Args:
            data: Разобранный JSON-объект.
            path: Путь к файлу (для сообщений об ошибках).

        Returns:
            list[Any]: токены в порядке файла (значения в плоской схеме игнорируются).

        Raises:
            GlossaryError: устаревшая схема ``entries`` заполнена некорректно.
        """
        entries = data.get("entries")
        if isinstance(entries, list):
            tokens: list[Any] = []
            for index, item in enumerate(entries):
                token = item.get("token") if isinstance(item, dict) else item
                if not isinstance(token, str):
                    raise GlossaryError(
                        f"entries[{index}]: нужен строковый токен (token)", path
                    )
                tokens.append(token)
            self._log(
                "info",
                f"{path.name}: использована устаревшая схема entries (приведена к dict)",
            )
            return tokens
        return list(data.keys())

    @staticmethod
    def _build_pattern(term: str) -> re.Pattern[str]:
        """Скомпилировать шаблон поиска термина как целого слова.

        Args:
            term: Термин глоссария.

        Returns:
            re.Pattern[str]: шаблон ``\\b<термин>\\b`` без учёта регистра.

        Raises:
            GlossaryError: если из термина невозможно собрать регулярное выражение.
        """
        try:
            return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)
        except re.error as exc:  # pragma: no cover - re.escape делает это маловероятным
            raise GlossaryError(f"Некорректный термин {term!r}: {exc}") from exc

    @staticmethod
    def _has_word_edges(term: str) -> bool:
        """Проверить, что термин начинается и заканчивается словесным символом.

        Для терминов вроде ``C++`` граница ``\\b`` после ``+`` не сработает,
        поэтому о них предупреждаем при загрузке.

        Args:
            term: Термин глоссария.

        Returns:
            bool: ``True``, если ``\\b`` на обеих границах термина осмысленна.

        Raises:
            Ничего не бросает.
        """

        def is_word(char: str) -> bool:
            """Проверить, что символ является словесным для регулярных выражений.

            Args:
                char: Один символ.

            Returns:
                bool: ``True`` для буквы, цифры или подчёркивания.

            Raises:
                Ничего не бросает.
            """
            return char.isalnum() or char == "_"

        return bool(term) and is_word(term[0]) and is_word(term[-1])

    # -- поиск по тексту -----------------------------------------------------

    def get_replacements(self, text: str) -> dict[str, str]:
        """Найти термины, которые встречаются в тексте целыми словами.

        Исключения не попадают в результат, даже если есть в глоссарии.

        Args:
            text: Произвольный текст (абзац, заголовок, ячейка таблицы).

        Returns:
            dict[str, str]: ``{канонический термин: замена}``. Порядок — по убыванию
            длины термина (при равной длине — лексикографически), чтобы более
            длинные словосочетания обрабатывались раньше коротких. Ключи — в том
            написании, в котором они записаны в глоссарии.

        Raises:
            TypeError: если ``text`` не является строкой.
        """
        if not isinstance(text, str):
            raise TypeError(f"text должен быть строкой, получено {type(text).__name__}")
        if not text:
            return {}

        found: dict[str, str] = {}
        for term in self._terms_by_length:
            if self.is_exception(term):
                continue
            pattern = self._patterns.get(term)
            if pattern is not None and pattern.search(text):
                found[term] = self._glossary[term]
        return found

    def is_exception(self, term: str) -> bool:
        """Проверить, исключён ли термин из замены (без учёта регистра).

        Args:
            term: Термин глоссария или слово из текста.

        Returns:
            bool: ``True``, если токен есть в exceptions.json (``KPI``, ``kpi``,
            ``Kpi`` считаются одним исключением).

        Raises:
            Ничего не бросает: значения, не являющиеся строкой, не считаются
            исключениями.
        """
        if not isinstance(term, str):
            return False
        return term.strip().lower() in self._exceptions_lower

    def pattern_for(self, term: str) -> re.Pattern[str] | None:
        """Вернуть скомпилированный шаблон термина.

        Args:
            term: Канонический термин глоссария.

        Returns:
            re.Pattern[str] | None: шаблон ``\\b<термин>\\b`` с ``re.IGNORECASE``
            либо ``None``, если такого термина нет в глоссарии.

        Raises:
            Ничего не бросает.
        """
        return self._patterns.get(term)

    # -- логирование ---------------------------------------------------------

    def _log(self, level: str, message: str) -> None:
        """Записать сообщение в ``logging`` и в ``log_callback``.

        Args:
            level: ``"debug"``, ``"info"``, ``"warning"`` или ``"error"``.
            message: Текст сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает: исключение внутри ``log_callback`` логируется и
            гасится, чтобы сбой колбэка не ломал обработку.
        """
        getattr(LOGGER, level, LOGGER.info)("%s", message)
        if self._log_callback is None:
            return
        try:
            self._log_callback(level, message)
        except Exception as exc:  # noqa: BLE001 - колбэк внешний, не ломает обработку
            LOGGER.warning("log_callback завершился ошибкой: %s", exc)




