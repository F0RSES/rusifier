"""Настройки GUI: ключ DeepSeek и режим ИИ (хранится в QSettings)."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..deepseek_client import DeepSeekClient, DeepSeekError

#: Организация и приложение для QSettings (Linux: ~/.config/TermRu/Editor.conf).
ORGANIZATION = "TermRu"
APPLICATION = "Editor"
KEY_API = "deepseek/api_key"
KEY_USE_LLM = "deepseek/use_llm"


def app_settings() -> QSettings:
    """Открыть хранилище настроек приложения.

    Returns:
        QSettings: настройки ``TermRu/Editor``.

    Raises:
        Ничего не бросает.
    """
    return QSettings(ORGANIZATION, APPLICATION)


def load_settings() -> dict[str, Any]:
    """Прочитать ключ и флаг использования ИИ.

    Ключ без значения делает режим ИИ бессмысленным, поэтому флаг принудительно
    снимается, если ключа нет.

    Returns:
        dict[str, Any]: ``{"api_key": str, "use_llm": bool}``.

    Raises:
        Ничего не бросает.
    """
    settings = app_settings()
    api_key = str(settings.value(KEY_API, "") or "").strip()
    use_llm = bool(settings.value(KEY_USE_LLM, False, type=bool)) and bool(api_key)
    return {"api_key": api_key, "use_llm": use_llm}


def save_settings(api_key: str, use_llm: bool) -> None:
    """Сохранить настройки в QSettings.

    Args:
        api_key: Ключ DeepSeek (может быть пустым).
        use_llm: Использовать ли ИИ при русификации.

    Returns:
        None

    Raises:
        Ничего не бросает.
    """
    key = api_key.strip()
    settings = app_settings()
    settings.setValue(KEY_API, key)
    settings.setValue(KEY_USE_LLM, bool(use_llm) and bool(key))
    settings.sync()


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
        self.setWindowTitle("Настройки DeepSeek")
        self.resize(560, 260)
        self._client_factory = client_factory or DeepSeekClient

        current = initial if initial is not None else load_settings()

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.key_edit = QLineEdit(str(current.get("api_key", "")), self)
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("sk-…")
        form.addRow("API-ключ DeepSeek:", self.key_edit)

        self.check_button = QPushButton("Проверить ключ", self)
        self.check_button.clicked.connect(self.check_key)
        form.addRow("", self.check_button)

        self.use_llm = QCheckBox("Использовать ИИ (DeepSeek) при русификации", self)
        self.use_llm.setChecked(bool(current.get("use_llm", False)))
        form.addRow("", self.use_llm)

        layout.addLayout(form)
        layout.addWidget(
            QLabel(
                "Без ключа доступен только словарный режим. "
                "Ключ хранится локально в настройках приложения.",
                self,
            )
        )

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
            dict[str, Any]: ``{"api_key": str, "use_llm": bool}``.

        Raises:
            Ничего не бросает.
        """
        key = self.key_edit.text().strip()
        return {"api_key": key, "use_llm": bool(self.use_llm.isChecked()) and bool(key)}

    def save_and_close(self) -> None:
        """Сохранить значения в QSettings и закрыть диалог.

        Returns:
            None

        Raises:
            Ничего не бросает.
        """
        values = self.values()
        save_settings(values["api_key"], values["use_llm"])
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

