"""Точка входа GUI TermRu: ``python -m src.gui.main``.

Тема оформления — qdarkstyle (тёмная или светлая, выбор хранится в настройках).
Пакет ``qdarktheme``/``pyqtdarktheme`` не используется: его версии ограничены
Python < 3.12, а проект работает на 3.13.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .settings import apply_theme, load_settings


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
    apply_theme(load_settings()["theme"])

    window = MainWindow()
    window.show()
    return app.exec()


def smoke_check() -> int:
    """Поднять окно и проверить, что интерфейс собирается (режим CI).

    Вызывается собранным бинарником с флагом ``--gui-smoke``: под виртуальным
    дисплеем (``xvfb-run``) проверяется, что Qt инициализируется, главное окно
    строится и в нём три вкладки — без ожидания действий пользователя.

    Returns:
        int: ``0`` — GUI поднялся, ``1`` — окно не собралось.

    Raises:
        Ничего не бросает: ошибки Qt попадают в stderr стандартным обработчиком.
    """
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("TermRu")
    app.setApplicationDisplayName("TermRu")
    app.setOrganizationName("TermRu contributors")
    apply_theme(load_settings()["theme"])

    window = MainWindow()
    window.show()
    app.processEvents()
    tabs = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    window.close()
    app.processEvents()

    if len(tabs) < 3:
        print(f"GUI smoke: ожидались 3 вкладки, получено {tabs}", file=sys.stderr)
        return 1
    print("GUI smoke ok: " + ", ".join(tabs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
