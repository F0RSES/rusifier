"""PyInstaller hook для ``qdarkstyle``.

Стили qdarkstyle лежат в скомпилированных Qt-ресурсах (``darkstyle_rc`` и
``lightstyle_rc``), которые импортируются внутри ``load_stylesheet``. Хук
собирает все подмодули пакета (иначе в замороженном приложении нет ресурсов и
``QFile(':qdarkstyle/dark/darkstyle.qss')`` пуст) и файлы стилей — на случай
версий, читающих QSS с диска.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("qdarkstyle")

datas = collect_data_files(
    "qdarkstyle",
    includes=["dark/*.qss", "light/*.qss", "qss/*", "svg/*"],
)
