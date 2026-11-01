"""Согласованность версии в исходниках и в собранных артефактах.

Тесты артефактов пропускаются, если сборки нет: `pytest` в чистом клоне не
требует наличия `.deb` и бинарника.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Версия релиза; при бампе меняется здесь и во всех манифестах.
EXPECTED_VERSION = "0.2.0"

#: Собранный бинарник: onedir (`dist/termru/termru`) или onefile (`dist/termru`).
_BINARY_CANDIDATES = (
    PROJECT_ROOT / "dist" / "TermRu" / "TermRu",
    PROJECT_ROOT / "dist" / "TermRu",
    PROJECT_ROOT / "dist" / "termru" / "termru",
    PROJECT_ROOT / "dist" / "termru",
)


def _read(path: Path) -> str:
    """Прочитать текстовый файл проекта."""
    return path.read_text(encoding="utf-8")


def _binary() -> Path | None:
    """Найти собранный бинарник, если он есть."""
    for candidate in _BINARY_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def test_version_consistency() -> None:
    """Версия совпадает в src, unifypy.json, PKGBUILD и README."""
    init_text = _read(PROJECT_ROOT / "src" / "__init__.py")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', init_text)
    assert match is not None, "__version__ не найден в src/__init__.py"
    assert match.group(1) == EXPECTED_VERSION

    config = json.loads(_read(PROJECT_ROOT / "unifypy.json"))
    assert config["version"] == EXPECTED_VERSION, config["version"]

    pkgbuild = _read(PROJECT_ROOT / "packaging" / "PKGBUILD")
    pkgver = re.search(r"^pkgver=(\S+)$", pkgbuild, re.MULTILINE)
    assert pkgver is not None, "pkgver не найден в PKGBUILD"
    assert pkgver.group(1) == EXPECTED_VERSION

    readme = _read(PROJECT_ROOT / "README.md")
    assert f"termru_{EXPECTED_VERSION}_amd64.deb" in readme
    assert f"<{EXPECTED_VERSION}>" not in readme, "остался плейсхолдер версии"


def test_strip_is_linux_only() -> None:
    """``strip`` не живёт в общем конфиге: Windows-сборка падала из-за него.

    Флаг включается только в ``packaging/build-deb.sh`` через временный конфиг.
    """
    config = json.loads(_read(PROJECT_ROOT / "unifypy.json"))
    assert "strip" not in config["pyinstaller"], (
        "strip в unifypy.json сломает сборку .exe на Windows"
    )

    script = _read(PROJECT_ROOT / "packaging" / "build-deb.sh")
    assert '["strip"] = True' in script, "build-deb.sh должен включать strip для Linux"


def test_deb_version() -> None:
    """Собранный .deb несёт ту же версию в имени файла и в метаданных."""
    debs = sorted((PROJECT_ROOT / "dist").glob("[Tt]erm[Rr]u_*.deb"))
    if not debs:
        pytest.skip("нет собранного .deb (нужен packaging/build-deb.sh)")

    deb = debs[-1]
    assert EXPECTED_VERSION in deb.name, deb.name

    info = subprocess.run(
        ["dpkg-deb", "--info", str(deb)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert info.returncode == 0, info.stderr[-2000:]
    assert f"Version: {EXPECTED_VERSION}" in info.stdout, info.stdout[:2000]


def test_cli_version_flag() -> None:
    """Собранный бинарник отвечает на --version актуальной версией."""
    binary = _binary()
    if binary is None:
        pytest.skip("нет собранного бинарника (нужен packaging/build-deb.sh)")

    completed = subprocess.run(
        [str(binary), "--version"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr[-2000:]
    assert EXPECTED_VERSION in completed.stdout, completed.stdout
