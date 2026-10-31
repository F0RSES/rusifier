#!/usr/bin/env bash
#
# Сборка пакета для Arch Linux (termru-bin).
#
# Шаги:
#   1. собрать onefile-бинарник через unifypy (dist/termru);
#   2. положить рядом termru.desktop и icon.png;
#   3. упаковать в packaging/arch/termru-<version>.tar.gz;
#   4. скопировать PKGBUILD;
#   5. напечатать команду для makepkg.
#
# Использование:
#   packaging/build-arch.sh              # собрать бинарник и архив
#   SKIP_BUILD=1 packaging/build-arch.sh # взять готовый dist/termru
#
# Требования: bash, tar, python из окружения проекта (или PYTHON=...).

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

VERSION="$("$PYTHON" -c 'import json; print(json.load(open("unifypy.json"))["version"])')"
OUT_DIR="$PROJECT_ROOT/packaging/arch"
STAGE_DIR="$OUT_DIR/stage"
BINARY="$PROJECT_ROOT/dist/termru"

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  echo "==> Сборка onefile-бинарника (unifypy)"
  # unifypy кеширует termru.spec по merged_config и игнорирует CLI-оверрайды:
  # без удаления spec сборка молча останется onedir (каталог вместо файла).
  rm -f "$PROJECT_ROOT/termru.spec"
  PATH="$PROJECT_ROOT/.venv/bin:$PATH" \
    "$PYTHON" -m unifypy . --config unifypy.json --onefile --skip-installer
fi

if [[ ! -f "$BINARY" ]]; then
  echo "ОШИБКА: не найден onefile-бинарник $BINARY" >&2
  echo "Соберите его: $PYTHON -m unifypy . --config unifypy.json --onefile --skip-installer" >&2
  exit 1
fi

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
install -m755 "$BINARY" "$STAGE_DIR/termru"
install -m644 "$PROJECT_ROOT/packaging/termru.desktop" "$STAGE_DIR/termru.desktop"
install -m644 "$PROJECT_ROOT/packaging/icon.png" "$STAGE_DIR/icon.png"

TARBALL="$OUT_DIR/termru-${VERSION}.tar.gz"
tar -czf "$TARBALL" -C "$STAGE_DIR" termru termru.desktop icon.png
install -m644 "$PROJECT_ROOT/packaging/PKGBUILD" "$OUT_DIR/PKGBUILD"
rm -rf "$STAGE_DIR"

echo "==> Готово: $TARBALL"
echo "    Установка: cd packaging/arch && makepkg -si"
