"""Диалог редактирования словаря замен и списка исключений.

Две вкладки-таблицы: «Словарь» (английский термин → русская замена) и
«Исключения» (слова и аббревиатуры, которые не переводятся). Данные читаются
из :class:`~src.glossary_manager.GlossaryManager`, а сохранение идёт через его
метод ``save``: он валидирует результат и сразу перечитывает файлы, поэтому
новые слова работают при следующей русификации без перезапуска приложения.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..glossary_manager import GlossaryError, GlossaryManager

#: Подпись вкладки глоссария.
GLOSSARY_TAB_TITLE = "Словарь"

#: Подпись вкладки исключений.
EXCEPTIONS_TAB_TITLE = "Исключения"

#: Заголовки колонок таблицы глоссария.
GLOSSARY_HEADERS = ("Английский", "Русский")

#: Заголовок единственной колонки исключений.
EXCEPTIONS_HEADERS = ("Слово / Аббревиатура",)

#: Доступные способы начать редактирование ячейки.
_EDIT_TRIGGERS = (
    QAbstractItemView.EditTrigger.DoubleClicked
    | QAbstractItemView.EditTrigger.SelectedClicked
    | QAbstractItemView.EditTrigger.EditKeyPressed
    | QAbstractItemView.EditTrigger.AnyKeyPressed
)


class GlossaryEditorDialog(QDialog):
    """Редактор ``glossary.json`` и ``exceptions.json``.

    Результат сообщается кодом возврата ``QDialog.DialogCode.Accepted`` —
    диалог закрывается им только после успешной записи файлов.

    Args:
        glossary: Менеджер глоссария, чьи файлы редактируются.
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    def __init__(
        self,
        glossary: GlossaryManager,
        parent: QWidget | None = None,
    ) -> None:
        """Собрать диалог и заполнить таблицы текущими данными.

        Args:
            glossary: Менеджер глоссария (источник и приёмник данных).
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.glossary = glossary
        self.setWindowTitle("Редактор словаря")
        self.resize(720, 520)
        self._build_ui()
        self.reload_rows()

    # -- сборка интерфейса ---------------------------------------------------
    def _build_ui(self) -> None:
        """Создать вкладки с таблицами, панели кнопок и подписи.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("glossaryTabs")

        self.glossary_table = self._make_table(list(GLOSSARY_HEADERS), "glossaryTable")
        self.tabs.addTab(self._make_tab(self.glossary_table), GLOSSARY_TAB_TITLE)

        self.exceptions_table = self._make_table(
            list(EXCEPTIONS_HEADERS), "exceptionsTable"
        )
        self.tabs.addTab(self._make_tab(self.exceptions_table), EXCEPTIONS_TAB_TITLE)
        layout.addWidget(self.tabs)

        self.hint = QLabel(
            "Двойной клик по ячейке — правка. Термины ищутся целыми словами; "
            "исключения не переводятся (регистр не важен).",
            self,
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.hint)

        self.counts = QLabel("", self)
        self.counts.setObjectName("glossaryCounts")
        layout.addWidget(self.counts)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        self.save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("Сохранить")
        self.save_button.setObjectName("saveGlossary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons

        self.tabs.currentChanged.connect(self._refresh_counts)

    def _make_table(self, headers: list[str], name: str) -> QTableWidget:
        """Создать таблицу с растянутыми колонками и правкой по клику.

        Args:
            headers: Заголовки колонок.
            name: ``objectName`` для поиска в тестах.

        Returns:
            QTableWidget: пустая таблица.

        Raises:
            Ничего не бросает.
        """
        table = QTableWidget(0, len(headers), self)
        table.setObjectName(name)
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(_EDIT_TRIGGERS)
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        for column in range(len(headers)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        return table

    def _make_tab(self, table: QTableWidget) -> QWidget:
        """Обернуть таблицу во вкладку с собственной панелью кнопок.

        Args:
            table: Таблица этой вкладки.

        Returns:
            QWidget: виджет вкладки с кнопками «Добавить» и «Удалить».

        Raises:
            Ничего не бросает.
        """
        index = self.tabs.count()
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(6, 6, 6, 6)

        toolbar = QHBoxLayout()
        add_button = QPushButton("Добавить", page)
        add_button.setObjectName(f"addRow{index}")
        remove_button = QPushButton("Удалить", page)
        remove_button.setObjectName(f"removeRow{index}")
        toolbar.addWidget(add_button)
        toolbar.addWidget(remove_button)
        toolbar.addStretch(1)
        page_layout.addLayout(toolbar)
        page_layout.addWidget(table)

        add_button.clicked.connect(lambda: self.add_row(table))
        remove_button.clicked.connect(lambda: self.remove_selected_rows(table))
        return page

    # -- данные --------------------------------------------------------------
    def reload_rows(self) -> None:
        """Заполнить таблицы данными менеджера глоссария.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.glossary_table.setRowCount(0)
        for term, translation in self.glossary.glossary.items():
            row = self.glossary_table.rowCount()
            self.glossary_table.insertRow(row)
            self.glossary_table.setItem(row, 0, QTableWidgetItem(term))
            self.glossary_table.setItem(row, 1, QTableWidgetItem(translation))

        self.exceptions_table.setRowCount(0)
        for token in sorted(self.glossary.exceptions, key=str.lower):
            row = self.exceptions_table.rowCount()
            self.exceptions_table.insertRow(row)
            self.exceptions_table.setItem(row, 0, QTableWidgetItem(token))

        self._refresh_counts()

    def add_row(self, table: QTableWidget) -> int:
        """Добавить пустую строку и начать её редактирование.

        Args:
            table: Таблица, в которую добавляется строка.

        Returns:
            int: Индекс новой строки.

        Raises:
            Ничего не бросает.
        """
        row = table.rowCount()
        table.insertRow(row)
        for column in range(table.columnCount()):
            table.setItem(row, column, QTableWidgetItem(""))
        table.setCurrentCell(row, 0)
        table.scrollToBottom()
        item = table.item(row, 0)
        if item is not None:
            table.editItem(item)
        self._refresh_counts()
        return row

    def remove_selected_rows(self, table: QTableWidget) -> int:
        """Удалить выделенные строки таблицы.

        Args:
            table: Таблица, из которой удаляются строки.

        Returns:
            int: Сколько строк удалено.

        Raises:
            Ничего не бросает.
        """
        rows = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
        if not rows and table.currentRow() >= 0:
            rows = [table.currentRow()]
        for row in rows:
            table.removeRow(row)
        if rows:
            self._refresh_counts()
        return len(rows)

    @staticmethod
    def _cell_text(table: QTableWidget, row: int, column: int) -> str:
        """Текст ячейки без лишних пробелов.

        Args:
            table: Таблица.
            row: Индекс строки.
            column: Индекс колонки.

        Returns:
            str: Текст ячейки (``""``, если ячейки нет).

        Raises:
            Ничего не бросает.
        """
        item = table.item(row, column)
        return (item.text() if item is not None else "").strip()

    def collect_glossary(self) -> dict[str, str]:
        """Собрать глоссарий из таблицы.

        Returns:
            dict[str, str]: ``{термин: замена}`` в порядке строк.

        Raises:
            GlossaryError: строка заполнена наполовину, термин повторяется или
                термины различаются только регистром.
        """
        result: dict[str, str] = {}
        seen: dict[str, int] = {}
        for row in range(self.glossary_table.rowCount()):
            term = self._cell_text(self.glossary_table, row, 0)
            translation = self._cell_text(self.glossary_table, row, 1)
            if not term and not translation:
                continue
            if not term:
                raise GlossaryError(f"Строка {row + 1}: не заполнен английский термин")
            if not translation:
                raise GlossaryError(f"Строка {row + 1}: нет перевода для {term!r}")
            key = term.lower()
            if key in seen:
                raise GlossaryError(
                    f"Термин {term!r} повторяется (строки {seen[key]} и {row + 1})"
                )
            seen[key] = row + 1
            result[term] = translation
        return result

    def collect_exceptions(self) -> list[str]:
        """Собрать исключения из таблицы.

        Returns:
            list[str]: токены в порядке строк (пустые строки пропускаются).

        Raises:
            GlossaryError: токен повторяется (регистр не важен).
        """
        tokens: list[str] = []
        seen: dict[str, int] = {}
        for row in range(self.exceptions_table.rowCount()):
            token = self._cell_text(self.exceptions_table, row, 0)
            if not token:
                continue
            key = token.lower()
            if key in seen:
                raise GlossaryError(
                    f"Исключение {token!r} повторяется (строки {seen[key]} и {row + 1})"
                )
            seen[key] = row + 1
            tokens.append(token)
        return tokens

    # -- сохранение ----------------------------------------------------------
    def save(self) -> bool:
        """Записать таблицы в JSON-файлы и перечитать их.

        Returns:
            bool: ``True`` при успехе (диалог закрывается с ``Accepted``).

        Raises:
            Ничего не бросает: ошибки показываются диалогом.
        """
        try:
            glossary = self.collect_glossary()
            exceptions = self.collect_exceptions()
            self.glossary.save(glossary, exceptions)
        except GlossaryError as exc:
            QMessageBox.warning(self, "Словарь не сохранён", str(exc))
            return False
        except OSError as exc:
            QMessageBox.critical(self, "Не удалось записать файлы словаря", str(exc))
            return False

        self.reload_rows()
        self.accept()
        return True

    def _refresh_counts(self, *_: object) -> None:
        """Обновить подпись с количеством строк.

        Args:
            *_: Аргументы сигнала (не используются).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.counts.setText(
            f"Терминов: {self.glossary_table.rowCount()} · "
            f"исключений: {self.exceptions_table.rowCount()}"
        )
