"""Главное окно TermRu: меню, панель инструментов, редактор, статус-бар."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QFont,
    QKeySequence,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QToolBar,
    QWidget,
)

from .docx_editor import DocxEditor
from .rusify import ReplacementsDialog, RusifyWorker
from .settings import SettingsDialog, load_settings

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
        self._build_format_toolbar()
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

        self.action_settings = QAction(
            style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView),
            "Настройки…",
            self,
        )
        self.action_settings.triggered.connect(self.open_settings)

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
        tools_menu.addAction(self.action_settings)
        tools_menu.addSeparator()
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

    def _build_format_toolbar(self) -> None:
        """Создать панель форматирования: шрифт, выравнивание, списки, таблица.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        toolbar = QToolBar("Форматирование", self)
        toolbar.setObjectName("formatToolBar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)

        self.action_bold = self._make_format_action("Ж", "Жирный (Ctrl+B)", "action_bold")
        self.action_bold.setShortcut(QKeySequence.StandardKey.Bold)
        self.action_bold.setCheckable(True)
        self.action_bold.toggled.connect(self._toggle_bold)

        self.action_italic = self._make_format_action("К", "Курсив (Ctrl+I)", "action_italic")
        self.action_italic.setShortcut(QKeySequence.StandardKey.Italic)
        self.action_italic.setCheckable(True)
        self.action_italic.toggled.connect(self._toggle_italic)

        self.action_underline = self._make_format_action(
            "Ч", "Подчёркнутый (Ctrl+U)", "action_underline"
        )
        self.action_underline.setShortcut(QKeySequence.StandardKey.Underline)
        self.action_underline.setCheckable(True)
        self.action_underline.toggled.connect(self._toggle_underline)

        toolbar.addAction(self.action_bold)
        toolbar.addAction(self.action_italic)
        toolbar.addAction(self.action_underline)
        toolbar.addSeparator()

        self.align_group = QActionGroup(self)
        self.align_group.setExclusive(True)
        self.action_align_left = self._make_format_action(
            "Л", "По левому краю", "action_align_left"
        )
        self.action_align_center = self._make_format_action(
            "Ц", "По центру", "action_align_center"
        )
        self.action_align_right = self._make_format_action(
            "П", "По правому краю", "action_align_right"
        )
        for action, alignment in (
            (self.action_align_left, Qt.AlignmentFlag.AlignLeft),
            (self.action_align_center, Qt.AlignmentFlag.AlignHCenter),
            (self.action_align_right, Qt.AlignmentFlag.AlignRight),
        ):
            action.setCheckable(True)
            action.setData(alignment)
            self.align_group.addAction(action)
            action.triggered.connect(self._apply_alignment)
            toolbar.addAction(action)
        self.action_align_left.setChecked(True)

        toolbar.addSeparator()
        self.action_bullet_list = self._make_format_action(
            "• Список", "Маркированный список", "action_bullet_list"
        )
        self.action_bullet_list.triggered.connect(
            lambda: self.insert_list(QTextListFormat.Style.ListDisc)
        )
        self.action_numbered_list = self._make_format_action(
            "1. Список", "Нумерованный список", "action_numbered_list"
        )
        self.action_numbered_list.triggered.connect(
            lambda: self.insert_list(QTextListFormat.Style.ListDecimal)
        )
        toolbar.addAction(self.action_bullet_list)
        toolbar.addAction(self.action_numbered_list)

        toolbar.addSeparator()
        self.action_table = self._make_format_action(
            "Таблица", "Вставить таблицу", "action_table"
        )
        self.action_table.triggered.connect(self.insert_table_dialog)
        toolbar.addAction(self.action_table)

        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.format_toolbar = toolbar

        self.editor.cursorPositionChanged.connect(self._sync_format_actions)
        self.editor.selectionChanged.connect(self._sync_format_actions)
        self._sync_format_actions()

    def _make_format_action(self, text: str, tooltip: str, object_name: str) -> QAction:
        """Создать действие панели форматирования.

        Args:
            text: Короткая подпись кнопки (``"Ж"``, ``"Ц"``, …).
            tooltip: Подсказка с полным названием и горячей клавишей.
            object_name: Стабильное имя для тестов.

        Returns:
            QAction: созданное действие.

        Raises:
            Ничего не бросает.
        """
        action = QAction(text, self)
        action.setToolTip(tooltip)
        action.setObjectName(object_name)
        return action

    # -- форматирование ------------------------------------------------------
    def _toggle_bold(self, enabled: bool) -> None:
        """Включить/выключить жирный для выделения или будущего ввода.

        Args:
            enabled: Состояние кнопки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.editor.setFontWeight(QFont.Weight.Bold if enabled else QFont.Weight.Normal)

    def _toggle_italic(self, enabled: bool) -> None:
        """Включить/выключить курсив.

        Args:
            enabled: Состояние кнопки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.editor.setFontItalic(enabled)

    def _toggle_underline(self, enabled: bool) -> None:
        """Включить/выключить подчёркивание.

        Args:
            enabled: Состояние кнопки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.editor.setFontUnderline(enabled)

    def _apply_alignment(self) -> None:
        """Применить выравнивание выбранной кнопки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        action = self.sender()
        if isinstance(action, QAction):
            self.editor.setAlignment(action.data())

    def insert_list(self, style: QTextListFormat.Style) -> None:
        """Превратить текущий абзац в список.

        Args:
            style: ``ListDisc`` (маркированный) или ``ListDecimal`` (нумерованный).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.createList(style)
        cursor.endEditBlock()

    def insert_table(self, rows: int, columns: int) -> None:
        """Вставить таблицу в позицию курсора.

        Args:
            rows: Число строк.
            columns: Число столбцов.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.insertTable(rows, columns)
        cursor.endEditBlock()

    def insert_table_dialog(self) -> None:
        """Спросить размер таблицы и вставить её.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        rows, accepted = QInputDialog.getInt(self, "Вставка таблицы", "Строк:", 3, 1, 50)
        if not accepted:
            return
        columns, accepted = QInputDialog.getInt(
            self, "Вставка таблицы", "Столбцов:", 3, 1, 20
        )
        if not accepted:
            return
        self.insert_table(rows, columns)

    def _sync_format_actions(self) -> None:
        """Показать на панели формат под курсором (жирный, курсив, выравнивание).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        weight = self.editor.fontWeight()
        self._set_action_checked(self.action_bold, weight >= QFont.Weight.Bold)
        self._set_action_checked(self.action_italic, self.editor.fontItalic())
        self._set_action_checked(self.action_underline, self.editor.fontUnderline())

        alignment = self.editor.alignment()
        for action in (
            self.action_align_left,
            self.action_align_center,
            self.action_align_right,
        ):
            self._set_action_checked(action, bool(alignment & action.data()))

    @staticmethod
    def _set_action_checked(action: QAction, checked: bool) -> None:
        """Обновить состояние кнопки без повторного применения формата.

        Args:
            action: Кнопка панели.
            checked: Новое состояние.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        action.blockSignals(True)
        action.setChecked(checked)
        action.blockSignals(False)

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
    def open_settings(self) -> None:
        """Открыть диалог настроек DeepSeek.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        dialog = SettingsDialog(self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            state = load_settings()
            mode = "ИИ + словарь" if state["use_llm"] else "только словарь"
            self.statusBar().showMessage(f"Настройки сохранены ({mode})", 4000)

    def russify_document(self) -> None:
        """Запустить русификацию в фоне и показать предпросмотр замен.

        Тяжёлая работа идёт в :class:`RusifyWorker` (QThread), поэтому окно не
        замирает: пока идёт анализ, редактор заблокирован и виден прогресс.
        Найденные замены не применяются автоматически — пользователь снимает
        галочки в диалоге предпросмотра.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        text = self.editor.toPlainText()
        if not text.strip():
            QMessageBox.information(self, "Русификация", "Документ пуст.")
            return
        if getattr(self, "_worker", None) is not None and self._worker.isRunning():
            return

        settings = load_settings()
        self._rusify_cancelled = False
        self._progress = QProgressDialog("Идёт анализ документа…", "Отмена", 0, 100, self)
        self._progress.setWindowTitle("Русификация")
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)
        self._progress.canceled.connect(self._cancel_rusify)

        self.editor.setReadOnly(True)

        self._worker = RusifyWorker(
            text,
            parent=self,
            use_llm=settings["use_llm"],
            api_key=settings["api_key"],
        )
        self._worker.progress.connect(self._progress.setValue)
        self._worker.result_ready.connect(self._on_rusify_ready)
        self._worker.failed.connect(self._on_rusify_failed)
        self._worker.finished.connect(self._on_rusify_finished)
        self._worker.start()

    def _cancel_rusify(self) -> None:
        """Отменить показ результата (поток доработает и будет проигнорирован).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._rusify_cancelled = True
        self.editor.setReadOnly(False)
        self.statusBar().showMessage("Русификация отменена", 3000)

    def _on_rusify_ready(self, new_text: str, changes: object) -> None:
        """Показать диалог предпросмотра и применить выбранные замены.

        Args:
            new_text: Текст после замен (в диалоге не используется — замены
                применяются к редактору по парам, чтобы сохранить форматирование).
            changes: Список записей движка.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._close_progress()
        self.editor.setReadOnly(False)
        if getattr(self, "_rusify_cancelled", False):
            return

        items = [item for item in (changes or []) if isinstance(item, dict)]
        if not items:
            QMessageBox.information(
                self, "Русификация", "Словарных замен в документе не найдено."
            )
            return

        dialog = ReplacementsDialog(items, self)
        if dialog.exec() == ReplacementsDialog.DialogCode.Accepted:
            applied = self.apply_replacements(dialog.selected_replacements())
            self.statusBar().showMessage(f"Применено замен: {applied}", 5000)
        else:
            self.statusBar().showMessage("Русификация отменена", 3000)

    def _on_rusify_failed(self, message: str) -> None:
        """Показать ошибку движка.

        Args:
            message: Текст ошибки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._close_progress()
        self.editor.setReadOnly(False)
        QMessageBox.critical(self, "Ошибка русификации", message)

    def _on_rusify_finished(self) -> None:
        """Разблокировать редактор после завершения потока.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.editor.setReadOnly(False)

    def _close_progress(self) -> None:
        """Закрыть и уничтожить прогресс-диалог, если он открыт.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        progress = getattr(self, "_progress", None)
        if progress is not None:
            progress.close()
            progress.deleteLater()
            self._progress = None

    def apply_replacements(self, pairs: Sequence[tuple[str, str]]) -> int:
        """Заменить фрагменты в редакторе, сохраняя форматирование.

        Поиск идёт по документу (``QTextDocument.find``), а вставка — через
        ``QTextCursor``: новый текст наследует формат найденного фрагмента,
        поэтому жирный/курсив не теряются.

        Args:
            pairs: Пары ``(оригинал, замена)`` в порядке применения.

        Returns:
            int: Сколько замен реально применено.

        Raises:
            Ничего не бросает.
        """
        document = self.editor.document()
        applied = 0
        for original, replacement in pairs:
            if not original:
                continue
            found = document.find(original)
            if found.isNull():
                self.statusBar().showMessage(
                    f"Не найдено в тексте: {original}", 3000
                )
                continue
            found.beginEditBlock()
            found.insertText(replacement)
            found.endEditBlock()
            applied += 1
        return applied

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


