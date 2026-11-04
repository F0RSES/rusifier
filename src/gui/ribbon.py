"""Ribbon-панель в стиле ONLYOFFICE: вкладки сверху, ленты групп под ними.

Устройство: строка заголовка (быстрый доступ + ``QTabBar``) и ``QStackedWidget``
с лентой для каждой вкладки. Лента собирается из :class:`RibbonGroup` — групп
кнопок с подписью снизу и разделителями между группами, как в офисных пакетах.

Сами действия (``QAction``) создаёт окно: лента только раскладывает их по
вкладкам и группам и владеет «своими» виджетами — списками шрифта и кегля,
ползунком масштаба, переключателем темы и чекбоксом ИИ.

При сужении окна лента адаптируется: сначала кнопки становятся иконочными без
подписей (``COMPACT_WIDTH``), затем сворачивается вся лента и остаются только
вкладки (``COLLAPSE_WIDTH``).
"""

from __future__ import annotations

from typing import Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .start_screen import PALETTES, qt_icon

#: Подписи вкладок ленты.
HOME_TAB = "Главная"
INSERT_TAB = "Вставка"
TERMRU_TAB = "TermRu"
VIEW_TAB = "Вид"
TAB_NAMES = (HOME_TAB, INSERT_TAB, TERMRU_TAB, VIEW_TAB)

#: Высота ленты в обычном режиме (вкладки + группы с подписями).
RIBBON_HEIGHT = 128

#: Высота свёрнутой ленты: остаются только вкладки.
RIBBON_HEADER_HEIGHT = 36

#: Ниже этой ширины кнопки становятся иконочными (без подписи под иконкой).
COMPACT_WIDTH = 980

#: Ниже этой ширины лента сворачивается: остаются только вкладки.
COLLAPSE_WIDTH = 720

#: Доступные кегли в списке размера шрифта.
FONT_SIZES = (8, 9, 10, 11, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 64)

#: Варианты межстрочного интервала: ``(подпись, множитель)``.
LINE_SPACINGS = (
    ("1,0", 1.0),
    ("1,15", 1.15),
    ("1,5", 1.5),
    ("2,0", 2.0),
)

#: Шаблон QSS ленты: подставляются цвета темы.
STYLE_TEMPLATE = """
QWidget#ribbonRoot {{
    background: {card};
    border-bottom: 1px solid {border};
}}
QTabBar#ribbonTabs {{
    background: transparent;
}}
QTabBar#ribbonTabs::tab {{
    background: transparent;
    color: {muted};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 6px 16px;
    font-size: 11pt;
}}
QTabBar#ribbonTabs::tab:hover {{
    color: {text};
    background: {card_hover};
}}
QTabBar#ribbonTabs::tab:selected {{
    color: {text};
    border-bottom: 2px solid {accent};
    font-weight: 600;
}}
QFrame[group="true"] {{
    background: transparent;
    border: none;
}}
QLabel#ribbonGroupTitle {{
    color: {muted};
    font-size: 8pt;
    background: transparent;
}}
QFrame#ribbonSeparator {{
    background: {border};
    max-width: 1px;
    border: none;
}}
QToolButton[ribbon="true"] {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: {text};
}}
QToolButton[ribbon="true"]:hover {{
    background: {card_hover};
    border-color: {border};
}}
QToolButton[ribbon="true"]:checked {{
    background: {border};
    border-color: {accent};
}}
QToolButton[ribbon="true"]:disabled {{
    color: {muted};
}}
QToolButton#ribbonRussify {{
    background: {accent};
    border: 1px solid {accent};
    border-radius: 6px;
    color: #ffffff;
    font-size: 10pt;
    font-weight: 600;
    padding: 6px 14px;
}}
QToolButton#ribbonRussify:hover {{
    background: {card_hover};
    color: {text};
}}
QComboBox[ribbon="true"], QComboBox[ribbon="true"]:on {{
    background: {bg};
    color: {text};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 2px 6px;
    min-width: 64px;
}}
QCheckBox#ribbonUseLlm {{
    color: {text};
    font-size: 9pt;
    background: transparent;
}}
QLabel[ribbon="true"] {{
    color: {muted};
    font-size: 8pt;
    background: transparent;
}}
"""

class RibbonGroup(QFrame):
    """Группа кнопок ленты с подписью под ними.

    Args:
        title: Подпись группы («Шрифт», «Абзац», …).
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        """Создать пустую группу с рядом кнопок и подписью.

        Args:
            title: Подпись группы.
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setObjectName("ribbonGroup")
        self.setProperty("group", "true")
        self.title = title
        self.buttons: list[QToolButton] = []
        self._compact = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 2)
        layout.setSpacing(2)

        self.row_widget = QWidget(self)
        self.row = QHBoxLayout(self.row_widget)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(3)
        self.row.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.row_widget, 1)

        self.caption = QLabel(title, self)
        self.caption.setObjectName("ribbonGroupTitle")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.caption, 0)

        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)

    def add_action(self, action: QAction, *, big: bool = False) -> QToolButton:
        """Добавить кнопку, связанную с действием.

        Кнопка создаётся через ``setDefaultAction``: текст, иконка, подсказка и
        состояние «нажато» берутся из ``QAction``, поэтому синхронизация формата
        не требует обновлять кнопки отдельно.

        Args:
            action: Действие окна.
            big: Крупная кнопка (иконка сверху, подпись снизу).

        Returns:
            QToolButton: созданная кнопка.

        Raises:
            Ничего не бросает.
        """
        button = QToolButton(self.row_widget)
        button.setDefaultAction(action)
        button.setProperty("ribbon", "true")
        button.setAutoRaise(True)
        button.setProperty("big", "true" if big else "false")
        self._apply_style(button, big)
        button.setToolTip(action.toolTip() or action.text())
        self.row.addWidget(button)
        self.buttons.append(button)
        # Подпись действия может измениться («Русифицировать слайд» на вкладке
        # PPTX): минимальная ширина крупной кнопки должна считаться заново,
        # иначе новый текст обрезается.
        action.changed.connect(
            lambda: self._apply_style(button, big and not self._compact)
        )
        # Ряд не должен «съедать» высоту кнопки: подпись группы рисуется ниже.
        self.row_widget.setMinimumHeight(
            max(self.row_widget.minimumHeight(), button.minimumHeight())
        )
        return button

    def add_widget(self, widget: QWidget, *, stretch: int = 0) -> QWidget:
        """Добавить произвольный виджет (список, ползунок, чекбокс).

        Args:
            widget: Виджет для группы.
            stretch: Растяжение в ряду группы.

        Returns:
            QWidget: тот же виджет (для удобства цепочек).

        Raises:
            Ничего не бросает.
        """
        widget.setParent(self.row_widget)
        widget.setProperty("ribbon", "true")
        self.row.addWidget(widget, stretch)
        return widget

    def set_compact(self, compact: bool) -> None:
        """Переключить кнопки между режимом «иконка + подпись» и «только иконка».

        Args:
            compact: ``True`` — узкий режим (без подписей под иконками).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._compact == compact:
            return
        self._compact = compact
        for button in self.buttons:
            big = button.property("big") == "true"
            self._apply_style(button, big and not compact)

    @staticmethod
    def _apply_style(button: QToolButton, with_text: bool) -> None:
        """Задать стиль кнопки: с подписью или иконочный.

        Крупная кнопка получает минимальную ширину по ширине своей подписи:
        иначе «Русифицировать документ» обрезается до иконки.

        Args:
            button: Кнопка ленты.
            with_text: Показывать подпись под иконкой.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if not with_text:
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setIconSize(QSize(20, 20))
            inner = button.font()
            inner.setPointSizeF(8.0)
            button.setFont(inner)
            button.setMinimumWidth(0)
            return

        font = button.font()
        font.setPointSizeF(8.5)
        button.setFont(font)
        button.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        button.setIconSize(QSize(24, 24))
        button.setMinimumHeight(50)
        text_width = button.fontMetrics().horizontalAdvance(button.text())
        button.setMinimumWidth(max(72, text_width + 18))


class RibbonWidget(QWidget):
    """Лента в стиле ONLYOFFICE: вкладки, группы кнопок, быстрый доступ.

    Signals:
        theme_changed: выбрана тема оформления (``dark``/``light``).
        zoom_changed: изменён масштаб, проценты.
        use_llm_changed: переключён флажок «Использовать ИИ».

    Args:
        parent: Родительский виджет Qt.

    Raises:
        Ничего не бросает.
    """

    theme_changed = Signal(str)
    zoom_changed = Signal(int)
    use_llm_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Собрать вкладки, ленты и собственные виджеты ленты.

        Args:
            parent: Родительский виджет Qt.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setObjectName("ribbonRoot")
        self._groups: dict[tuple[str, str], RibbonGroup] = {}
        self._pages: dict[str, QWidget] = {}
        self._page_layouts: dict[str, QHBoxLayout] = {}
        self._action_tabs: dict[str, str] = {}
        self._buttons: dict[str, QToolButton] = {}
        self._compact = False
        self._collapsed = False
        self._theme = "dark"
        self._build_ui()
        self.build_widgets()

    # -- сборка --------------------------------------------------------------
    def _build_ui(self) -> None:
        """Создать строку вкладок, ленты и область быстрого доступа.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget(self)
        header.setObjectName("ribbonHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 2, 8, 0)
        header_layout.setSpacing(6)

        self.quick_widget = QWidget(header)
        self.quick_layout = QHBoxLayout(self.quick_widget)
        self.quick_layout.setContentsMargins(0, 0, 0, 0)
        self.quick_layout.setSpacing(2)
        header_layout.addWidget(self.quick_widget)

        self.tabs = QTabBar(header)
        self.tabs.setObjectName("ribbonTabs")
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        # Стрелки прокрутки вкладок выглядят как артефакт: при нехватке ширины
        # лента сворачивается сама, а вкладки остаются фиксированными.
        self.tabs.setUsesScrollButtons(False)
        for name in TAB_NAMES:
            self.tabs.addTab(name)
        header_layout.addWidget(self.tabs)
        header_layout.addStretch(1)
        root.addWidget(header)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("ribbonStack")
        for name in TAB_NAMES:
            page = QWidget(self.stack)
            page.setObjectName(f"ribbonPage{name}")
            page.setProperty("page", "true")
            layout = QHBoxLayout(page)
            layout.setContentsMargins(6, 2, 6, 2)
            layout.setSpacing(0)
            layout.addStretch(1)
            self.stack.addWidget(page)
            self._pages[name] = page
            self._page_layouts[name] = layout

        root.addWidget(self.stack, 1)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)
        self.setFixedHeight(RIBBON_HEIGHT)

        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.setCurrentIndex(0)


    # -- виджеты ленты -------------------------------------------------------
    def build_widgets(self) -> None:
        """Создать собственные виджеты ленты (списки, ползунок, флажок).

        Вызывается один раз после создания ленты: группы для них уже существуют.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        font_group = self.group(HOME_TAB, "Шрифт")
        self.font_combo = QComboBox(self)
        self.font_combo.setObjectName("ribbonFontCombo")
        self.font_combo.setEditable(True)
        self.font_combo.setMinimumWidth(140)
        for family in (
            "Segoe UI",
            "DejaVu Sans",
            "Liberation Sans",
            "Arial",
            "Times New Roman",
            "Courier New",
        ):
            self.font_combo.addItem(family)

        self.size_combo = QComboBox(self)
        self.size_combo.setObjectName("ribbonSizeCombo")
        self.size_combo.setEditable(True)
        self.size_combo.setMinimumWidth(56)
        for size in FONT_SIZES:
            self.size_combo.addItem(str(size), size)
        self.size_combo.setCurrentText("11")

        self.color_button = QToolButton(self)
        self.color_button.setObjectName("ribbonColor")
        self.color_button.setProperty("ribbon", "true")
        self.color_button.setProperty("big", "false")
        self.color_button.setAutoRaise(True)
        self.color_button.setIcon(qt_icon("fa5s.font", "#e06c75"))
        self.color_button.setIconSize(QSize(18, 18))
        self.color_button.setToolTip("Цвет текста")

        font_group.add_widget(self.font_combo)
        font_group.add_widget(self.size_combo)
        font_group.add_widget(self.color_button)

        self.spacing_combo = QComboBox(self)
        self.spacing_combo.setObjectName("ribbonSpacingCombo")
        self.spacing_combo.setMinimumWidth(64)
        self.spacing_combo.setToolTip("Межстрочный интервал")
        for label, value in LINE_SPACINGS:
            self.spacing_combo.addItem(label, value)
        self.group(HOME_TAB, "Абзац").add_widget(self.spacing_combo)

        self._build_theme_buttons()
        self._build_zoom_widgets()
        self._build_llm_widget()

    def _build_theme_buttons(self) -> None:
        """Создать переключатель темы на вкладке «Вид».

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.theme_buttons = QButtonGroup(self)
        self.theme_buttons.setExclusive(True)
        group = self.group(VIEW_TAB, "Тема")
        for name, label, icon_name in (
            ("dark", "Тёмная", "fa5s.moon"),
            ("light", "Светлая", "fa5s.sun"),
        ):
            button = QToolButton(self)
            button.setObjectName(f"ribbonTheme{name.capitalize()}")
            button.setProperty("ribbon", "true")
            button.setProperty("big", "true")
            button.setText(label)
            button.setIcon(qt_icon(icon_name, "#9aa0a6"))
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolTip(f"Тема: {label.lower()}")
            RibbonGroup._apply_style(button, True)
            button.clicked.connect(
                lambda _checked=False, value=name: self._choose_theme(value)
            )
            self.theme_buttons.addButton(button)
            group.add_widget(button)
            setattr(self, f"theme_{name}_button", button)
        self.theme_dark_button.setChecked(True)

    def _build_zoom_widgets(self) -> None:
        """Создать ползунок масштаба и подпись к нему.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        group = self.group(VIEW_TAB, "Масштаб")
        self.zoom_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.zoom_slider.setObjectName("ribbonZoomSlider")
        self.zoom_slider.setRange(50, 200)
        self.zoom_slider.setSingleStep(10)
        self.zoom_slider.setPageStep(25)
        self.zoom_slider.setValue(100)
        self.zoom_slider.setFixedWidth(130)
        self.zoom_slider.setToolTip("Масштаб документа")
        self.zoom_slider.valueChanged.connect(self._on_zoom_changed)
        group.add_widget(self.zoom_slider)

        self.zoom_label = QLabel("100 %", self)
        self.zoom_label.setObjectName("ribbonZoomLabel")
        self.zoom_label.setProperty("ribbon", "true")
        group.add_widget(self.zoom_label)

    def _build_llm_widget(self) -> None:
        """Создать флажок «Использовать ИИ» на вкладке TermRu.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        group = self.group(TERMRU_TAB, "Искусственный интеллект")
        self.use_llm_check = QCheckBox("Использовать ИИ (DeepSeek)", self)
        self.use_llm_check.setObjectName("ribbonUseLlm")
        self.use_llm_check.setToolTip("Дополнять словарные замены правками DeepSeek")
        self.use_llm_check.toggled.connect(self.use_llm_changed)
        group.add_widget(self.use_llm_check)


    # -- наполнение ----------------------------------------------------------
    def group(self, tab: str, title: str) -> RibbonGroup:
        """Вернуть группу вкладки, создав её при первом обращении.

        Args:
            tab: Подпись вкладки (``Главная``/``Вставка``/…).
            title: Подпись группы.

        Returns:
            RibbonGroup: группа кнопок.

        Raises:
            KeyError: неизвестная вкладка.
        """
        key = (tab, title)
        existing = self._groups.get(key)
        if existing is not None:
            return existing
        if tab not in self._page_layouts:
            raise KeyError(f"неизвестная вкладка ленты: {tab!r}")

        layout = self._page_layouts[tab]
        if any(owner == tab for owner, _ in self._groups):
            separator = QFrame(self._pages[tab])
            separator.setObjectName("ribbonSeparator")
            separator.setFrameShape(QFrame.Shape.VLine)
            separator.setFixedWidth(1)
            layout.insertWidget(layout.count() - 1, separator)

        group = RibbonGroup(title, self._pages[tab])
        layout.insertWidget(layout.count() - 1, group)
        self._groups[key] = group
        return group

    def add_quick_action(self, action: QAction) -> QToolButton:
        """Добавить кнопку в область быстрого доступа над вкладками.

        Args:
            action: Действие окна (открыть, сохранить, …).

        Returns:
            QToolButton: созданная кнопка.

        Raises:
            Ничего не бросает.
        """
        button = QToolButton(self.quick_widget)
        button.setDefaultAction(action)
        button.setProperty("ribbon", "true")
        button.setProperty("big", "false")
        button.setAutoRaise(True)
        button.setIconSize(QSize(18, 18))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setToolTip(action.toolTip() or action.text())
        self.quick_layout.addWidget(button)
        self._buttons[action.objectName() or action.text()] = button
        return button

    def add_action(
        self, tab: str, group_title: str, action: QAction, *, big: bool = False
    ) -> QToolButton:
        """Положить действие в группу указанной вкладки.

        Args:
            tab: Подпись вкладки.
            group_title: Подпись группы внутри вкладки.
            action: Действие окна.
            big: Крупная кнопка (иконка сверху, подпись снизу).

        Returns:
            QToolButton: созданная кнопка.

        Raises:
            KeyError: неизвестная вкладка.
        """
        group = self.group(tab, group_title)
        button = group.add_action(action, big=big)
        if self._compact and big:
            group.set_compact(True)
        name = action.objectName() or action.text()
        self._action_tabs[name] = tab
        self._buttons[name] = button
        return button

    def add_widget(self, tab: str, group_title: str, widget: QWidget) -> QWidget:
        """Положить произвольный виджет в группу вкладки.

        Args:
            tab: Подпись вкладки.
            group_title: Подпись группы.
            widget: Виджет.

        Returns:
            QWidget: тот же виджет.

        Raises:
            KeyError: неизвестная вкладка.
        """
        return self.group(tab, group_title).add_widget(widget)

    # -- состояние -----------------------------------------------------------
    def tab_names(self) -> list[str]:
        """Подписи вкладок ленты.

        Returns:
            list[str]: названия в порядке отображения.

        Raises:
            Ничего не бросает.
        """
        return [self.tabs.tabText(index) for index in range(self.tabs.count())]

    def current_tab(self) -> str:
        """Текущая вкладка ленты.

        Returns:
            str: подпись вкладки.

        Raises:
            Ничего не бросает.
        """
        return self.tabs.tabText(self.tabs.currentIndex())

    def set_current_tab(self, name: str) -> None:
        """Переключить ленту на вкладку по имени.

        Args:
            name: Подпись вкладки.

        Returns:
            None

        Raises:
            Ничего не бросает: неизвестное имя игнорируется.
        """
        for index, title in enumerate(self.tab_names()):
            if title == name:
                self.tabs.setCurrentIndex(index)
                return

    def actions_on(self, tab: str) -> list[str]:
        """Имена действий, размещённых на вкладке.

        Args:
            tab: Подпись вкладки.

        Returns:
            list[str]: ``objectName`` действий в порядке добавления.

        Raises:
            Ничего не бросает.
        """
        return [name for name, owner in self._action_tabs.items() if owner == tab]

    def tab_of_action(self, object_name: str) -> str | None:
        """Вкладка, на которой лежит действие.

        Args:
            object_name: ``objectName`` действия.

        Returns:
            str | None: подпись вкладки либо ``None``, если действия нет.

        Raises:
            Ничего не бросает.
        """
        return self._action_tabs.get(object_name)

    def button_for(self, object_name: str) -> QToolButton | None:
        """Кнопка, связанная с действием.

        Args:
            object_name: ``objectName`` действия.

        Returns:
            QToolButton | None: кнопка или ``None``.

        Raises:
            Ничего не бросает.
        """
        return self._buttons.get(object_name)


    def set_theme(self, name: str) -> None:
        """Отметить активную тему и перекрасить ленту.

        Args:
            name: ``dark`` или ``light``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self._theme = name if name in PALETTES else "dark"
        button = (
            self.theme_light_button if self._theme == "light" else self.theme_dark_button
        )
        button.setChecked(True)
        self.refresh_style(self._theme)

    def set_use_llm(self, enabled: bool) -> None:
        """Показать состояние флажка ИИ без испускания сигнала.

        Args:
            enabled: Включён ли режим ИИ.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.use_llm_check.blockSignals(True)
        self.use_llm_check.setChecked(bool(enabled))
        self.use_llm_check.blockSignals(False)

    def set_zoom(self, percent: int) -> None:
        """Показать масштаб в ползунке без испускания сигнала.

        Args:
            percent: Масштаб в процентах (50–200).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(max(50, min(200, int(percent))))
        self.zoom_slider.blockSignals(False)
        self.zoom_label.setText(f"{self.zoom_slider.value()} %")

    def set_compact(self, compact: bool) -> None:
        """Переключить кнопки в узкий режим (без подписей).

        Args:
            compact: ``True`` — только иконки.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        if self._compact == compact:
            return
        self._compact = compact
        for group in self._groups.values():
            group.set_compact(compact)

    def refresh_style(self, theme: str | None = None) -> None:
        """Перекрасить ленту под тему.

        Args:
            theme: ``dark``/``light``; ``None`` — текущая тема ленты.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        name = theme or self._theme
        palette = PALETTES.get(name, PALETTES["dark"])
        self._theme = name
        self.setStyleSheet(STYLE_TEMPLATE.format(**palette))
        if not hasattr(self, "color_button"):
            return
        self.color_button.setIcon(qt_icon("fa5s.font", palette["accent"]))
        self.theme_dark_button.setIcon(qt_icon("fa5s.moon", palette["muted"]))
        self.theme_light_button.setIcon(qt_icon("fa5s.sun", palette["muted"]))

    # -- обработчики ---------------------------------------------------------
    def _choose_theme(self, name: str) -> None:
        """Обработать клик по переключателю темы.

        Args:
            name: ``dark`` или ``light``.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.set_theme(name)
        self.theme_changed.emit(self._theme)

    def _on_zoom_changed(self, value: int) -> None:
        """Обновить подпись масштаба и сообщить окну.

        Args:
            value: Новое значение ползунка.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.zoom_label.setText(f"{value} %")
        self.zoom_changed.emit(value)

    def _on_tab_changed(self, _index: int) -> None:
        """Подстроить высоту ленты при смене вкладки.

        Args:
            _index: Индекс вкладки (не используется).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        target = RIBBON_HEADER_HEIGHT if self._collapsed else RIBBON_HEIGHT
        if self.height() != target:
            self.setFixedHeight(target)

    def apply_width(self, width: int) -> None:
        """Подстроить режим ленты под доступную ширину.

        Сначала кнопки теряют подписи (``COMPACT_WIDTH``), затем лента целиком
        сворачивается до строки вкладок (``COLLAPSE_WIDTH``). Обратное
        расширение возвращает подписи и ленту.

        Args:
            width: Доступная ширина ленты в пикселях.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        self.set_compact(width < COMPACT_WIDTH)
        collapsed = width < COLLAPSE_WIDTH
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.stack.setVisible(not collapsed)
        target = RIBBON_HEADER_HEIGHT if collapsed else RIBBON_HEIGHT
        if self.height() != target:
            self.setFixedHeight(target)

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - API Qt
        """Свернуть кнопки или всю ленту при нехватке ширины.

        Args:
            event: Событие изменения размера.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().resizeEvent(event)  # type: ignore[arg-type]
        self.apply_width(self.width())

