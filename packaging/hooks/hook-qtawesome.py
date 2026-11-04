"""PyInstaller hook для ``qtawesome``.

Шрифты и charmap-файлы иконок лежат в пакете данными, а ``qtawesome`` читает их с
диска при первом обращении к иконке. Без ``collect_data_files`` в замороженном
приложении иконки стартового экрана остались бы пустыми.

Включаются только наборы Font Awesome 5 (``fa5s``/``fa5r``/``fa5b``) — ими
пользуется интерфейс; остальные наборы (Material, Codicon, Elusive и т. п.)
весят ещё ~4.5 МБ и в бандл не попадают.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = collect_submodules("qtawesome")

datas = collect_data_files(
    "qtawesome",
    includes=["fonts/fontawesome5-*-webfont*.ttf", "fonts/fontawesome5-*-webfont*.json"],
)
