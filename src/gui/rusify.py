"""Русификация в фоне: QThread-воркер и диалог предпросмотра замен.

Вызов словаря/LLM не должен блокировать интерфейс, поэтому работа идёт в
``RusifyWorker`` (QThread). Результат не применяется автоматически: пользователь
сначала видит список замен и снимает галочки с ненужных.
"""

from __future__ import annotations

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

from ..glossary_manager import GlossaryManager
from ..replacement_engine import ReplacementEngine

#: Корень проекта (``data/glossary.json`` лежит рядом с ``src``).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GLOSSARY = PROJECT_ROOT / "data" / "glossary.json"
DEFAULT_EXCEPTIONS = PROJECT_ROOT / "data" / "exceptions.json"


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
    ) -> None:
        """Создать воркер русификации.

        Args:
            text: Исходный текст документа.
            engine: Готовый движок замен (для тестов можно передать двойник).
            parent: Родительский объект Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self._text = text
        self._engine = engine if engine is not None else _default_engine()

    def run(self) -> None:
        """Выполнить замены и отправить результат сигналом.

        Returns:
            None

        Raises:
            Ничего не бросает: исключение превращается в сигнал ``failed``.
        """
        try:
            self.progress.emit(10)
            new_text, changes = self._engine.replace_dict_only(self._text)
            self.progress.emit(100)
            self.result_ready.emit(new_text, list(changes))
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            self.failed.emit(str(exc))


def _default_engine() -> ReplacementEngine:
    """Создать движок на проектных словарях.

    Returns:
        ReplacementEngine: движок словарных замен.

    Raises:
        GlossaryError: словари проекта не читаются.
    """
    manager = GlossaryManager(str(DEFAULT_GLOSSARY), str(DEFAULT_EXCEPTIONS))
    return ReplacementEngine(manager)


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

        self.table = QTableWidget(len(changes), 3, self)
        self.table.setHorizontalHeaderLabels(["Оригинал", "Замена", "Применить"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)

        for row, change in enumerate(changes):
            original = QTableWidgetItem(str(change.get("original", "")))
            replacement = QTableWidgetItem(str(change.get("replacement", "")))
            toggle = QTableWidgetItem("")
            toggle.setFlags(
                toggle.flags()
                | Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
            )
            toggle.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(row, 0, original)
            self.table.setItem(row, 1, replacement)
            self.table.setItem(row, 2, toggle)

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
            toggle = self.table.item(row, 2)
            if toggle is not None and toggle.checkState() == Qt.CheckState.Checked:
                selected.append(
                    (str(change.get("original", "")), str(change.get("replacement", "")))
                )
        return selected

