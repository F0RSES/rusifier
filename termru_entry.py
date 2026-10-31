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

from src.cli import main

if __name__ == "__main__":
    sys.exit(main())
