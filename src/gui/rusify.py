"""Русификация в фоне: QThread-воркер и диалог предпросмотра замен.

Вызов словаря/LLM не должен блокировать интерфейс, поэтому работа идёт в
``RusifyWorker`` (QThread). Результат не применяется автоматически: пользователь
сначала видит список замен и снимает галочки с ненужных.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..deepseek_client import DeepSeekClient, DeepSeekError
from ..glossary_manager import GlossaryManager
from ..replacement_engine import ReplacementEngine

#: Корень проекта (``data/glossary.json`` лежит рядом с ``src``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY = PROJECT_ROOT / "data" / "glossary.json"
DEFAULT_EXCEPTIONS = PROJECT_ROOT / "data" / "exceptions.json"

#: Сколько символов отправлять в одном запросе к LLM.
LLM_BATCH_CHARS = 1500

#: Слово вместе с хвостовыми пробелами: diff сохраняет разбиение по словам.
_WORD_RE = re.compile(r"\S+\s*")

#: Подписи источников замен в таблице предпросмотра.
_SOURCE_LABELS = {"dict": "Словарь", "llm": "ИИ"}


class RusifyWorker(QThread):
    """Прогнать текст через движок замен в отдельном потоке.

    Название сигнала ``result_ready`` (а не ``finished``) выбрано намеренно:
    ``QThread.finished`` — встроенный сигнал, переопределять его нельзя.

    Signals:
        progress: процент выполнения (0–100).
        result_ready: ``(новый текст, список замен)``.
        failed: текст ошибки, если движок упал.

    Args:
        text: Исходный текст документа.
        engine: Движок замен; ``None`` — создать по проектным словарям.
        parent: Родительский объект Qt.

    Raises:
        Ничего не бросает.
    """

    progress = Signal(int)
    result_ready = Signal(str, object)
    failed = Signal(str)

    def __init__(
        self,
        text: str,
        engine: ReplacementEngine | None = None,
        parent: QWidget | None = None,
        *,
        use_llm: bool = False,
        api_key: str = "",
        glossary: GlossaryManager | None = None,
    ) -> None:
        """Создать воркер русификации.

        Args:
            text: Исходный текст документа.
            engine: Готовый движок замен (для тестов можно передать двойник).
            parent: Родительский объект Qt.
            use_llm: Дополнительно прогнать текст через DeepSeek.
            api_key: Ключ API; без него режим ИИ не включается.
            glossary: Общий менеджер глоссария; ``None`` — перечитать файлы.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self._text = text
        self._glossary = glossary if glossary is not None else _default_glossary()
        self._engine = engine if engine is not None else ReplacementEngine(self._glossary)
        self._use_llm = bool(use_llm) and bool(api_key.strip())
        self._api_key = api_key.strip()

    def run(self) -> None:
        """Выполнить замены и отправить результат сигналом.

        Сначала идёт словарный проход, затем (если включён ИИ) — батчи в DeepSeek
        с построением diff-пар. Ошибки API превращаются в сигнал ``failed``,
        поэтому приложение не падает даже без интернета.

        Returns:
            None

        Raises:
            Ничего не бросает: исключение превращается в сигнал ``failed``.
        """
        try:
            self.progress.emit(10)
            text_after_dict, changes = self._engine.replace_dict_only(self._text)
            self.progress.emit(40)
            if self._use_llm:
                text_after_dict, llm_changes = self._apply_llm(text_after_dict)
                changes = list(changes) + llm_changes
            self.progress.emit(100)
            self.result_ready.emit(text_after_dict, list(changes))
        except DeepSeekError as exc:
            self.failed.emit(f"Ошибка API: {exc}")
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            self.failed.emit(str(exc))

    def _apply_llm(self, text: str) -> tuple[str, list[dict]]:
        """Прогнать текст через DeepSeek батчами и построить diff-пары.

        Args:
            text: Текст после словарного прохода.

        Returns:
            tuple[str, list[dict]]: (текст после ИИ, пары ``source="llm"``).

        Raises:
            DeepSeekError: ошибка конфигурации или API (обрабатывается в ``run``).
        """
        client = DeepSeekClient(api_key=self._api_key)
        glossary = dict(self._glossary.glossary)
        exceptions = dict.fromkeys(self._glossary.exceptions, None)

        batches = _split_batches(text, LLM_BATCH_CHARS)
        fixed_parts: list[str] = []
        changes: list[dict] = []
        for index, batch in enumerate(batches):
            self.progress.emit(40 + int(55 * index / max(1, len(batches))))
            fixed = client.replace_text(
                batch, glossary, exceptions, block_id=f"gui-{index}"
            )
            changes.extend(_diff_pairs(batch, fixed))
            fixed_parts.append(fixed)
        return "\n".join(fixed_parts), changes


def _default_glossary() -> GlossaryManager:
    """Загрузить проектные словари.

    Returns:
        GlossaryManager: глоссарий и исключения проекта.

    Raises:
        GlossaryError: словари проекта не читаются.
    """
    return GlossaryManager(str(DEFAULT_GLOSSARY), str(DEFAULT_EXCEPTIONS))


def _split_batches(text: str, limit: int) -> list[str]:
    """Разбить текст на батчи по абзацам, не превышая ``limit`` символов.

    Args:
        text: Исходный текст.
        limit: Максимальный размер батча в символах.

    Returns:
        list[str]: непустые батчи в исходном порядке.

    Raises:
        Ничего не бросает.
    """
    batches: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        piece = paragraph if not current else "\n" + paragraph
        if current and len(current) + len(piece) > limit:
            batches.append(current)
            current = paragraph
        else:
            current += piece
    if current:
        batches.append(current)
    return batches or [text]


def _diff_pairs(before: str, after: str, source: str = "llm") -> list[dict]:
    """Построить пары «было → стало» по различию текстов.

    Сравнение идёт по словам, а соседние изменённые участки склеиваются, если
    между ними не больше двух неизменных слов: иначе одна правка распадалась бы
    на десяток строк вида «is → ""». Пары применяются к редактору поиском
    подстроки, поэтому склеенные фразы находятся надёжнее отдельных слов.

    Args:
        before: Текст до ИИ (после словаря).
        after: Текст после ИИ.
        source: Метка источника для таблицы предпросмотра.

    Returns:
        list[dict]: ``{"original", "replacement", "source", "position"}``.

    Raises:
        Ничего не бросает.
    """
    before_words = _WORD_RE.findall(before)
    after_words = _WORD_RE.findall(after)
    if before_words == after_words:
        return []

    matcher = difflib.SequenceMatcher(None, before_words, after_words, autojunk=False)
    merged: list[tuple[int, int, int, int]] = []
    pending: tuple[int, int, int, int] | None = None
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            if pending is None:
                continue
            if (i2 - i1) <= 2:  # короткий общий кусок внутри правки
                pending = (pending[0], i2, pending[2], j2)
                continue
            merged.append(pending)
            pending = None
            continue
        pending = (i1, i2, j1, j2) if pending is None else (pending[0], i2, pending[2], j2)
    if pending is not None:
        merged.append(pending)

    pairs: list[dict] = []
    for i1, i2, j1, j2 in merged:
        original = "".join(before_words[i1:i2]).strip()
        replacement = "".join(after_words[j1:j2]).strip()
        if not original and not replacement:
            continue
        pairs.append(
            {
                "original": original,
                "replacement": replacement,
                "source": source,
                "position": i1,
            }
        )
    return pairs


class ReplacementsDialog(QDialog):
    """Предпросмотр замен: таблица «Оригинал / Замена / Применить».

    Args:
        changes: Записи движка (``{"original", "replacement", ...}``).
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    def __init__(self, changes: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        """Собрать диалог по списку замен.

        Args:
            changes: Записи движка в порядке появления в документе.
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setWindowTitle("Русификация документа")
        self.resize(680, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                f"Найдено замен: {len(changes)}. Снимите галочки с тех, "
                "которые не нужно применять.",
                self,
            )
        )

        self.table = QTableWidget(len(changes), 4, self)
        self.table.setHorizontalHeaderLabels(
            ["Оригинал", "Замена", "Источник", "Применить"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        for row, change in enumerate(changes):
            original = QTableWidgetItem(str(change.get("original", "")))
            replacement = QTableWidgetItem(str(change.get("replacement", "")))
            source = QTableWidgetItem(_SOURCE_LABELS.get(str(change.get("source")), "—"))
            toggle = QTableWidgetItem("")
            toggle.setFlags(
                toggle.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            toggle.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, original)
            self.table.setItem(row, 1, replacement)
            self.table.setItem(row, 2, source)
            self.table.setItem(row, 3, toggle)

        self._changes = changes
        layout.addWidget(self.table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_replacements(self) -> list[tuple[str, str]]:
        """Пары «оригинал → замена» для отмеченных строк.

        Returns:
            list[tuple[str, str]]: выбранные замены в порядке таблицы.

        Raises:
            Ничего не бросает.
        """
        selected: list[tuple[str, str]] = []
        for row, change in enumerate(self._changes):
            toggle = self.table.item(row, 3)
            if toggle is not None and toggle.checkState() == Qt.CheckState.Checked:
                selected.append(
                    (str(change.get("original", "")), str(change.get("replacement", "")))
                )
        return selected

