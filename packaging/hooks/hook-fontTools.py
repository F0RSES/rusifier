"""PyInstaller hook для ``fontTools``.

``fontTools`` импортируется лениво (``from fontTools import subset`` внутри
``build_type0_font`` и ``TTFont`` внутри ``_probe_cyrillic``), а его подмодули
подгружаются по мере надобности — в замороженном приложении это даёт
``ModuleNotFoundError: fontTools.ttLib.tables...``. Хук собирает всё сразу.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("fontTools")
