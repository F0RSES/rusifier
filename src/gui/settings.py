"""Настройки GUI: DeepSeek, тема оформления и режим шрифтов PDF.

Все значения хранятся в ``QSettings`` (``TermRu/Editor``): ключ API и режим ИИ,
тема (``dark``/``light``) и режим подмены шрифта при записи PDF.
"""

from __future__ import annotations

from typing import Any, Callable

import qdarkstyle
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..deepseek_client import DeepSeekClient, DeepSeekError
from ..pdf_fonts import FONT_SUBSTITUTION_MODES

#: Организация и приложение для QSettings (Linux: ~/.config/TermRu/Editor.conf).
ORGANIZATION = "TermRu"
APPLICATION = "Editor"
KEY_API = "deepseek/api_key"
KEY_USE_LLM = "deepseek/use_llm"
KEY_THEME = "ui/theme"
KEY_PDF_FONTS = "pdf/substitute_fonts"
KEY_RECENT = "files/recent"

#: Сколько недавних файлов помнить и показывать на стартовом экране.
RECENT_LIMIT = 5

#: Тёмная тема (qdarkstyle) и светлая (тот же стиль со светлой палитрой).
THEME_DARK = "dark"
THEME_LIGHT = "light"
THEMES: tuple[str, ...] = (THEME_DARK, THEME_LIGHT)
DEFAULT_THEME = THEME_DARK

#: Подписи тем в выпадающем списке.
THEME_LABELS = {THEME_DARK: "Dark (qdarkstyle)", THEME_LIGHT: "Светлая"}

#: Режим подмены шрифта по умолчанию: подставлять CID-шрифт при любой ошибке.
DEFAULT_PDF_FONTS = "always"

#: Подписи режимов PDF в выпадающем списке.
PDF_FONT_LABELS = {
    "strict": "strict — только base-14 шрифты",
    "base14": "base14 — base-14 и шрифты с ошибкой кодирования",
    "always": "always — подставлять при любой ошибке (рекомендуется)",
}

#: Подписи вкладок диалога настроек.
TAB_DEEPSEEK = "DeepSeek"
TAB_APPEARANCE = "Внешний вид"
TAB_PDF = "PDF"


def app_settings() -> QSettings:
    """Открыть хранилище настроек приложения.

    Returns:
        QSettings: настройки ``TermRu/Editor``.

    Raises:
        Ничего не бросает.
    """
    return QSettings(ORGANIZATION, APPLICATION)


def load_settings() -> dict[str, Any]:
    """Прочитать ключ, флаг ИИ, тему и режим шрифтов PDF.

    Ключ без значения делает режим ИИ бессмысленным, поэтому флаг принудительно
    снимается, если ключа нет. Неизвестные значения темы и режима PDF заменяются
    значениями по умолчанию.

    Returns:
        dict[str, Any]: ``{"api_key": str, "use_llm": bool, "theme": str,
        "pdf_fonts": str}``.

    Raises:
        Ничего не бросает.
    """
    settings = app_settings()
    api_key = str(settings.value(KEY_API, "") or "").strip()
    use_llm = bool(settings.value(KEY_USE_LLM, False, type=bool)) and bool(api_key)
    theme = str(settings.value(KEY_THEME, DEFAULT_THEME) or DEFAULT_THEME)
    pdf_fonts = str(settings.value(KEY_PDF_FONTS, DEFAULT_PDF_FONTS) or DEFAULT_PDF_FONTS)
    return {
        "api_key": api_key,
        "use_llm": use_llm,
        "theme": theme if theme in THEMES else DEFAULT_THEME,
        "pdf_fonts": pdf_fonts if pdf_fonts in FONT_SUBSTITUTION_MODES else DEFAULT_PDF_FONTS,
    }


def save_settings(
    api_key: str,
    use_llm: bool,
    theme: str = DEFAULT_THEME,
    pdf_fonts: str = DEFAULT_PDF_FONTS,
) -> None:
    """Сохранить настройки в QSettings.

    Args:
        api_key: Ключ DeepSeek (может быть пустым).
        use_llm: Использовать ли ИИ при русификации.
        theme: Тема оформления (``dark`` или ``light``).
        pdf_fonts: Режим подмены шрифта PDF (``strict``/``base14``/``always``).

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    key = api_key.strip()
    settings = app_settings()
    settings.setValue(KEY_API, key)
    settings.setValue(KEY_USE_LLM, bool(use_llm) and bool(key))
    settings.setValue(KEY_THEME, theme if theme in THEMES else DEFAULT_THEME)
    settings.setValue(
        KEY_PDF_FONTS,
        pdf_fonts if pdf_fonts in FONT_SUBSTITUTION_MODES else DEFAULT_PDF_FONTS,
    )
    settings.sync()


def load_recent_files() -> list[str]:
    """Прочитать список недавних файлов из QSettings.

    QSettings может вернуть как список, так и одиночную строку (зависит от бэкенда
    и от того, сколько значений сохранено), поэтому оба варианта приводятся к
    списку без дубликатов.

    Returns:
        list[str]: пути в порядке убывания свежести (не больше ``RECENT_LIMIT``).

    Raises:
        Ничего не бросает.
    """
    raw = app_settings().value(KEY_RECENT, [])
    if isinstance(raw, str):
        raw = [raw]
    result: list[str] = []
    for item in raw or []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result[:RECENT_LIMIT]


def remember_recent_file(path: str) -> list[str]:
    """Добавить файл в начало списка недавних и сохранить его.

    Args:
        path: Путь открытого файла.

    Returns:
        list[str]: обновлённый список недавних файлов.

    Raises:
        Ничего не бросает.
    """
    text = str(path).strip()
    if not text:
        return load_recent_files()
    items = [item for item in load_recent_files() if item != text]
    items.insert(0, text)
    items = items[:RECENT_LIMIT]
    settings = app_settings()
    settings.setValue(KEY_RECENT, items)
    settings.sync()
    return items


def stylesheet_for(theme: str) -> str:
    """Собрать таблицу стилей qdarkstyle для темы.

    Args:
        theme: ``dark`` или ``light`` (неизвестное значение — тёмная тема).

    Returns:
        str: готовая таблица стилей для ``QApplication.setStyleSheet``.

    Raises:
        Ничего не бросает.
    """
    palette = (
        qdarkstyle.LightPalette if theme == THEME_LIGHT else qdarkstyle.DarkPalette
    )
    return qdarkstyle.load_stylesheet(qt_api="pyside6", palette=palette)


def apply_theme(theme: str) -> str:
    """Применить тему к запущенному приложению.

    Args:
        theme: ``dark`` или ``light``.

    Returns:
        str: фактически применённая тема (неизвестная заменяется на ``dark``).

    Raises:
        Ничего не бросает: без ``QApplication`` стиль просто не применяется.
    """
    name = theme if theme in THEMES else DEFAULT_THEME
    app = QApplication.instance()
    if app is not None:
        app.setStyleSheet(stylesheet_for(name))
    return name


class SettingsDialog(QDialog):
    """Диалог настроек DeepSeek: ключ, режим ИИ, проверка ключа.

    Args:
        parent: Родительский виджет Qt.
        client_factory: Фабрика клиента (``deepseek_client.DeepSeekClient`` по
            умолчанию); подменяется в тестах, чтобы не ходить в сеть.
        initial: Начальные значения; ``None`` — прочитать из QSettings.

    Raises:
        Ничего не бросает.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        client_factory: Callable[..., Any] | None = None,
        initial: dict[str, Any] | None = None,
    ) -> None:
        """Собрать диалог настроек.

        Args:
            parent: Родительский виджет Qt.
            client_factory: Фабрика ``DeepSeekClient`` для проверки ключа.
            initial: Начальные значения настроек.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.resize(620, 380)
        self._client_factory = client_factory or DeepSeekClient

        current = initial if initial is not None else load_settings()
        self._initial_theme = str(current.get("theme", DEFAULT_THEME))

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("settingsTabs")
        self.tabs.addTab(self._build_deepseek_tab(current), TAB_DEEPSEEK)
        self.tabs.addTab(self._build_appearance_tab(current), TAB_APPEARANCE)
        self.tabs.addTab(self._build_pdf_tab(current), TAB_PDF)
        layout.addWidget(self.tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.accepted.connect(self.save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.key_edit.textChanged.connect(self._sync_llm_state)
        self._sync_llm_state(self.key_edit.text())
        # Сигнал темы подключается после установки значения, чтобы открытие
        # диалога не применяло стиль повторно.
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)

    def _build_deepseek_tab(self, current: dict[str, Any]) -> QWidget:
        """Собрать вкладку с ключом DeepSeek и режимом ИИ.

        Args:
            current: Начальные значения настроек.

        Returns:
            QWidget: вкладка «DeepSeek».

        Raises:
            Ничего не бросает.
        """
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        form = QFormLayout()

        self.key_edit = QLineEdit(str(current.get("api_key", "")), page)
        self.key_edit.setObjectName("apiKeyEdit")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…")
        form.addRow("API-ключ DeepSeek:", self.key_edit)

        self.check_button = QPushButton("Проверить ключ", page)
        self.check_button.clicked.connect(self.check_key)
        form.addRow("", self.check_button)

        self.use_llm = QCheckBox("Использовать ИИ (DeepSeek) при русификации", page)
        self.use_llm.setChecked(bool(current.get("use_llm", False)))
        form.addRow("", self.use_llm)

        page_layout.addLayout(form)
        page_layout.addWidget(
            QLabel(
                "Без ключа доступен только словарный режим. "
                "Ключ хранится локально в настройках приложения.",
                page,
            )
        )
        page_layout.addStretch(1)
        return page

    def _build_appearance_tab(self, current: dict[str, Any]) -> QWidget:
        """Собрать вкладку «Внешний вид» с выбором темы.

        Args:
            current: Начальные значения настроек.

        Returns:
            QWidget: вкладка выбора темы.

        Raises:
            Ничего не бросает.
        """
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        form = QFormLayout()

        self.theme_combo = QComboBox(page)
        self.theme_combo.setObjectName("themeCombo")
        for name in THEMES:
            self.theme_combo.addItem(THEME_LABELS[name], name)
        theme = str(current.get("theme", DEFAULT_THEME))
        index = self.theme_combo.findData(theme if theme in THEMES else DEFAULT_THEME)
        self.theme_combo.setCurrentIndex(max(0, index))
        form.addRow("Тема оформления:", self.theme_combo)

        page_layout.addLayout(form)
        page_layout.addWidget(
            QLabel("Тема применяется сразу. При отмене возвращается прежняя.", page)
        )
        page_layout.addStretch(1)
        return page

    def _build_pdf_tab(self, current: dict[str, Any]) -> QWidget:
        """Собрать вкладку с режимом подмены шрифта PDF.

        Args:
            current: Начальные значения настроек.

        Returns:
            QWidget: вкладка «PDF».

        Raises:
            Ничего не бросает.
        """
        page = QWidget(self)
        page_layout = QVBoxLayout(page)
        form = QFormLayout()

        self.pdf_combo = QComboBox(page)
        self.pdf_combo.setObjectName("pdfFontCombo")
        for mode in FONT_SUBSTITUTION_MODES:
            self.pdf_combo.addItem(PDF_FONT_LABELS.get(mode, mode), mode)
        pdf_fonts = str(current.get("pdf_fonts", DEFAULT_PDF_FONTS))
        index = self.pdf_combo.findData(
            pdf_fonts if pdf_fonts in FONT_SUBSTITUTION_MODES else DEFAULT_PDF_FONTS
        )
        self.pdf_combo.setCurrentIndex(max(0, index))
        form.addRow("Подмена шрифта:", self.pdf_combo)

        page_layout.addLayout(form)
        page_layout.addWidget(
            QLabel(
                "Режим применяется при записи PDF: если шрифт блока не умеет "
                "кириллицу, TermRu подставляет DejaVu из data/fonts.\n"
                "strict — только base-14; base14 — base-14 и шрифты с ошибкой "
                "кодирования; always — при любой ошибке.",
                page,
            )
        )
        page_layout.addStretch(1)
        return page

    def theme(self) -> str:
        """Выбранная тема оформления.

        Returns:
            str: ``dark`` или ``light``.

        Raises:
            Ничего не бросает.
        """
        data = self.theme_combo.currentData()
        return str(data) if data in THEMES else DEFAULT_THEME

    def pdf_font_mode(self) -> str:
        """Выбранный режим подмены шрифта PDF.

        Returns:
            str: ``strict``, ``base14`` или ``always``.

        Raises:
            Ничего не бросает.
        """
        data = self.pdf_combo.currentData()
        return str(data) if data in FONT_SUBSTITUTION_MODES else DEFAULT_PDF_FONTS

    def _on_theme_changed(self, _index: int) -> None:
        """Применить выбранную тему сразу.

        Args:
            _index: Индекс выбранного элемента (не используется).

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        apply_theme(self.theme())

    def reject(self) -> None:
        """Закрыть диалог без сохранения и вернуть прежнюю тему.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        apply_theme(self._initial_theme)
        super().reject()

    def _sync_llm_state(self, text: str) -> None:
        """Запретить режим ИИ, пока ключ пуст.

        Args:
            text: Текущий текст поля ключа.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        has_key = bool(text.strip())
        self.use_llm.setEnabled(has_key)
        self.check_button.setEnabled(has_key)
        if not has_key:
            self.use_llm.setChecked(False)

    def values(self) -> dict[str, Any]:
        """Текущие значения полей.

        Returns:
            dict[str, Any]: ``{"api_key": str, "use_llm": bool, "theme": str,
            "pdf_fonts": str}``.

        Raises:
            Ничего не бросает.
        """
        key = self.key_edit.text().strip()
        return {
            "api_key": key,
            "use_llm": bool(self.use_llm.isChecked()) and bool(key),
            "theme": self.theme(),
            "pdf_fonts": self.pdf_font_mode(),
        }

    def save_and_close(self) -> None:
        """Сохранить значения в QSettings и закрыть диалог.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        values = self.values()
        save_settings(
            values["api_key"],
            values["use_llm"],
            values["theme"],
            values["pdf_fonts"],
        )
        self.accept()

    def check_key(self) -> None:
        """Проверить ключ реальным запросом к API и показать результат.

        Returns:
            None

        Raises:
            Ничего не бросает: ошибки показываются диалогом.
        """
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "Проверка ключа", "Введите API-ключ.")
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            client = self._client_factory(api_key=key)
            client.replace_text("Ping", {}, {})
        except DeepSeekError as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Проверка ключа", f"Ошибка API: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - любой сбой конфигурации/сети
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(
                self, "Проверка ключа", f"Не удалось проверить ключ: {exc}"
            )
            return
        QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "Проверка ключа", "Ключ работает.")

