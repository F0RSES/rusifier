"""Уменьшение собранного бандла: лишние Qt-плагины и их зависимости.

PyInstaller собирает все Qt-плагины, которые находит хук PySide6, вместе с их
системными зависимостями. Плагин системной GTK-темы (``platformthemes/libqgtk3``)
тянет ~26 МБ (gtk3, gdk-pixbuf, glycin, gnutls, p11-kit, …), а приложение рисует
себя само через qdarkstyle и GTK не использует. Туда же уходят плагины SQL,
QML-отладки, TLS и EGLFS, которых в настольном редакторе нет.

Скрипт делает две вещи:

1. удаляет ненужные каталоги плагинов и библиотеки embedded-платформ Qt;
2. считает по ``DT_NEEDED`` (``readelf -d``), какие библиотеки верхнего уровня
   ещё кому-то нужны, и удаляет осиротевшие — те, что требовались только
   удалённым плагинам.

Запуск::

    python packaging/trim_bundle.py dist/TermRu

Каталог бандла меняется на месте; печатается отчёт об освобождённом месте.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

#: Каталоги плагинов Qt, которые приложению не нужны.
PLUGINS_TO_REMOVE = (
    "platformthemes",  # GTK-тема: тянет gtk3/glycin/gnutls (~26 МБ)
    "sqldrivers",  # QtSql не используется
    "qmltooling",  # отладка QML
    "egldeviceintegrations",  # embedded EGLFS
    "generic",  # qtuiotouch
    "networkinformation",  # bearer-менеджмент QtNetwork
    "tls",  # TLS QtNetwork: сеть живёт на стороне Python
    "designer",  # Qt Designer
)

#: Библиотеки Qt, нужные только embedded-платформам.
LIBS_TO_REMOVE = (
    "libQt6EglFSDeviceIntegration.so*",
    "libQt6EglFsKmsSupport.so*",
)

#: Каталоги внутри ``PySide6/Qt``, которые приложению не нужны.
#: ``translations`` — файлы локализации Qt: интерфейс TermRu написан по-русски
#: и ``QTranslator`` не устанавливает, поэтому .qm-файлы (7 МБ) не читаются.
QT_DIRS_TO_REMOVE = ("translations",)

#: Что нельзя удалять, даже если на него нет ``DT_NEEDED``: bootloader
#: PyInstaller загружает интерпретатор Python сам, по пути, вычисленному
#: во время запуска.
ALWAYS_KEEP_PREFIXES = ("libpython",)

#: Строка ``NEEDED`` в выводе ``readelf -d``.
_NEEDED_RE = re.compile(r"\(NEEDED\).*\[(?P<name>[^\]]+)\]")

#: Шаблоны имён разделяемых библиотек верхнего уровня.
_LIB_GLOBS = ("lib*.so", "lib*.so.*")


def _size(path: Path) -> int:
    """Размер файла или каталога в байтах.

    Args:
        path: Файл или каталог.

    Returns:
        int: суммарный размер в байтах (0, если пути нет).

    Raises:
        Ничего не бросает.
    """
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def _needed_libraries(path: Path) -> list[str]:
    """Прочитать список ``DT_NEEDED`` из заголовка ELF.

    Args:
        path: Файл ELF (библиотека, плагин или расширение Python).

    Returns:
        list[str]: имена зависимых библиотек; пустой список, если файл не ELF
        или ``readelf`` недоступен.

    Raises:
        Ничего не бросает: ошибки чтения считаются отсутствием зависимостей.
    """
    try:
        result = subprocess.run(
            ["readelf", "-d", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return [match.group("name") for match in _NEEDED_RE.finditer(result.stdout)]


def _remove_paths(paths: list[Path], root: Path) -> int:
    """Удалить файлы и каталоги, вернув освобождённое место.

    Args:
        paths: Что удалять.
        root: Каталог бандла (для сообщений об ошибках).

    Returns:
        int: сколько байт освобождено.

    Raises:
        Ничего не бросает: ошибки файловой системы пропускаются.
    """
    freed = 0
    for path in paths:
        if not path.exists():
            continue
        size = _size(path)
        try:
            if path.is_dir():
                for item in sorted(path.rglob("*"), reverse=True):
                    if item.is_file():
                        item.unlink(missing_ok=True)
                    else:
                        item.rmdir()
                path.rmdir()
            else:
                path.unlink()
        except OSError as exc:
            print(f"    пропущено {path.relative_to(root)}: {exc}")
            continue
        freed += size
    return freed


def _library_index(root: Path) -> dict[str, Path]:
    """Построить индекс ``имя библиотеки -> путь`` по всему бандлу.

    Args:
        root: Каталог бандла (``_internal``).

    Returns:
        dict[str, Path]: карта по имени файла.

    Raises:
        Ничего не бросает.
    """
    index: dict[str, Path] = {}
    for item in root.rglob("*"):
        if item.is_file() and ".so" in item.name:
            index.setdefault(item.name, item)
    return index


def _reachable_libraries(
    seeds: list[Path], index: dict[str, Path]
) -> set[str]:
    """Замкнуть множество библиотек бандла, которые кому-то нужны.

    Args:
        seeds: Файлы, с которых начинается обход (все ELF бандла, кроме
            библиотек верхнего уровня).
        index: Результат :func:`_library_index`.

    Returns:
        set[str]: имена библиотек бандла, достижимых по ``DT_NEEDED``.

    Raises:
        Ничего не бросает.
    """
    needed: set[str] = set()
    queue = list(seeds)
    seen: set[Path] = set()
    while queue:
        current = queue.pop()
        if current in seen:
            continue
        seen.add(current)
        for soname in _needed_libraries(current):
            target = index.get(soname)
            if target is None or soname in needed:
                continue
            needed.add(soname)
            queue.append(target)
    return needed


def trim_bundle(bundle_dir: str | Path) -> int:
    """Удалить лишние плагины и осиротевшие библиотеки бандла.

    Args:
        bundle_dir: Каталог собранного бандла (например, ``dist/TermRu``).

    Returns:
        int: сколько байт освобождено.

    Raises:
        FileNotFoundError: в каталоге нет ``_internal``.
    """
    root = Path(bundle_dir).resolve()
    internal = root / "_internal"
    if not internal.is_dir():
        raise FileNotFoundError(f"нет каталога _internal в {root}")

    before = _size(root)
    freed = 0

    qt_dir = internal / "PySide6" / "Qt"
    plugin_root = qt_dir / "plugins"
    if plugin_root.is_dir():
        freed += _remove_paths(
            [plugin_root / name for name in PLUGINS_TO_REMOVE], root
        )
        if plugin_root.is_dir() and not any(plugin_root.iterdir()):
            plugin_root.rmdir()

    library_dir = qt_dir / "lib"
    if library_dir.is_dir():
        freed += _remove_paths(
            [
                path
                for pattern in LIBS_TO_REMOVE
                for path in library_dir.glob(pattern)
            ],
            root,
        )

    freed += _remove_paths(
        [qt_dir / name for name in QT_DIRS_TO_REMOVE if (qt_dir / name).is_dir()],
        root,
    )

    # Осиротевшие библиотеки: обход по уже оставшимся файлам. Без readelf
    # зависимость не определить — тогда этот шаг пропускается целиком, иначе
    # можно удалить нужное.
    index = _library_index(internal)
    top_level = {
        path
        for pattern in _LIB_GLOBS
        for path in internal.glob(pattern)
        if path.is_file()
    }
    seeds = [
        item
        for item in internal.rglob("*")
        if item.is_file() and item not in top_level and ".so" in item.name
    ]
    launcher = root / root.name  # bootloader PyInstaller с именем приложения
    if launcher.is_file():
        seeds.append(launcher)
        index.setdefault(launcher.name, launcher)

    needed = _reachable_libraries(seeds, index)
    if not needed:
        print("    readelf недоступен: осиротевшие библиотеки не удалялись")
        orphans: list[Path] = []
    else:
        orphans = sorted(
            path
            for path in top_level
            if path.name not in needed
            and not any(path.name.startswith(prefix) for prefix in ALWAYS_KEEP_PREFIXES)
        )
    freed += _remove_paths(orphans, root)

    after = _size(root)
    print(
        f"    бандл: {before / 1024 / 1024:.1f} МиБ -> {after / 1024 / 1024:.1f} МиБ "
        f"(освобождено {freed / 1024 / 1024:.1f} МиБ, "
        f"плагинов: {len(PLUGINS_TO_REMOVE)}, осиротевших библиотек: {len(orphans)})"
    )
    return freed


def main(argv: list[str] | None = None) -> int:
    """Точка входа скрипта.

    Args:
        argv: Аргументы: путь к каталогу бандла.

    Returns:
        int: 0 при успехе, 1 — нет _internal, 2 — неверные аргументы.

    Raises:
        Ничего не бросает: ошибки печатаются в stderr.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: trim_bundle.py <bundle-dir>", file=sys.stderr)
        return 2
    try:
        trim_bundle(arguments[0])
    except FileNotFoundError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
