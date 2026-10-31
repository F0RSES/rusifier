"""Точка входа для PyInstaller-сборок.

Приложение живёт в пакете ``src`` и запускается как ``python -m src.cli``;
внутри ``src/cli.py`` используются относительные импорты (``from .document_processor
import ...``). PyInstaller запускает переданный скрипт как ``__main__`` без пакета,
поэтому относительные импорты в нём не работают.

Этот файл лежит в корне проекта (``src`` импортируется из текущего каталога) и
вызывает ``src.cli.main`` — единственную публичную точку входа CLI.
"""

from __future__ import annotations

import sys
import warnings

#: Форматы polydoc, которых в сборке нет (исключены через ``exclude_module``
#: в ``unifypy.json``, чтобы не тянуть лишние зависимости). CLI поддерживает
#: только DOCX/PPTX/PDF, но polydoc при старте предупреждает о каждом
#: отсутствующем формате — это ожидаемо и пользователю не нужно.
_EXCLUDED_FORMATS = "txt|markdown|html|json|csv|xlsx"
warnings.filterwarnings(
    "ignore",
    message=rf"polydoc: the '(?:{_EXCLUDED_FORMATS})' (?:reader|writer) is unavailable",
    category=RuntimeWarning,
)

from src.cli import main  # noqa: E402 - фильтр предупреждений должен стоять до импорта polydoc

if __name__ == "__main__":
    sys.exit(main())
