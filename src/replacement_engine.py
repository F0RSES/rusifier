"""Движок замены англицизмов на русские аналоги.

Детерминированный словарный режим (:meth:`ReplacementEngine.replace_dict_only`)
работает до вызова LLM и не зависит от сети: он находит термины глоссария целыми
словами, подгоняет регистр под найденное написание и никогда не трогает
исключения (``TLS``, ``KPI`` и т.п.).

Как разрешаются пересечения
---------------------------
1. :class:`~src.glossary_manager.GlossaryManager` отбирает термины, реально
   встречающиеся в тексте, в порядке убывания длины.
2. Для каждого термина шаблон ``\\bтермин\\b`` (``re.IGNORECASE``) находит все
   вхождения в **исходном** тексте.
3. Кандидаты сортируются по позиции, а внутри одной позиции — по убыванию длины,
   поэтому ``product roadmap`` выигрывает у ``roadmap``.
4. Результат собирается одним проходом по исходному тексту.

Поскольку поиск идёт только по исходному тексту, а вставленные замены повторно
не сканируются, замена, содержащая сам термин (``meeting → встреча (meeting)``),
не приводит к бесконечному циклу или каскадной замене.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from .glossary_manager import GlossaryManager

__all__ = ["ReplacementEngine"]

LOGGER = logging.getLogger(__name__)


class ReplacementEngine:
    """Замена англицизмов по глоссарию с сохранением регистра.

    Пример:
        >>> engine = ReplacementEngine(manager)
        >>> text, changes = engine.replace_dict_only("Our feedback is ready")
        >>> text
        'Our обратная связь is ready'
        >>> changes[0]["position"]
        4

    Args:
        glossary: Загруженный :class:`~src.glossary_manager.GlossaryManager`.
        deepseek_client: Необязательный клиент LLM для этапа 3. При ``None``
            доступен только словарный режим.
        log_callback: Необязательный ``callable(level: str, msg: str)``; уровни —
            ``"debug"``, ``"info"``, ``"warning"``, ``"error"``.
    """

    def __init__(
        self,
        glossary: GlossaryManager,
        deepseek_client: Any | None = None,
        log_callback: Callable[[str, str], None] | None = None,
    ) -> None:
        """Создать движок замены.

        Args:
            glossary: Менеджер глоссария и исключений.
            deepseek_client: Необязательный клиент LLM (этап 3).
            log_callback: Необязательный обработчик сообщений.

        Returns:
            None

        Raises:
            TypeError: если ``glossary`` не является :class:`GlossaryManager`.
        """
        if not isinstance(glossary, GlossaryManager):
            raise TypeError(
                "glossary должен быть GlossaryManager, "
                f"получено {type(glossary).__name__}"
            )
        self._glossary = glossary
        self._deepseek_client = deepseek_client
        self._log_callback = log_callback

    @property
    def has_llm_client(self) -> bool:
        """Есть ли клиент LLM для :meth:`replace_with_llm`.

        Returns:
            bool: ``True``, если ``deepseek_client`` передан в конструктор.

        Raises:
            Ничего не бросает.
        """
        return self._deepseek_client is not None

    # -- словарный режим -----------------------------------------------------

    def replace_dict_only(self, text: str) -> tuple[str, list[dict]]:
        """Заменить термины глоссария без обращения к LLM.

        Args:
            text: Исходный текст блока (абзац, заголовок, ячейка таблицы).

        Returns:
            tuple[str, list[dict]]: ``(new_text, changes)``, где ``changes`` —
            список ``{"original": str, "replacement": str, "source": "dict",
            "position": int}`` в порядке появления. ``original`` — найденное
            написание, ``replacement`` — фактически вставленный текст (с учётом
            регистра), ``position`` — индекс найденного термина в **исходном**
            тексте.

        Raises:
            TypeError: если ``text`` не является строкой.
        """
        if not isinstance(text, str):
            raise TypeError(f"text должен быть строкой, получено {type(text).__name__}")
        if not text:
            return "", []

        matches = self._collect_matches(text)
        if not matches:
            return text, []

        pieces: list[str] = []
        changes: list[dict] = []
        cursor = 0

        for position, end, original, replacement in matches:
            applied = self._apply_case(original, replacement)
            pieces.append(text[cursor:position])
            pieces.append(applied)
            changes.append(
                {
                    "original": original,
                    "replacement": applied,
                    "source": "dict",
                    "position": position,
                }
            )
            self._log("info", f"{original!r} → {applied!r} @ {position}")
            cursor = end

        pieces.append(text[cursor:])
        return "".join(pieces), changes

    # -- внутренняя механика -------------------------------------------------

    def _collect_matches(self, text: str) -> list[tuple[int, int, str, str]]:
        """Собрать непересекающиеся совпадения терминов.

        Термины перебираются в порядке убывания длины (его задаёт
        :class:`GlossaryManager`), затем кандидаты сортируются по позиции, а при
        равной позиции — по убыванию длины. Левое совпадение выигрывает у
        перекрывающегося: ``product roadmap`` поглощает ``roadmap``.

        Args:
            text: Исходный текст.

        Returns:
            list[tuple[int, int, str, str]]: ``(начало, конец, найденный текст,
            замена)`` в порядке возрастания позиции.

        Raises:
            Ничего не бросает.
        """
        candidates: list[tuple[int, int, str, str]] = []

        for term, replacement in self._glossary.get_replacements(text).items():
            pattern = self._glossary.pattern_for(term)
            if pattern is None:  # защита от рассинхронизации кэша и глоссария
                pattern = self._compile(term)
            for match in pattern.finditer(text):
                original = match.group(0)
                if self._glossary.is_exception(original):
                    self._log("debug", f"{original!r}: исключение, пропущено")
                    continue
                candidates.append((match.start(), match.end(), original, replacement))

        # Позиция важнее длины: идём слева направо, при равном начале — длинный термин.
        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))

        accepted: list[tuple[int, int, str, str]] = []
        cursor = 0
        for start, end, original, replacement in candidates:
            if start < cursor:
                self._log(
                    "debug",
                    f"{original!r} @ {start}: перекрыто более длинным совпадением",
                )
                continue
            accepted.append((start, end, original, replacement))
            cursor = end

        return accepted

    def _apply_case(self, original: str, replacement: str) -> str:
        """Подогнать регистр замены под найденное написание.

        Args:
            original: Термин в том виде, как он найден в тексте.
            replacement: Замена из глоссария.

        Returns:
            str: ``Feedback`` → ``Обратная связь``, ``FEEDBACK`` → ``ОБРАТНАЯ СВЯЗЬ``,
            ``feedback`` → ``обратная связь``. Внутренний регистр замены не
            затрагивается (``capitalize()`` не используется).

        Raises:
            Ничего не бросает: при пустых аргументах возвращает замену как есть.
        """
        if not original or not replacement:
            return replacement
        if original.isupper():
            return replacement.upper()
        if original[0].isupper():
            return replacement[0].upper() + replacement[1:]
        return replacement

    def replace_with_llm(self, text: str) -> tuple[str, list[dict]]:
        """LLM-режим замены (этап 3).

        Args:
            text: Исходный текст блока.

        Returns:
            tuple[str, list[dict]]: в текущей версии не возвращает — это заглушка.

        Raises:
            NotImplementedError: всегда; появится вместе с клиентом DeepSeek на
                этапе 3.
        """
        raise NotImplementedError("LLM-режим замены появится на этапе 3")

    @staticmethod
    def _compile(term: str) -> re.Pattern[str]:
        """Собрать шаблон целого слова для термина.

        Args:
            term: Термин глоссария.

        Returns:
            re.Pattern[str]: ``\\b<термин>\\b`` без учёта регистра.

        Raises:
            re.error: если шаблон не компилируется (маловероятно из-за ``re.escape``).
        """
        return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)

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


