"""Главное окно TermRu: меню, панель инструментов, редактор, статус-бар."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QStyle,
    QToolBar,
    QWidget,
)

from .docx_editor import DocxEditor

APP_TITLE = "TermRu — Редактор документов"
DOCX_FILTER = "Документы Word (*.docx)"
NEW_DOCUMENT = "Новый документ"


class MainWindow(QMainWindow):
    """Окно редактора с меню, панелью инструментов и статус-баром.

    Args:
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Собрать интерфейс главного окна.

        Args:
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.resize(1100, 760)
        self.editor = DocxEditor(self)
        self.setCentralWidget(self.editor)

        self._build_actions()
        self._build_menus()
        self._build_toolbar()
        self._build_statusbar()

        self.editor.textChanged.connect(self._refresh_status)
        self.editor.document().modificationChanged.connect(self._refresh_title)
        # Сигналы редактора обновляют шапку и статус при любом пути загрузки:
        # и через диалог, и программно (например, из тестов или будущего CLI-GUI).
        self.editor.file_loaded.connect(self._on_file_changed)
        self.editor.file_saved.connect(self._on_file_changed)
        self._refresh_title()
        self._refresh_status()

    def _on_file_changed(self, _path: str) -> None:
        """Обновить заголовок и статус после открытия/сохранения файла.

        Args:
            _path: Путь файла (значение берётся из редактора).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._refresh_title()
        self._refresh_status()

    # -- сборка интерфейса ---------------------------------------------------
    def _build_actions(self) -> None:
        """Создать действия меню и панели инструментов.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        style = self.style()
        self.action_open = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton),
            "Открыть",
            self,
        )
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.triggered.connect(self.open_document)

        self.action_save = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton),
            "Сохранить",
            self,
        )
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.triggered.connect(self.save_document)

        self.action_save_as = QAction("Сохранить как…", self)
        self.action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.action_save_as.triggered.connect(self.save_document_as)

        self.action_quit = QAction("Выход", self)
        self.action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.action_quit.triggered.connect(self.close)

        self.action_russify = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload),
            "Русифицировать документ",
            self,
        )
        self.action_russify.triggered.connect(self.russify_document)

        self.action_about = QAction("О программе", self)
        self.action_about.triggered.connect(self.show_about)

    def _build_menus(self) -> None:
        """Создать строку меню.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&Файл")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        file_menu.addAction(self.action_save_as)
        file_menu.addSeparator()
        file_menu.addAction(self.action_quit)

        edit_menu = menubar.addMenu("&Правка")
        undo = edit_menu.addAction("Отменить")
        undo.setShortcut(QKeySequence.StandardKey.Undo)
        undo.triggered.connect(self.editor.undo)
        redo = edit_menu.addAction("Вернуть")
        redo.setShortcut(QKeySequence.StandardKey.Redo)
        redo.triggered.connect(self.editor.redo)
        edit_menu.addSeparator()
        cut = edit_menu.addAction("Вырезать")
        cut.setShortcut(QKeySequence.StandardKey.Cut)
        cut.triggered.connect(self.editor.cut)
        copy = edit_menu.addAction("Копировать")
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(self.editor.copy)
        paste = edit_menu.addAction("Вставить")
        paste.setShortcut(QKeySequence.StandardKey.Paste)
        paste.triggered.connect(self.editor.paste)

        tools_menu = menubar.addMenu("&Инструменты")
        tools_menu.addAction(self.action_russify)

        help_menu = menubar.addMenu("С&правка")
        help_menu.addAction(self.action_about)

    def _build_toolbar(self) -> None:
        """Создать панель инструментов с основными действиями.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        toolbar = QToolBar("Основная", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.addAction(self.action_open)
        toolbar.addAction(self.action_save)
        toolbar.addSeparator()
        toolbar.addAction(self.action_russify)
        self.addToolBar(toolbar)
        self.toolbar = toolbar

    def _build_statusbar(self) -> None:
        """Создать статус-бар: путь слева, счётчики справа.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.status_path = QLabel(NEW_DOCUMENT, self)
        self.status_counts = QLabel("", self)
        self.statusBar().addWidget(self.status_path, 1)
        self.statusBar().addPermanentWidget(self.status_counts)

    # -- состояние -----------------------------------------------------------
    def _refresh_title(self, *_: object) -> None:
        """Обновить заголовок окна по текущему файлу и флагу изменений.

        Args:
            *_: Аргументы сигнала Qt (``modificationChanged``), не используются.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        path = self.editor.current_path
        name = path.name if path is not None else NEW_DOCUMENT
        self.setWindowTitle(f"{APP_TITLE} — {name}[*]")
        self.setWindowModified(self.editor.is_modified())

    def _refresh_status(self) -> None:
        """Обновить путь и счётчики слов/символов в статус-баре.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        path = self.editor.current_path
        self.status_path.setText(str(path) if path is not None else NEW_DOCUMENT)
        text = self.editor.toPlainText()
        words = len(text.split())
        self.status_counts.setText(f"Слов: {words} · Символов: {len(text)}")

    # -- файлы ---------------------------------------------------------------
    def open_document(self) -> None:
        """Открыть DOCX через диалог выбора файла.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        filepath, _ = QFileDialog.getOpenFileName(self, "Открыть документ", "", DOCX_FILTER)
        if not filepath:
            return
        if self.editor.load_file(filepath):
            self._refresh_title()
            self._refresh_status()
            self.statusBar().showMessage("Документ открыт", 3000)
        else:
            self.statusBar().showMessage("Не удалось открыть документ", 5000)

    def save_document(self) -> bool:
        """Сохранить документ; если пути нет — вызвать «Сохранить как».

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        if self.editor.current_path is None:
            return self.save_document_as()
        if not self.editor.save_file(str(self.editor.current_path)):
            self.statusBar().showMessage("Не удалось сохранить документ", 5000)
            return False
        self._refresh_title()
        self.statusBar().showMessage("Документ сохранён", 3000)
        return True

    def save_document_as(self) -> bool:
        """Сохранить под новым именем (расширение ``.docx`` добавляется).

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        filepath, _ = QFileDialog.getSaveFileName(self, "Сохранить как", "", DOCX_FILTER)
        if not filepath:
            return False
        if not filepath.lower().endswith(".docx"):
            filepath += ".docx"
        if not self.editor.save_file(filepath):
            self.statusBar().showMessage("Не удалось сохранить документ", 5000)
            return False
        self._refresh_title()
        self._refresh_status()
        self.statusBar().showMessage(f"Сохранено: {Path(filepath).name}", 3000)
        return True

    # -- инструменты ---------------------------------------------------------
    def russify_document(self) -> None:
        """Заглушка «Русифицировать документ» (модуль подключается позже).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        QMessageBox.information(
            self,
            "Русификация документа",
            "Модуль русификации в разработке: он будет использовать существующий\n"
            "словарь и DeepSeek-клиент TermRu для замены англицизмов.",
        )

    def show_about(self) -> None:
        """Показать окно «О программе».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        from .. import __version__

        QMessageBox.about(
            self,
            "О программе",
            f"<b>TermRu</b> {__version__}<br>"
            "Редактор документов с заменой англицизмов на русские аналоги.<br><br>"
            "DOCX, PPTX и PDF · словарь + DeepSeek",
        )

    # -- закрытие ------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - API Qt
        """Спросить о сохранении несохранённых изменений перед закрытием.

        Args:
            event: Событие закрытия окна.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if not self.editor.is_modified():
            event.accept()
            return

        answer = QMessageBox.question(
            self,
            "Несохранённые изменения",
            "Сохранить изменения в документе?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            event.ignore()
            return
        if answer == QMessageBox.StandardButton.Save and not self.save_document():
            event.ignore()
            return
        event.accept()


