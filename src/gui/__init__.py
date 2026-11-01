"""GUI TermRu (PySide6): редактор документов с русификацией.

Пакет не импортирует Qt на уровне модуля: тесты и CLI-часть TermRu работают
без графической библиотеки, пока GUI не запрошен явно.
"""

from __future__ import annotations

__all__ = ["MainWindow", "DocxEditor", "PptxEditor", "PdfEditor"]


def __getattr__(name: str) -> object:
    """Ленивая загрузка публичных классов (PEP 562).

    Args:
        name: Имя атрибута пакета.

    Returns:
        object: Класс ``MainWindow``, ``DocxEditor``, ``PptxEditor`` или ``PdfEditor``.

    Raises:
        AttributeError: для неизвестного имени.
    """
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name == "DocxEditor":
        from .docx_editor import DocxEditor

        return DocxEditor
    if name == "PptxEditor":
        from .pptx_editor import PptxEditor

        return PptxEditor
    if name == "PdfEditor":
        from .pdf_editor import PdfEditor

        return PdfEditor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
