"""Главное окно TermRu: меню, вкладки, панели инструментов, статус-бар.

Центральный виджет — ``QTabWidget``: вкладка Word (DOCX) и вкладка
PowerPoint (PPTX). Меню и панели подстраиваются под активную вкладку.
"""


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
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QTabWidget,
    QTextEdit,
    QToolBar,
    QWidget,
)

from .docx_editor import DocxEditor
from .pptx_editor import TAB_TITLE as PPTX_TAB_TITLE
from .pptx_editor import PptxEditor
from .rusify import ReplacementsDialog, RusifyWorker
from .settings import SettingsDialog, load_settings

APP_TITLE = "TermRu — Редактор документов"
DOCX_FILTER = "Документы Word (*.docx)"
PPTX_FILTER = "Презентации PowerPoint (*.pptx)"
ANY_FILTER = "Документы TermRu (*.docx *.pptx)"
NEW_DOCUMENT = "Новый документ"
NEW_PRESENTATION = "Новая презентация"
WORD_TAB_TITLE = "Word (DOCX)"
WORD_TAB_INDEX = 0
PPTX_TAB_INDEX = 1

#: Подписи действия «Русифицировать» для вкладок DOCX и PPTX.
RUSSIFY_WORD_LABEL = "Русифицировать документ"
RUSSIFY_PPTX_LABEL = "Русифицировать слайд"


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
        self.pptx_editor = PptxEditor(self)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("documentTabs")
        self.tabs.addTab(self.editor, WORD_TAB_TITLE)
        self.tabs.addTab(self.pptx_editor, PPTX_TAB_TITLE)
        self.setCentralWidget(self.tabs)

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
        self.pptx_editor.file_loaded.connect(self._on_file_changed)
        self.pptx_editor.file_saved.connect(self._on_file_changed)
        self.pptx_editor.modified_changed.connect(self._on_pptx_modified)
        self.pptx_editor.slide_changed.connect(self._on_slide_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_title()
        self._refresh_status()

    def _on_pptx_modified(self, _modified: bool) -> None:
        """Обновить заголовок окна после правки презентации.

        Args:
            _modified: Новое значение флага (берётся из редактора).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._refresh_title()

    def _on_slide_changed(self, _index: int) -> None:
        """Обновить счётчики при выборе другого слайда.

        Args:
            _index: Индекс слайда (не используется).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
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
        undo.triggered.connect(lambda: self._dispatch_edit("undo"))
        redo = edit_menu.addAction("Вернуть")
        redo.setShortcut(QKeySequence.StandardKey.Redo)
        redo.triggered.connect(lambda: self._dispatch_edit("redo"))
        edit_menu.addSeparator()
        cut = edit_menu.addAction("Вырезать")
        cut.setShortcut(QKeySequence.StandardKey.Cut)
        cut.triggered.connect(lambda: self._dispatch_edit("cut"))
        copy = edit_menu.addAction("Копировать")
        copy.setShortcut(QKeySequence.StandardKey.Copy)
        copy.triggered.connect(lambda: self._dispatch_edit("copy"))
        paste = edit_menu.addAction("Вставить")
        paste.setShortcut(QKeySequence.StandardKey.Paste)
        paste.triggered.connect(lambda: self._dispatch_edit("paste"))

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
        self._sync_tab_ui()

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

    # -- вкладки -------------------------------------------------------------
    def _is_word_tab(self) -> bool:
        """Активна ли вкладка Word.

        Returns:
            bool: ``True`` для DOCX, ``False`` для PPTX.

        Raises:
            Ничего не бросает.
        """
        return self.tabs.currentIndex() == WORD_TAB_INDEX

    def _sync_tab_ui(self) -> None:
        """Подстроить панель форматирования и меню под активную вкладку.

        Панель форматирования работает только с DOCX (PPTX здесь — текстовый
        редактор блоков), поэтому на вкладке презентации она скрывается.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        is_word = self._is_word_tab()
        self.format_toolbar.setVisible(is_word)
        self.action_russify.setText(
            RUSSIFY_WORD_LABEL if is_word else RUSSIFY_PPTX_LABEL
        )

    def _on_tab_changed(self, _index: int) -> None:
        """Обновить интерфейс при переключении вкладки.

        Args:
            _index: Индекс новой вкладки (не используется).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._sync_tab_ui()
        self._refresh_title()
        self._refresh_status()

    def _edit_target(self) -> QWidget | None:
        """Виджет, к которому применяются команды меню «Правка».

        Returns:
            QWidget | None: поле с фокусом, редактор DOCX или первый блок
            презентации; ``None`` — править нечего.

        Raises:
            Ничего не бросает.
        """
        focused = QApplication.focusWidget()
        if isinstance(focused, QTextEdit):
            return focused
        if self._is_word_tab():
            return self.editor
        editors = self.pptx_editor.block_editors()
        return editors[0] if editors else None

    def _dispatch_edit(self, name: str) -> None:
        """Выполнить команду правки в активном поле ввода.

        Args:
            name: Имя метода Qt (``undo``, ``redo``, ``cut``, ``copy``, ``paste``).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        target = self._edit_target()
        method = getattr(target, name, None) if target is not None else None
        if callable(method):
            method()

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
        if self._is_word_tab():
            path = self.editor.current_path
            name = path.name if path is not None else NEW_DOCUMENT
            modified = self.editor.is_modified()
        else:
            path = self.pptx_editor.current_path
            name = path.name if path is not None else NEW_PRESENTATION
            modified = self.pptx_editor.is_modified()
        self.setWindowTitle(f"{APP_TITLE} — {name}[*]")
        self.setWindowModified(modified)

    def _refresh_status(self) -> None:
        """Обновить путь и счётчики в статус-баре под активную вкладку.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._is_word_tab():
            path = self.editor.current_path
            self.status_path.setText(str(path) if path is not None else NEW_DOCUMENT)
            text = self.editor.toPlainText()
            words = len(text.split())
            self.status_counts.setText(f"Слов: {words} · Символов: {len(text)}")
            return

        path = self.pptx_editor.current_path
        self.status_path.setText(
            str(path) if path is not None else NEW_PRESENTATION
        )
        self.status_counts.setText(
            f"Слайдов: {self.pptx_editor.slide_count()} · "
            f"Блоков: {self.pptx_editor.block_count()}"
        )

    # -- файлы ---------------------------------------------------------------
    def open_document(self) -> None:
        """Открыть файл через диалог; фильтр зависит от активной вкладки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        preferred = DOCX_FILTER if self._is_word_tab() else PPTX_FILTER
        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Открыть документ",
            "",
            f"{preferred};;{ANY_FILTER}",
        )
        if not filepath:
            return
        self.open_path(filepath)

    def open_path(self, filepath: str) -> bool:
        """Открыть файл по расширению и переключить вкладку.

        Args:
            filepath: Путь к ``.docx`` или ``.pptx``.

        Returns:
            bool: ``True``, если файл открыт.

        Raises:
            Ничего не бросает: ошибки показываются диалогом редактора.
        """
        is_pptx = Path(filepath).suffix.lower() == ".pptx"
        self.tabs.setCurrentIndex(PPTX_TAB_INDEX if is_pptx else WORD_TAB_INDEX)
        target = self.pptx_editor if is_pptx else self.editor
        if not target.load_file(filepath):
            self.statusBar().showMessage("Не удалось открыть файл", 5000)
            return False
        self._refresh_title()
        self._refresh_status()
        self.statusBar().showMessage(f"Открыто: {Path(filepath).name}", 3000)
        return True

    def save_document(self) -> bool:
        """Сохранить активный документ; если пути нет — «Сохранить как».

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        return self._save_word() if self._is_word_tab() else self._save_pptx()

    def _save_word(self) -> bool:
        """Сохранить DOCX по известному пути.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        if self.editor.current_path is None:
            return self._save_as("docx")
        if not self.editor.save_file(str(self.editor.current_path)):
            self.statusBar().showMessage("Не удалось сохранить документ", 5000)
            return False
        self._refresh_title()
        self.statusBar().showMessage("Документ сохранён", 3000)
        return True

    def _save_pptx(self) -> bool:
        """Сохранить PPTX по известному пути.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        if self.pptx_editor.current_path is None:
            return self._save_as("pptx")
        if not self.pptx_editor.save_file(str(self.pptx_editor.current_path)):
            self.statusBar().showMessage("Не удалось сохранить презентацию", 5000)
            return False
        self._refresh_title()
        self.statusBar().showMessage("Презентация сохранена", 3000)
        return True

    def save_document_as(self) -> bool:
        """Сохранить активный документ под новым именем.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        return self._save_as("docx" if self._is_word_tab() else "pptx")

    def _save_as(self, kind: str) -> bool:
        """Диалог «Сохранить как» с расширением по типу документа.

        Args:
            kind: ``"docx"`` или ``"pptx"``.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        is_word = kind == "docx"
        file_filter = DOCX_FILTER if is_word else PPTX_FILTER
        suffix = ".docx" if is_word else ".pptx"
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", "", file_filter
        )
        if not filepath:
            return False
        if not filepath.lower().endswith(suffix):
            filepath += suffix

        target = self.editor if is_word else self.pptx_editor
        if not target.save_file(filepath):
            self.statusBar().showMessage("Не удалось сохранить файл", 5000)
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
        is_word = self._is_word_tab()
        text = self.editor.toPlainText() if is_word else self.pptx_editor.current_text()
        if not text.strip():
            message = "Документ пуст." if is_word else "На слайде нет текста."
            QMessageBox.information(self, "Русификация", message)
            return
        if getattr(self, "_worker", None) is not None and self._worker.isRunning():
            return

        settings = load_settings()
        self._rusify_cancelled = False
        self._rusify_target = "word" if is_word else "pptx"
        self._progress = QProgressDialog("Идёт анализ документа…", "Отмена", 0, 100, self)
        self._progress.setWindowTitle("Русификация")
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setMinimumDuration(0)
        self._progress.setValue(0)
        self._progress.canceled.connect(self._cancel_rusify)

        self._set_surface_read_only(True)

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
        self._set_surface_read_only(False)
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
        self._set_surface_read_only(False)
        if getattr(self, "_rusify_cancelled", False):
            return

        items = [item for item in (changes or []) if isinstance(item, dict)]
        if not items:
            target = "документе" if self._is_word_tab() else "слайде"
            QMessageBox.information(
                self, "Русификация", f"Словарных замен в {target} не найдено."
            )
            return

        dialog = ReplacementsDialog(items, self)
        if dialog.exec() == ReplacementsDialog.DialogCode.Accepted:
            pairs = dialog.selected_replacements()
            if getattr(self, "_rusify_target", "word") == "pptx":
                applied = self.pptx_editor.apply_replacements(pairs)
            else:
                applied = self.apply_replacements(pairs)
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
        self._set_surface_read_only(False)
        QMessageBox.critical(self, "Ошибка русификации", message)

    def _on_rusify_finished(self) -> None:
        """Разблокировать редактор после завершения потока.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._set_surface_read_only(False)

    def _set_surface_read_only(self, read_only: bool) -> None:
        """Заблокировать тот редактор, для которого запущена русификация.

        Args:
            read_only: ``True`` — запретить правку на время анализа.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if getattr(self, "_rusify_target", "word") == "pptx":
            self.pptx_editor.set_read_only(read_only)
        else:
            self.editor.setReadOnly(read_only)

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
        if not (self.editor.is_modified() or self.pptx_editor.is_modified()):
            event.accept()
            return

        answer = QMessageBox.question(
            self,
            "Несохранённые изменения",
            "Сохранить изменения перед закрытием?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            event.ignore()
            return
        if answer == QMessageBox.StandardButton.Save:
            # Сохраняем каждую изменённую поверхность, переключая вкладку:
            # диалог «Сохранить как» берёт тип файла из активной вкладки.
            if self.editor.is_modified():
                self.tabs.setCurrentIndex(WORD_TAB_INDEX)
                if not self._save_word():
                    event.ignore()
                    return
            if self.pptx_editor.is_modified():
                self.tabs.setCurrentIndex(PPTX_TAB_INDEX)
                if not self._save_pptx():
                    event.ignore()
                    return
        event.accept()


