"""PyInstaller hook для ``pdf_edit_engine``.

Движок импортируется в ``src/document_processor.py`` лениво (``_load_pdf_engine``),
поэтому PyInstaller может не увидеть часть подмодулей. Хук собирает их все —
иначе PDF-ветка в собранном бинарнике падает на импорте.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("pdf_edit_engine")
