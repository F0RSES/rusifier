#!/usr/bin/env bash
#
# Сборка Debian/Ubuntu пакета (onedir + .deb).
#
# unifypy кеширует spec по merged_config и не учитывает `--format`/`--onefile`,
# поэтому перед сборкой spec удаляется: иначе после onefile-сборки (Arch)
# остаётся файл вместо каталога и deb-этап падает.
#
# Использование:
#   packaging/build-deb.sh
#   PYTHON=/usr/bin/python3.13 packaging/build-deb.sh

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON="${PYTHON:-$PROJECT_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

echo "==> Очистка кеша spec и прошлых артефактов"
rm -rf "$PROJECT_ROOT/build" "$PROJECT_ROOT/dist" "$PROJECT_ROOT/installer"
rm -f "$PROJECT_ROOT/termru.spec"

echo "==> Сборка .deb (unifypy)"
PATH="$PROJECT_ROOT/.venv/bin:$PATH" \
  "$PYTHON" -m unifypy . --config unifypy.json --format deb

echo "==> Замена .desktop и иконки (unifypy их больше не ставит)"
for deb in "$PROJECT_ROOT"/installer/*.deb; do
  [[ -f "$deb" ]] || continue
  bash "$PROJECT_ROOT/packaging/fix-deb-desktop.sh" "$deb"
done

echo "==> Артефакты"
if compgen -G "$PROJECT_ROOT/installer/*.deb" > /dev/null; then
  cp "$PROJECT_ROOT"/installer/*.deb "$PROJECT_ROOT/dist/"
fi
ls -lh "$PROJECT_ROOT/dist"
