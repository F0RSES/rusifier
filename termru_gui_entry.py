"""Точка входа GUI-сборки TermRu (PyInstaller).

Один и тот же бинарник запускает и графический редактор, и CLI:

* без аргументов — GUI (``src.gui.main``);
* с любым аргументом — CLI (``src.cli``), поэтому ``TermRu --input a.docx --json``
  и ``termru --help`` работают так же, как у прежней консольной сборки;
* ``--gui`` — принудительно GUI (если аргументы всё-таки есть);
* ``--gui-smoke`` — служебный режим CI: поднять Qt, собрать окно, проверить
  вкладки и выйти с кодом 0 (нужен дисплей, в CI — ``xvfb-run``).

PyInstaller запускает этот файл как ``__main__`` без пакета, поэтому импорты
``src.*`` идут после настройки путей и фильтра предупреждений.
"""

from __future__ import annotations

import sys
import warnings

#: Форматы polydoc, которых в сборке нет (исключены через ``exclude_module``
#: в ``unifypy.json``). Фильтр ставится до импорта polydoc, иначе предупреждения
#: «reader is unavailable» попадают в stderr CLI.
_EXCLUDED_FORMATS = "txt|markdown|html|json|csv|xlsx"
warnings.filterwarnings(
    "ignore",
    message=rf"polydoc: the '(?:{_EXCLUDED_FORMATS})' (?:reader|writer) is unavailable",
    category=RuntimeWarning,
)

#: Служебный флаг CI: поднять GUI, проверить вкладки и выйти.
GUI_SMOKE_FLAG = "--gui-smoke"

#: Флаг, принудительно включающий GUI (по умолчанию любой аргумент = CLI).
GUI_FLAG = "--gui"


def use_cli_mode(argv: "list[str]") -> bool:
    """Решить по аргументам, какой режим запускать.

    GUI аргументов не принимает, поэтому любое слово в командной строке — это
    запрос CLI (``--help``, ``--version``, ``--input ...``). Исключение —
    служебные флаги ``--gui`` и ``--gui-smoke``.

    Args:
        argv: Аргументы командной строки без имени программы.

    Returns:
        bool: ``True`` — запускать CLI, ``False`` — GUI.

    Raises:
        Ничего не бросает.
    """
    for argument in argv:
        if argument in (GUI_FLAG, GUI_SMOKE_FLAG):
            return False
    return bool(argv)


def _attach_parent_console() -> None:
    """Подключиться к консоли родителя (Windows, сборка без консоли).

    ``windowed``-сборка PyInstaller не имеет ``stdout``/``stderr``, поэтому в
    CLI-режиме вывод потерялся бы. ``AttachConsole(ATTACH_PARENT_PROCESS)``
    возвращает его в терминал, из которого запущен ``TermRu.exe``.

    Returns:
        None

    Raises:
        Ничего не бросает: без консоли вывод просто останется пустым.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
            return
        for name, device, mode in (
            ("stdout", "CONOUT$", "w"),
            ("stderr", "CONOUT$", "w"),
            ("stdin", "CONIN$", "r"),
        ):
            if getattr(sys, name) is not None:
                continue
            setattr(sys, name, open(device, mode, encoding="utf-8", buffering=1))
    except Exception:  # noqa: BLE001 - отсутствие консоли не должно ломать запуск
        return


def main(argv: "list[str] | None" = None) -> int:
    """Запустить GUI или CLI в зависимости от аргументов.

    Args:
        argv: Аргументы без имени программы; ``None`` — ``sys.argv[1:]``.

    Returns:
        int: код возврата приложения.

    Raises:
        Ничего не бросает: ошибки обрабатываются внутри выбранного режима.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)

    if GUI_SMOKE_FLAG in arguments:
        from src.gui.main import smoke_check

        return smoke_check()

    if use_cli_mode(arguments):
        _attach_parent_console()
        from src.cli import main as cli_main

        return cli_main(arguments)

    from src.gui.main import main as gui_main

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
