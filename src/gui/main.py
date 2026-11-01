"""Точка входа GUI TermRu: ``python -m src.gui.main``.

Тема оформления — qdarkstyle (строгий тёмный стиль в духе офисных приложений).
Пакет ``qdarktheme``/``pyqtdarktheme`` не используется: его версии ограничены
Python < 3.12, а проект работает на 3.13.
"""

from __future__ import annotations

import sys

import qdarkstyle
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow


def main(argv: list[str] | None = None) -> int:
    """Запустить приложение.

    Args:
        argv: Аргументы командной строки; ``None`` — ``sys.argv``.

    Returns:
        int: Код возврата Qt-цикла событий.

    Raises:
        Ничего не бросает: ошибки Qt попадают в stderr стандартным обработчиком.
    """
    # Политика масштабирования задаётся до создания QApplication.
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("TermRu")
    app.setApplicationDisplayName("TermRu")
    app.setOrganizationName("TermRu contributors")
    app.setStyleSheet(qdarkstyle.load_stylesheet(qt_api="pyside6"))

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
