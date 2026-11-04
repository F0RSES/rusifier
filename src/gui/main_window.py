"""Главное окно TermRu: стартовый экран, редактор во вкладках, меню и статус-бар.

Центральный виджет — ``QStackedWidget``: 0 — приветственный экран
(:class:`~src.gui.start_screen.StartScreen`), 1 — редактор с вкладками Word,
PowerPoint и PDF. Меню и панели подстраиваются под активную вкладку.
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
    QTextBlockFormat,
    QTextCursor,
    QTextListFormat,
)
from PySide6.QtWidgets import (
    QApplication,
    QColorDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..glossary_manager import GlossaryError, GlossaryManager
from .docx_editor import DocxEditor
from .glossary_editor import GlossaryEditorDialog
from .pdf_editor import TAB_TITLE as PDF_TAB_TITLE
from .pdf_editor import PdfEditor
from .pptx_editor import TAB_TITLE as PPTX_TAB_TITLE
from .pptx_editor import PptxEditor
from .ribbon import HOME_TAB, INSERT_TAB, TERMRU_TAB, RibbonWidget
from .rusify import (
    DEFAULT_EXCEPTIONS,
    DEFAULT_GLOSSARY,
    ReplacementsDialog,
    RusifyWorker,
)
from .settings import (
    SettingsDialog,
    apply_theme,
    load_recent_files,
    load_settings,
    remember_recent_file,
    save_settings,
)
from .start_screen import StartScreen, qt_icon

APP_TITLE = "TermRu — Редактор документов"
DOCX_FILTER = "Документы Word (*.docx)"
PPTX_FILTER = "Презентации PowerPoint (*.pptx)"
PDF_FILTER = "Документы PDF (*.pdf)"
ANY_FILTER = "Документы TermRu (*.docx *.pptx *.pdf)"
NEW_DOCUMENT = "Новый документ"
NEW_PRESENTATION = "Новая презентация"
NEW_PDF = "PDF не открыт"
WORD_TAB_TITLE = "Word (DOCX)"
WORD_TAB_INDEX = 0
PPTX_TAB_INDEX = 1
PDF_TAB_INDEX = 2

#: Страницы QStackedWidget: приветственный экран и редактор.
START_INDEX = 0
EDITOR_INDEX = 1

#: Подписи действия «Русифицировать» для каждой вкладки.
RUSSIFY_WORD_LABEL = "Русифицировать документ"
RUSSIFY_PPTX_LABEL = "Русифицировать слайд"
RUSSIFY_PDF_LABEL = "Русифицировать страницу"

#: Тип вкладки → подпись действия русификации.
RUSSIFY_LABELS = {
    "word": RUSSIFY_WORD_LABEL,
    "pptx": RUSSIFY_PPTX_LABEL,
    "pdf": RUSSIFY_PDF_LABEL,
}


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
        state = load_settings()
        self.pdf_editor = PdfEditor(self, font_mode=state["pdf_fonts"])
        #: Проектный словарь; ``None`` — файлы недоступны (см. ``_glossary_error``).
        self.glossary: GlossaryManager | None = None
        self._glossary_error = ""
        try:
            self.glossary = GlossaryManager(
                str(DEFAULT_GLOSSARY), str(DEFAULT_EXCEPTIONS)
            )
        except (GlossaryError, OSError) as exc:  # noqa: BLE001 - окно без словаря работает
            self._glossary_error = str(exc)

        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("documentTabs")
        self.tabs.addTab(self.editor, WORD_TAB_TITLE)
        self.tabs.addTab(self.pptx_editor, PPTX_TAB_TITLE)
        self.tabs.addTab(self.pdf_editor, PDF_TAB_TITLE)

        # Экран редактора: сверху лента (Ribbon), под ней вкладки документов.
        self.ribbon = RibbonWidget(self)
        self.editor_screen = QWidget(self)
        editor_layout = QVBoxLayout(self.editor_screen)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self.ribbon)
        editor_layout.addWidget(self.tabs, 1)

        self.start_screen = StartScreen(self)
        self.start_screen.create_requested.connect(self.new_document)
        self.start_screen.open_requested.connect(self.open_document)
        self.start_screen.recent_requested.connect(self.open_path)
        self.start_screen.settings_requested.connect(self.open_settings)
        self.start_screen.about_requested.connect(self.show_about)
        self.start_screen.file_dropped.connect(self.open_path)
        self.start_screen.set_recent_files(load_recent_files())

        # Стек экранов: 0 — приветственный, 1 — редактор с лентой и вкладками.
        self.stack = QStackedWidget(self)
        self.stack.setObjectName("mainStack")
        self.stack.addWidget(self.start_screen)  # START_INDEX
        self.stack.addWidget(self.editor_screen)  # EDITOR_INDEX
        self.stack.setCurrentIndex(START_INDEX)
        self.setCentralWidget(self.stack)

        #: Действия, доступные только для DOCX (PPTX/PDF их не поддерживают).
        self._docx_actions: list[QAction] = []
        #: База масштаба Word-редактора в пунктах (для пересчёта процентов).
        self._zoom_base = max(1, self.editor.font().pointSize() or 11)
        self._zoom_steps = 0

        self._build_actions()
        self._build_menus()
        self._build_ribbon()
        self._build_statusbar()

        self.editor.textChanged.connect(self._refresh_status)
        self.editor.document().modificationChanged.connect(self._refresh_title)
        # Сигналы редактора обновляют шапку и статус при любом пути загрузки:
        # и через диалог, и программно (например, из тестов или будущего CLI-GUI).
        self.editor.file_loaded.connect(self._on_file_changed)
        self.editor.file_saved.connect(self._on_file_changed)
        self.pptx_editor.file_loaded.connect(self._on_file_changed)
        self.pptx_editor.file_saved.connect(self._on_file_changed)
        self.pptx_editor.modified_changed.connect(self._on_editor_modified)
        self.pptx_editor.slide_changed.connect(self._on_selection_changed)
        self.pdf_editor.file_loaded.connect(self._on_file_changed)
        self.pdf_editor.file_saved.connect(self._on_file_changed)
        self.pdf_editor.modified_changed.connect(self._on_editor_modified)
        self.pdf_editor.page_changed.connect(self._on_selection_changed)
        self.pdf_editor.status_message.connect(self._show_status_message)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_title()
        self._refresh_status()

    def _on_editor_modified(self, _modified: bool) -> None:
        """Обновить заголовок окна после правки презентации или PDF.

        Args:
            _modified: Новое значение флага (берётся из редактора).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._refresh_title()

    def _on_selection_changed(self, _index: int) -> None:
        """Обновить счётчики при выборе другого слайда или страницы.

        Args:
            _index: Индекс слайда/страницы (не используется).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._refresh_status()

    def _show_status_message(self, message: str) -> None:
        """Показать сообщение редактора в статус-баре.

        Args:
            message: Текст сообщения.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if message:
            self.statusBar().showMessage(message, 8000)

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
        self.action_open = QAction(qt_icon("fa5s.folder-open", "#9aa0a6"), "Открыть", self)
        self.action_open.setObjectName("actionOpen")
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_open.triggered.connect(self.open_document)

        self.action_save = QAction(qt_icon("fa5s.save", "#9aa0a6"), "Сохранить", self)
        self.action_save.setObjectName("actionSave")
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_save.triggered.connect(self.save_document)

        self.action_save_as = QAction(
            qt_icon("fa5s.file-export", "#9aa0a6"), "Сохранить как…", self
        )
        self.action_save_as.setObjectName("actionSaveAs")
        self.action_save_as.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.action_save_as.triggered.connect(self.save_document_as)

        self.action_quit = QAction(qt_icon("fa5s.sign-out-alt", "#9aa0a6"), "Выход", self)
        self.action_quit.setObjectName("actionQuit")
        self.action_quit.setShortcut(QKeySequence.StandardKey.Quit)
        self.action_quit.triggered.connect(self.close)

        self.action_home = QAction(
            qt_icon("fa5s.home", "#9aa0a6"), "На главный экран", self
        )
        self.action_home.setObjectName("actionHome")
        self.action_home.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.action_home.triggered.connect(self.show_start_screen)

        self.action_russify = QAction(
            qt_icon("fa5s.magic", "#9aa0a6"), "Русифицировать документ", self
        )
        self.action_russify.setObjectName("action_russify")
        self.action_russify.triggered.connect(self.russify_document)

        self.action_settings = QAction(
            qt_icon("fa5s.cog", "#9aa0a6"), "Настройки…", self
        )
        self.action_settings.setObjectName("actionSettings")
        self.action_settings.triggered.connect(self.open_settings)

        self.action_glossary = QAction(
            qt_icon("fa5s.book", "#9aa0a6"), "Редактор словаря…", self
        )
        self.action_glossary.setObjectName("actionGlossary")
        self.action_glossary.triggered.connect(self.open_glossary_editor)

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
        file_menu.addAction(self.action_home)
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
        tools_menu.addAction(self.action_glossary)
        tools_menu.addAction(self.action_settings)
        tools_menu.addSeparator()
        tools_menu.addAction(self.action_russify)

        help_menu = menubar.addMenu("С&правка")
        help_menu.addAction(self.action_about)

    def _build_ribbon(self) -> None:
        """Собрать ленту: быстрый доступ, вкладки и группы действий.

        Действия создаются здесь же (у окна), а лента их только раскладывает —
        поэтому один и тот же ``QAction`` работает и в меню, и на ленте.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        for action in (
            self.action_open,
            self.action_save,
            self.action_save_as,
            self.action_home,
        ):
            self.ribbon.add_quick_action(action)

        # -- Главная: шрифт --------------------------------------------------
        self.action_bold = self._ribbon_action(
            "action_bold", "Жирный", "fa5s.bold", "Жирный (Ctrl+B)", checkable=True
        )
        self.action_bold.setShortcut(QKeySequence.StandardKey.Bold)
        self.action_bold.toggled.connect(self._toggle_bold)

        self.action_italic = self._ribbon_action(
            "action_italic", "Курсив", "fa5s.italic", "Курсив (Ctrl+I)", checkable=True
        )
        self.action_italic.setShortcut(QKeySequence.StandardKey.Italic)
        self.action_italic.toggled.connect(self._toggle_italic)

        self.action_underline = self._ribbon_action(
            "action_underline",
            "Подчёркнутый",
            "fa5s.underline",
            "Подчёркнутый (Ctrl+U)",
            checkable=True,
        )
        self.action_underline.setShortcut(QKeySequence.StandardKey.Underline)
        self.action_underline.toggled.connect(self._toggle_underline)

        for action in (self.action_bold, self.action_italic, self.action_underline):
            self.ribbon.add_action(HOME_TAB, "Шрифт", action)

        self._build_paragraph_actions()
        self._build_editing_actions()
        self._build_insert_tab()
        self._build_termru_tab()

        self._docx_actions = [
            self.action_bold,
            self.action_italic,
            self.action_underline,
            self.action_align_left,
            self.action_align_center,
            self.action_align_right,
            self.action_bullet_list,
            self.action_numbered_list,
            self.action_table,
            self.action_image,
            self.action_text_box,
            self.action_find,
        ]

        self._connect_ribbon()
        self.editor.cursorPositionChanged.connect(self._sync_format_actions)
        self.editor.selectionChanged.connect(self._sync_format_actions)
        self._sync_format_actions()
        self._sync_tab_ui()

    def _build_paragraph_actions(self) -> None:
        """Создать действия абзаца и положить их в группу «Абзац».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.align_group = QActionGroup(self)
        self.align_group.setExclusive(True)
        self.action_align_left = self._ribbon_action(
            "action_align_left",
            "По левому краю",
            "fa5s.align-left",
            "По левому краю",
            checkable=True,
        )
        self.action_align_center = self._ribbon_action(
            "action_align_center",
            "По центру",
            "fa5s.align-center",
            "По центру",
            checkable=True,
        )
        self.action_align_right = self._ribbon_action(
            "action_align_right",
            "По правому краю",
            "fa5s.align-right",
            "По правому краю",
            checkable=True,
        )
        for action, alignment in (
            (self.action_align_left, Qt.AlignmentFlag.AlignLeft),
            (self.action_align_center, Qt.AlignmentFlag.AlignHCenter),
            (self.action_align_right, Qt.AlignmentFlag.AlignRight),
        ):
            action.setData(alignment)
            self.align_group.addAction(action)
            action.triggered.connect(self._apply_alignment)
            self.ribbon.add_action(HOME_TAB, "Абзац", action)
        self.action_align_left.setChecked(True)

        self.action_bullet_list = self._ribbon_action(
            "action_bullet_list",
            "Маркированный список",
            "fa5s.list-ul",
            "Маркированный список",
        )
        self.action_bullet_list.triggered.connect(
            lambda: self.insert_list(QTextListFormat.Style.ListDisc)
        )
        self.action_numbered_list = self._ribbon_action(
            "action_numbered_list",
            "Нумерованный список",
            "fa5s.list-ol",
            "Нумерованный список",
        )
        self.action_numbered_list.triggered.connect(
            lambda: self.insert_list(QTextListFormat.Style.ListDecimal)
        )
        for action in (self.action_bullet_list, self.action_numbered_list):
            self.ribbon.add_action(HOME_TAB, "Абзац", action)

    def _build_editing_actions(self) -> None:
        """Создать действия группы «Редактирование».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.action_undo = self._ribbon_action(
            "action_undo", "Отменить", "fa5s.undo", "Отменить (Ctrl+Z)"
        )
        self.action_undo.triggered.connect(lambda: self._dispatch_edit("undo"))
        self.action_redo = self._ribbon_action(
            "action_redo", "Вернуть", "fa5s.redo", "Вернуть (Ctrl+Y)"
        )
        self.action_redo.triggered.connect(lambda: self._dispatch_edit("redo"))
        self.action_find = self._ribbon_action(
            "action_find", "Найти", "fa5s.search", "Найти (Ctrl+F)"
        )
        self.action_find.setShortcut(QKeySequence.StandardKey.Find)
        self.action_find.triggered.connect(self.find_text)
        for action in (self.action_undo, self.action_redo, self.action_find):
            self.ribbon.add_action(HOME_TAB, "Редактирование", action)

    def _build_insert_tab(self) -> None:
        """Наполнить вкладку «Вставка»: таблица, картинка, текстовое поле.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.action_table = self._ribbon_action(
            "action_table", "Таблица", "fa5s.table", "Вставить таблицу"
        )
        self.action_table.triggered.connect(self.insert_table_dialog)
        self.ribbon.add_action(INSERT_TAB, "Таблицы", self.action_table, big=True)

        self.action_image = self._ribbon_action(
            "action_image", "Картинка", "fa5s.image", "Вставить картинку"
        )
        self.action_image.triggered.connect(self.insert_image_stub)
        self.ribbon.add_action(INSERT_TAB, "Иллюстрации", self.action_image, big=True)

        self.action_text_box = self._ribbon_action(
            "action_text_box", "Текстовое поле", "fa5s.font", "Вставить текстовое поле"
        )
        self.action_text_box.triggered.connect(self.insert_text_box_stub)
        self.ribbon.add_action(INSERT_TAB, "Текст", self.action_text_box, big=True)

    def _build_termru_tab(self) -> None:
        """Наполнить вкладку TermRu: русификация и словарь.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.ribbon.add_action(TERMRU_TAB, "Русификация", self.action_russify, big=True)
        self.ribbon.add_action(TERMRU_TAB, "Словарь", self.action_glossary, big=True)

    def _ribbon_action(
        self,
        object_name: str,
        text: str,
        icon_name: str,
        tooltip: str,
        *,
        checkable: bool = False,
    ) -> QAction:
        """Создать действие ленты с векторной иконкой qtawesome.

        Args:
            object_name: Стабильное имя для тестов и связей.
            text: Подпись кнопки.
            icon_name: Имя иконки qtawesome (``fa5s.bold``).
            tooltip: Подсказка с горячей клавишей.
            checkable: Нужен ли переключаемый вид.

        Returns:
            QAction: созданное действие.

        Raises:
            Ничего не бросает.
        """
        action = QAction(qt_icon(icon_name, "#9aa0a6"), text, self)
        action.setObjectName(object_name)
        action.setToolTip(tooltip)
        action.setCheckable(checkable)
        return action

    def _connect_ribbon(self) -> None:
        """Связать сигналы ленты с обработчиками окна и прочитать настройки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        state = load_settings()
        self.ribbon.set_theme(state["theme"])
        self.ribbon.set_use_llm(state["use_llm"])
        self.ribbon.set_zoom(100)

        self.ribbon.theme_changed.connect(self._on_ribbon_theme)
        self.ribbon.zoom_changed.connect(self.set_zoom)
        self.ribbon.use_llm_changed.connect(self._on_ribbon_use_llm)
        self.ribbon.font_combo.currentTextChanged.connect(self.set_font_family)
        self.ribbon.size_combo.currentTextChanged.connect(self.set_font_size)
        self.ribbon.color_button.clicked.connect(self.choose_text_color)
        self.ribbon.spacing_combo.currentIndexChanged.connect(self._on_spacing_changed)

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

    # -- лента: шрифт, абзац, вставка ----------------------------------------
    def set_font_family(self, family: str) -> None:
        """Применить семейство шрифта из ленты к выделению.

        Args:
            family: Имя семейства (``DejaVu Sans``).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        name = str(family).strip()
        if not name:
            return
        self.editor.setFontFamily(name)

    def set_font_size(self, size_text: str) -> None:
        """Применить кегль из списка ленты к выделению.

        Args:
            size_text: Размер в пунктах как текст (``"14"`` или ``"12,5"``).

        Returns:
            None

        Raises:
            Ничего не бросает: нечисловое значение игнорируется.
        """
        try:
            size = float(str(size_text).replace(",", ".").strip())
        except ValueError:
            return
        if size <= 0:
            return
        self.editor.setFontPointSize(size)

    def choose_text_color(self) -> None:
        """Спросить цвет текста и применить его к выделению.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        color = QColorDialog.getColor(self.editor.textColor(), self, "Цвет текста")
        if color.isValid():
            self.editor.setTextColor(color)
            self.statusBar().showMessage(f"Цвет текста: {color.name()}", 3000)

    def _on_spacing_changed(self, index: int) -> None:
        """Применить межстрочный интервал, выбранный в ленте.

        Args:
            index: Индекс в списке интервалов (``None`` — пусто).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        value = self.ribbon.spacing_combo.itemData(index)
        if value is None:
            return
        self.set_line_spacing(float(value))

    def set_line_spacing(self, factor: float) -> None:
        """Задать межстрочный интервал текущего абзаца.

        Args:
            factor: Множитель высоты строки (``1.0``, ``1.15``, ``1.5``, ``2.0``).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        cursor = self.editor.textCursor()
        block_format = cursor.blockFormat()
        block_format.setLineHeight(
            factor * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value
        )
        cursor.setBlockFormat(block_format)
        self.statusBar().showMessage(f"Интервал: {factor:g}", 3000)

    def find_text(self) -> None:
        """Найти текст в документе и выделить первое вхождение.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        needle, accepted = QInputDialog.getText(self, "Найти", "Что искать:")
        if not accepted or not needle.strip():
            return
        found = self.editor.document().find(needle)
        if found.isNull():
            self.statusBar().showMessage(f"Не найдено: {needle}", 3000)
            return
        self.editor.setTextCursor(found)
        self.statusBar().showMessage("Найдено", 2000)

    def insert_image_stub(self) -> None:
        """Заглушка «Вставить картинку».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        QMessageBox.information(
            self,
            "Вставить картинку",
            "Вставка изображений появится в следующем обновлении.",
        )

    def insert_text_box_stub(self) -> None:
        """Заглушка «Вставить текстовое поле».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        QMessageBox.information(
            self,
            "Текстовое поле",
            "Текстовые поля появятся в следующем обновлении.",
        )

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
    def _active_kind(self) -> str:
        """Тип активной вкладки.

        Returns:
            str: ``"word"``, ``"pptx"`` или ``"pdf"``.

        Raises:
            Ничего не бросает.
        """
        index = self.tabs.currentIndex()
        if index == PPTX_TAB_INDEX:
            return "pptx"
        if index == PDF_TAB_INDEX:
            return "pdf"
        return "word"

    def _is_word_tab(self) -> bool:
        """Активна ли вкладка Word.

        Returns:
            bool: ``True`` для DOCX.

        Raises:
            Ничего не бросает.
        """
        return self._active_kind() == "word"

    def _sync_tab_ui(self) -> None:
        """Подстроить панель форматирования и меню под активную вкладку.

        Панель форматирования работает только с DOCX (PPTX и PDF здесь —
        текстовые редакторы блоков), поэтому на других вкладках она скрыта.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        kind = self._active_kind()
        on_editor = self.stack.currentIndex() == EDITOR_INDEX
        format_enabled = on_editor and kind == "word"
        for action in self._docx_actions:
            action.setEnabled(format_enabled)
        for widget in (
            self.ribbon.font_combo,
            self.ribbon.size_combo,
            self.ribbon.color_button,
            self.ribbon.spacing_combo,
        ):
            widget.setEnabled(format_enabled)
        self.action_russify.setText(RUSSIFY_LABELS[kind])
        self.action_russify.setEnabled(on_editor)

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

    # -- экраны --------------------------------------------------------------
    def show_start_screen(self) -> None:
        """Показать приветственный экран с недавними файлами.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.start_screen.set_recent_files(load_recent_files())
        self.start_screen.refresh_style()
        self.stack.setCurrentIndex(START_INDEX)
        self._sync_tab_ui()
        self._refresh_title()
        self._refresh_status()

    def show_editor(self) -> None:
        """Переключить стек экранов на редактор документов.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.stack.setCurrentIndex(EDITOR_INDEX)
        self._sync_tab_ui()
        self._refresh_title()
        self._refresh_status()

    def new_document(self, kind: str = "docx") -> None:
        """Создать пустой документ и открыть его в редакторе.

        Args:
            kind: ``"docx"``, ``"pptx"`` или ``"pdf"`` (неизвестное — DOCX).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if kind == "pptx":
            self.tabs.setCurrentIndex(PPTX_TAB_INDEX)
            self.pptx_editor.new_document()
        elif kind == "pdf":
            self.tabs.setCurrentIndex(PDF_TAB_INDEX)
            self.pdf_editor.new_document()
        else:
            self.tabs.setCurrentIndex(WORD_TAB_INDEX)
            self.editor.clear()
            self.editor.current_path = None
            self.editor.mark_saved()
        self.show_editor()
        self.statusBar().showMessage("Создан новый документ", 3000)

    def _remember_recent(self, filepath: str) -> None:
        """Запомнить открытый файл и обновить список на стартовом экране.

        Args:
            filepath: Путь открытого файла.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.start_screen.set_recent_files(remember_recent_file(filepath))

    # -- лента: вид и режимы -------------------------------------------------
    def set_zoom(self, percent: int) -> None:
        """Изменить масштаб активного документа (Word — текст, PDF — рендер).

        Args:
            percent: Масштаб в процентах (50–200).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        value = max(50, min(200, int(percent)))
        kind = self._active_kind()
        if kind == "word":
            steps = round(self._zoom_base * (value / 100 - 1))
            delta = steps - self._zoom_steps
            if delta > 0:
                self.editor.zoomIn(delta)
            elif delta < 0:
                self.editor.zoomOut(-delta)
            self._zoom_steps = steps
        elif kind == "pdf":
            self.pdf_editor.set_zoom(value / 100)
        self.ribbon.set_zoom(value)
        self.statusBar().showMessage(f"Масштаб: {value} %", 2000)

    def _on_ribbon_theme(self, theme: str) -> None:
        """Сменить тему из ленты и сохранить её в настройках.

        Args:
            theme: ``dark`` или ``light``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        state = load_settings()
        save_settings(state["api_key"], state["use_llm"], theme, state["pdf_fonts"])
        apply_theme(theme)
        self.ribbon.refresh_style(theme)
        self.start_screen.refresh_style(theme)
        self.statusBar().showMessage(f"Тема: {theme}", 2000)

    def _on_ribbon_use_llm(self, enabled: bool) -> None:
        """Включить/выключить ИИ из ленты.

        Без ключа DeepSeek режим бессмысленен, поэтому флажок снимается обратно
        и пользователь получает подсказку, где взять ключ.

        Args:
            enabled: Новое состояние флажка.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        state = load_settings()
        if enabled and not state["api_key"]:
            self.ribbon.set_use_llm(False)
            QMessageBox.information(
                self,
                "ИИ (DeepSeek)",
                "Сначала добавьте ключ API: «Инструменты → Настройки…».",
            )
            return
        save_settings(state["api_key"], enabled, state["theme"], state["pdf_fonts"])
        self.statusBar().showMessage(
            "ИИ включён: словарь + DeepSeek" if enabled else "ИИ выключен: только словарь",
            3000,
        )

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
        kind = self._active_kind()
        if kind == "pptx":
            editors = self.pptx_editor.block_editors()
        elif kind == "pdf":
            editors = self.pdf_editor.block_editors()
        else:
            return self.editor
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
        kind = self._active_kind()
        if kind == "pptx":
            path = self.pptx_editor.current_path
            name = path.name if path is not None else NEW_PRESENTATION
            modified = self.pptx_editor.is_modified()
        elif kind == "pdf":
            path = self.pdf_editor.current_path
            name = path.name if path is not None else NEW_PDF
            modified = self.pdf_editor.is_modified()
        else:
            path = self.editor.current_path
            name = path.name if path is not None else NEW_DOCUMENT
            modified = self.editor.is_modified()
        self.setWindowTitle(f"{APP_TITLE} — {name}[*]")
        self.setWindowModified(modified)

    def _refresh_status(self) -> None:
        """Обновить путь и счётчики в статус-баре под активную вкладку.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        kind = self._active_kind()
        if kind == "pptx":
            path = self.pptx_editor.current_path
            self.status_path.setText(
                str(path) if path is not None else NEW_PRESENTATION
            )
            self.status_counts.setText(
                f"Слайдов: {self.pptx_editor.slide_count()} · "
                f"Блоков: {self.pptx_editor.block_count()}"
            )
            return
        if kind == "pdf":
            path = self.pdf_editor.current_path
            self.status_path.setText(str(path) if path is not None else NEW_PDF)
            self.status_counts.setText(
                f"Страниц: {self.pdf_editor.page_count()} · "
                f"Блоков: {self.pdf_editor.block_count()}"
            )
            return

        path = self.editor.current_path
        self.status_path.setText(str(path) if path is not None else NEW_DOCUMENT)
        text = self.editor.toPlainText()
        words = len(text.split())
        self.status_counts.setText(f"Слов: {words} · Символов: {len(text)}")

    # -- файлы ---------------------------------------------------------------
    def open_document(self) -> None:
        """Открыть файл через диалог; фильтр зависит от активной вкладки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        preferred = {
            "word": DOCX_FILTER,
            "pptx": PPTX_FILTER,
            "pdf": PDF_FILTER,
        }[self._active_kind()]
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
            filepath: Путь к ``.docx``, ``.pptx`` или ``.pdf``.

        Returns:
            bool: ``True``, если файл открыт.

        Raises:
            Ничего не бросает: ошибки показываются диалогом редактора.
        """
        suffix = Path(filepath).suffix.lower()
        if suffix == ".pdf":
            index, target = PDF_TAB_INDEX, self.pdf_editor
        elif suffix == ".pptx":
            index, target = PPTX_TAB_INDEX, self.pptx_editor
        else:
            index, target = WORD_TAB_INDEX, self.editor
        # Сначала показываем редактор, потом грузим файл: так пользователь видит,
        # куда именно откроется документ.
        self.show_editor()
        self.tabs.setCurrentIndex(index)
        if not target.load_file(filepath):
            self.statusBar().showMessage("Не удалось открыть файл", 5000)
            self.show_start_screen()
            return False
        self._refresh_title()
        self._refresh_status()
        self._remember_recent(filepath)
        self.statusBar().showMessage(f"Открыто: {Path(filepath).name}", 3000)
        return True

    def save_document(self) -> bool:
        """Сохранить активный документ; если пути нет — «Сохранить как».

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        kind = self._active_kind()
        if kind == "pptx":
            return self._save_pptx()
        if kind == "pdf":
            return self._save_pdf()
        return self._save_word()

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

    def _save_pdf(self) -> bool:
        """Сохранить PDF по известному пути.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        if self.pdf_editor.current_path is None:
            return self._save_as("pdf")
        if not self.pdf_editor.save_file(str(self.pdf_editor.current_path)):
            self.statusBar().showMessage("Не удалось сохранить PDF", 5000)
            return False
        self._refresh_title()
        self.statusBar().showMessage("PDF записан", 3000)
        return True

    def save_document_as(self) -> bool:
        """Сохранить активный документ под новым именем.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        return self._save_as(
            {"word": "docx", "pptx": "pptx", "pdf": "pdf"}[self._active_kind()]
        )

    def _save_as(self, kind: str) -> bool:
        """Диалог «Сохранить как» с расширением по типу документа.

        Args:
            kind: ``"docx"``, ``"pptx"`` или ``"pdf"``.

        Returns:
            bool: ``True``, если файл записан.

        Raises:
            Ничего не бросает.
        """
        file_filter, suffix, target = {
            "docx": (DOCX_FILTER, ".docx", self.editor),
            "pptx": (PPTX_FILTER, ".pptx", self.pptx_editor),
            "pdf": (PDF_FILTER, ".pdf", self.pdf_editor),
        }[kind]
        filepath, _ = QFileDialog.getSaveFileName(
            self, "Сохранить как", "", file_filter
        )
        if not filepath:
            return False
        if not filepath.lower().endswith(suffix):
            filepath += suffix

        if not target.save_file(filepath):
            self.statusBar().showMessage("Не удалось сохранить файл", 5000)
            return False
        self._refresh_title()
        self._refresh_status()
        self.statusBar().showMessage(f"Сохранено: {Path(filepath).name}", 3000)
        return True

    # -- инструменты ---------------------------------------------------------
    def open_settings(self) -> None:
        """Открыть диалог настроек: DeepSeek, тема, режим шрифтов PDF.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        dialog = SettingsDialog(self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return

        state = load_settings()
        apply_theme(state["theme"])
        self.pdf_editor.set_font_substitution_mode(state["pdf_fonts"])
        self.start_screen.refresh_style(state["theme"])
        self.ribbon.set_theme(state["theme"])
        self.ribbon.set_use_llm(state["use_llm"])
        mode = "ИИ + словарь" if state["use_llm"] else "только словарь"
        self.statusBar().showMessage(
            f"Настройки сохранены ({mode}, тема {state['theme']}, "
            f"PDF {state['pdf_fonts']})",
            5000,
        )

    def open_glossary_editor(self) -> None:
        """Открыть редактор словаря замен и исключений.

        После сохранения диалог уже перечитал файлы через
        :meth:`GlossaryManager.reload`, поэтому новые слова работают при
        следующей русификации без перезапуска приложения.

        Returns:
            None

        Raises:
            Ничего не бросает: ошибки показываются диалогом.
        """
        if self.glossary is None:
            QMessageBox.warning(
                self,
                "Редактор словаря",
                "Словарь недоступен: "
                + (self._glossary_error or "файлы данных не найдены"),
            )
            return

        dialog = GlossaryEditorDialog(self.glossary, self)
        if dialog.exec() != GlossaryEditorDialog.DialogCode.Accepted:
            return

        QMessageBox.information(self, "Редактор словаря", "Словарь обновлён")
        self.statusBar().showMessage(
            f"Словарь обновлён: {len(self.glossary.glossary)} терминов, "
            f"исключений: {len(self.glossary.exceptions)}",
            5000,
        )

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
        kind = self._active_kind()
        if kind == "pptx":
            text = self.pptx_editor.current_text()
            empty_message = "На слайде нет текста."
        elif kind == "pdf":
            text = self.pdf_editor.current_text()
            empty_message = "На странице нет текста."
        else:
            text = self.editor.toPlainText()
            empty_message = "Документ пуст."
        if not text.strip():
            QMessageBox.information(self, "Русификация", empty_message)
            return
        if getattr(self, "_worker", None) is not None and self._worker.isRunning():
            return

        settings = load_settings()
        self._rusify_cancelled = False
        self._rusify_target = kind
        # Диалог живёт в атрибуте окна: иначе Python соберёт его сборщиком
        # мусора, и прогресс-бар исчезнет до окончания работы воркера.
        self.progress_dialog = QProgressDialog(
            "Идёт анализ документа…", "Отмена", 0, 100, self
        )
        self.progress_dialog.setWindowTitle("Русификация")
        self.progress_dialog.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_dialog.setMinimumDuration(0)
        # autoClose/autoReset выключены: иначе setValue(100) сам вызывает
        # cancel(), сигнал canceled помечает прогон отменённым, и результат
        # воркера молча отбрасывается. Закрываем диалог только мы.
        self.progress_dialog.setAutoClose(False)
        self.progress_dialog.setAutoReset(False)
        self.progress_dialog.setValue(0)
        self.progress_dialog.show()
        self.progress_dialog.canceled.connect(self._cancel_rusify)

        self._set_surface_read_only(True)

        self._worker = RusifyWorker(
            text,
            parent=self,
            use_llm=settings["use_llm"],
            api_key=settings["api_key"],
            glossary=self.glossary,
        )
        self._worker.progress.connect(self.progress_dialog.setValue)
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
            target = {
                "word": "документе",
                "pptx": "слайде",
                "pdf": "странице",
            }.get(getattr(self, "_rusify_target", "word"), "документе")
            QMessageBox.information(
                self, "Русификация", f"Словарных замен в {target} не найдено."
            )
            return

        dialog = ReplacementsDialog(items, self)
        if dialog.exec() == ReplacementsDialog.DialogCode.Accepted:
            pairs = dialog.selected_replacements()
            surface = getattr(self, "_rusify_target", "word")
            if surface == "pptx":
                applied = self.pptx_editor.apply_replacements(pairs)
            elif surface == "pdf":
                applied = self.pdf_editor.apply_replacements(pairs)
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
        self._close_progress()
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
        surface = getattr(self, "_rusify_target", "word")
        if surface == "pptx":
            self.pptx_editor.set_read_only(read_only)
        elif surface == "pdf":
            self.pdf_editor.set_read_only(read_only)
        else:
            self.editor.setReadOnly(read_only)

    def _close_progress(self) -> None:
        """Закрыть и уничтожить прогресс-диалог, если он открыт.

        Сначала отключается ``canceled``: ``QProgressDialog`` считает закрытие
        отменой, а нам важно не пометить успешный прогон как отменённый.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        progress = getattr(self, "progress_dialog", None)
        if progress is None:
            return
        try:
            progress.canceled.disconnect(self._cancel_rusify)
        except (RuntimeError, TypeError):
            pass
        self.progress_dialog = None
        progress.close()
        progress.deleteLater()

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
        if not (
            self.editor.is_modified()
            or self.pptx_editor.is_modified()
            or self.pdf_editor.is_modified()
        ):
            self.pdf_editor.close_document()
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
            if self.pdf_editor.is_modified():
                self.tabs.setCurrentIndex(PDF_TAB_INDEX)
                if not self._save_pdf():
                    event.ignore()
                    return
        self.pdf_editor.close_document()
        event.accept()


