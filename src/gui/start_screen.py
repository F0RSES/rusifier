"""Приветственный экран TermRu: боковая навигация, карточки создания, недавние файлы.

Экран показывается при запуске и по «Файл → На главный экран». Сам он ничего не
открывает и не сохраняет: он только сообщает наружу сигналами, а решения принимает
:class:`~src.gui.main_window.MainWindow`. Так экран остаётся тестируемым без окна.

Оформление задаётся QSS, собранным из палитры темы (``dark``/``light`` из
настроек), поэтому при переключении темы экран перекрашивается вместе с окном.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QIcon
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .settings import THEME_DARK, load_settings

#: Заголовок приветственного экрана.
WELCOME_TITLE = "Добро пожаловать в TermRu"

#: Подзаголовок приветственного экрана.
WELCOME_SUBTITLE = "Умный редактор документов с русификацией"

#: Карточки создания: ``(вид, заголовок, подпись, иконка, цвет иконки)``.
CREATE_CARDS = (
    ("docx", "Word", "Документ DOCX", "fa5s.file-word", "#2b579a"),
    ("pptx", "PowerPoint", "Презентация PPTX", "fa5s.file-powerpoint", "#d24726"),
    ("pdf", "PDF", "Пустая страница PDF", "fa5s.file-pdf", "#c0392b"),
)

#: Пункты боковой навигации: ``(идентификатор, подпись, иконка)``.
NAV_ITEMS = (
    ("home", "Главная", "fa5s.home"),
    ("open", "Открыть файл", "fa5s.folder-open"),
    ("settings", "Настройки", "fa5s.cog"),
    ("about", "О программе", "fa5s.info-circle"),
)

#: Цвета экрана для тёмной и светлой темы.
PALETTES = {
    "dark": {
        "bg": "#232629",
        "sidebar": "#1d2022",
        "card": "#2b2f33",
        "card_hover": "#353b40",
        "border": "#3a3f44",
        "text": "#eff0f1",
        "muted": "#9aa0a6",
        "accent": "#4a90d9",
    },
    "light": {
        "bg": "#f4f5f7",
        "sidebar": "#e8eaed",
        "card": "#ffffff",
        "card_hover": "#eef4fb",
        "border": "#d6d9dd",
        "text": "#1f2328",
        "muted": "#5f6368",
        "accent": "#2b579a",
    },
}

#: Шаблон QSS экрана: цвета подставляются из PALETTES.
STYLE_TEMPLATE = """
QWidget#startRoot {{
    background: {bg};
}}
QScrollArea#startScroll {{
    background: {bg};
    border: none;
}}
QWidget#startContent {{
    background: {bg};
}}
QWidget#startRoot QLabel {{
    background: transparent;
}}
QWidget#startSidebar {{
    background: {sidebar};
    border-right: 1px solid {border};
}}
QLabel#startBrand {{
    color: {text};
    font-size: 18pt;
    font-weight: 700;
}}
QLabel#startTitle {{
    color: {text};
    font-size: 25pt;
    font-weight: 700;
}}
QLabel#startSubtitle {{
    color: {muted};
    font-size: 13pt;
}}
QLabel#sectionLabel {{
    color: {muted};
    font-size: 10pt;
    font-weight: 600;
}}
QLabel#recentEmpty {{
    color: {muted};
    font-size: 11pt;
}}
QFrame#startDivider {{
    background: {border};
    max-height: 1px;
    border: none;
}}
QPushButton[nav="true"] {{
    text-align: left;
    padding: 9px 12px;
    border: none;
    border-radius: 6px;
    background: transparent;
    color: {text};
    font-size: 11pt;
}}
QPushButton[nav="true"]:hover {{
    background: {card_hover};
}}
QPushButton[card="true"] {{
    background: {card};
    border: 1px solid {border};
    border-radius: 12px;
    color: {text};
    font-size: 12pt;
    padding: 18px 14px;
    text-align: center;
}}
QPushButton[card="true"]:hover {{
    background: {card_hover};
    border-color: {accent};
}}
QPushButton[card="true"]:pressed {{
    background: {border};
}}
QPushButton#openButton {{
    background: {card};
    border: 1px dashed {border};
    border-radius: 10px;
    color: {text};
    font-size: 12pt;
    padding: 14px;
    text-align: left;
}}
QPushButton#openButton:hover {{
    border-color: {accent};
    background: {card_hover};
}}
QPushButton[recent="true"] {{
    text-align: left;
    background: transparent;
    border: none;
    color: {accent};
    padding: 3px 2px;
    font-size: 11pt;
}}
QPushButton[recent="true"]:hover {{
    color: {text};
}}
"""


def qt_icon(name: str, color: str = "") -> QIcon:
    """Создать иконку qtawesome, не падая без библиотеки.

    Args:
        name: Имя иконки, например ``fa5s.file-word``.
        color: Цвет заливки (``#rrggbb``) либо пустая строка.

    Returns:
        QIcon: иконка либо пустая иконка, если qtawesome недоступен.

    Raises:
        Ничего не бросает.
    """
    try:
        import qtawesome as qta  # noqa: PLC0415 - необязательная зависимость
    except ImportError:  # pragma: no cover - qtawesome есть в requirements
        return QIcon()
    try:
        return qta.icon(name, **({"color": color} if color else {}))
    except Exception:  # noqa: BLE001 - неизвестное имя не должно ломать экран
        return QIcon()


class StartScreen(QWidget):
    """Приветственный экран: навигация, создание документа, недавние файлы.

    Signals:
        create_requested: вид документа для создания (``docx``/``pptx``/``pdf``).
        open_requested: пользователь хочет открыть файл (диалог показывает окно).
        recent_requested: путь к файлу из списка недавних.
        settings_requested: открыть настройки.
        about_requested: открыть «О программе».
        file_dropped: путь, перетащенный на экран.

    Args:
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    create_requested = Signal(str)
    open_requested = Signal()
    recent_requested = Signal(str)
    settings_requested = Signal()
    about_requested = Signal()
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Собрать экран и применить стиль текущей темы.

        Args:
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setObjectName("startRoot")
        self.setAcceptDrops(True)
        self._recent_layout: QVBoxLayout | None = None
        self._recent_paths: list[str] = []
        self._cards: dict[str, QPushButton] = {}
        self._build_ui()
        self.refresh_style()

    # -- сборка интерфейса ---------------------------------------------------
    def _build_ui(self) -> None:
        """Создать боковую панель и центральную область с блоками.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())

        scroll = QScrollArea(self)
        scroll.setObjectName("startScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(scroll)
        content.setObjectName("startContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(40, 32, 40, 32)
        content_layout.setSpacing(10)

        title = QLabel(WELCOME_TITLE, content)
        title.setObjectName("startTitle")
        title.setWordWrap(True)
        content_layout.addWidget(title)

        subtitle = QLabel(WELCOME_SUBTITLE, content)
        subtitle.setObjectName("startSubtitle")
        subtitle.setWordWrap(True)
        content_layout.addWidget(subtitle)
        content_layout.addSpacing(18)

        content_layout.addWidget(self._section("Создать новый", content))
        content_layout.addLayout(self._build_cards(content))
        content_layout.addSpacing(14)

        content_layout.addWidget(self._section("Открыть", content))
        self.open_button = QPushButton(
            "Открыть файл с компьютера", content
        )
        self.open_button.setObjectName("openButton")
        self.open_button.setIcon(qt_icon("fa5s.folder-open", "#4a90d9"))
        self.open_button.setIconSize(self.open_button.iconSize() * 1.2)
        self.open_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.open_button.clicked.connect(self.open_requested)
        content_layout.addWidget(self.open_button)
        content_layout.addSpacing(14)

        content_layout.addWidget(self._section("Недавние файлы", content))
        self.recent_container = QWidget(content)
        self._recent_layout = QVBoxLayout(self.recent_container)
        self._recent_layout.setContentsMargins(0, 0, 0, 0)
        self._recent_layout.setSpacing(2)
        content_layout.addWidget(self.recent_container)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self._render_recent()

    def _build_sidebar(self) -> QWidget:
        """Создать боковую панель с навигацией.

        Returns:
            QWidget: панель навигации.

        Raises:
            Ничего не бросает.
        """
        sidebar = QWidget(self)
        sidebar.setObjectName("startSidebar")
        sidebar.setFixedWidth(210)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 22, 14, 18)
        layout.setSpacing(6)

        brand = QLabel("TermRu", sidebar)
        brand.setObjectName("startBrand")
        layout.addWidget(brand)
        layout.addSpacing(16)

        handlers = {
            "home": lambda: None,
            "open": self.open_requested.emit,
            "settings": self.settings_requested.emit,
            "about": self.about_requested.emit,
        }
        self.nav_buttons: dict[str, QPushButton] = {}
        for key, text, icon_name in NAV_ITEMS:
            button = QPushButton(text, sidebar)
            button.setProperty("nav", "true")
            button.setIcon(qt_icon(icon_name, "#9aa0a6"))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(handlers[key])
            layout.addWidget(button)
            self.nav_buttons[key] = button
        layout.addStretch(1)

        hint = QLabel("Открывайте DOCX, PPTX и PDF\nи русифицируйте их.", sidebar)
        hint.setObjectName("sectionLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return sidebar

    @staticmethod
    def _section(text: str, parent: QWidget) -> QLabel:
        """Заголовок блока.

        Args:
            text: Текст заголовка.
            parent: Родительский виджет.

        Returns:
            QLabel: подпись блока.

        Raises:
            Ничего не бросает.
        """
        label = QLabel(text.upper(), parent)
        label.setObjectName("sectionLabel")
        return label


    def _build_cards(self, parent: QWidget) -> QHBoxLayout:
        """Создать три карточки «новый документ».

        Args:
            parent: Родительский виджет.

        Returns:
            QHBoxLayout: ряд карточек.

        Raises:
            Ничего не бросает.
        """
        row = QHBoxLayout()
        row.setSpacing(14)
        for kind, title, caption, icon_name, color in CREATE_CARDS:
            card = QPushButton(f"{title}\n{caption}", parent)
            card.setObjectName(f"card{kind.capitalize()}")
            card.setProperty("card", "true")
            card.setProperty("kind", kind)
            card.setIcon(qt_icon(icon_name, color))
            card.setIconSize(card.iconSize() * 2.2)
            card.setMinimumHeight(112)
            card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            card.setCursor(Qt.CursorShape.PointingHandCursor)
            card.setToolTip(f"Создать пустой {caption.lower()}")
            card.clicked.connect(lambda _checked=False, k=kind: self.create_requested.emit(k))
            row.addWidget(card)
            self._cards[kind] = card
        return row

    # -- недавние файлы ------------------------------------------------------
    def set_recent_files(self, paths: list[str]) -> None:
        """Показать список недавних файлов.

        Args:
            paths: Пути в порядке убывания свежести.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._recent_paths = [str(path) for path in paths]
        self._render_recent()

    def recent_files(self) -> list[str]:
        """Текущий список недавних файлов.

        Returns:
            list[str]: пути в порядке отображения.

        Raises:
            Ничего не бросает.
        """
        return list(self._recent_paths)

    def _render_recent(self) -> None:
        """Перерисовать блок недавних файлов.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        layout = self._recent_layout
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        if not self._recent_paths:
            empty = QLabel("Пока пусто — откройте первый документ.", self.recent_container)
            empty.setObjectName("recentEmpty")
            layout.addWidget(empty)
            return

        for path in self._recent_paths:
            button = QPushButton(Path(path).name, self.recent_container)
            button.setProperty("recent", "true")
            button.setIcon(qt_icon("fa5s.file-alt", "#9aa0a6"))
            button.setToolTip(path)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, target=path: self.recent_requested.emit(target)
            )
            layout.addWidget(button)

    # -- оформление ----------------------------------------------------------
    def refresh_style(self, theme: str | None = None) -> None:
        """Пересобрать QSS под тему и обновить иконки навигации.

        Args:
            theme: Тема (``dark``/``light``); ``None`` — взять из настроек.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        name = theme or str(load_settings().get("theme") or THEME_DARK)
        palette = PALETTES.get(name, PALETTES[THEME_DARK])
        self.setStyleSheet(STYLE_TEMPLATE.format(**palette))
        for key, _text, icon_name in NAV_ITEMS:
            button = getattr(self, "nav_buttons", {}).get(key)
            if button is not None:
                button.setIcon(qt_icon(icon_name, palette["muted"]))
        if hasattr(self, "open_button"):
            self.open_button.setIcon(qt_icon("fa5s.folder-open", palette["accent"]))

    # -- перетаскивание ------------------------------------------------------
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - API Qt
        """Принять перетаскивание файла.

        Args:
            event: Событие перетаскивания.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        urls = event.mimeData().urls()
        if any(url.isLocalFile() for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - API Qt
        """Открыть первый локальный файл из перетащенных.

        Args:
            event: Событие сброса.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        for url in event.mimeData().urls():
            if url.isLocalFile():
                event.acceptProposedAction()
                self.file_dropped.emit(url.toLocalFile())
                return
        event.ignore()

