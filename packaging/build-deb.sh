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
# Имя spec берётся из "name" в unifypy.json (сейчас TermRu); старое имя тоже
# удаляем, чтобы не остался устаревший кеш после переименования.
rm -f "$PROJECT_ROOT/termru.spec" "$PROJECT_ROOT/TermRu.spec"

# strip имеет смысл только на Linux/macOS: в общем unifypy.json его нет,
# иначе Windows-сборка получает чужие флаги. Готовим временный конфиг.
STRIP_CONFIG="$(mktemp "${TMPDIR:-/tmp}/termru-unifypy-XXXXXX.json")"
trap 'rm -f "$STRIP_CONFIG"' EXIT
"$PYTHON" - "$STRIP_CONFIG" <<'PY'
import json
import sys
from pathlib import Path

config = json.loads(Path("unifypy.json").read_text(encoding="utf-8"))
config.setdefault("pyinstaller", {})["strip"] = True
Path(sys.argv[1]).write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
PY

echo "==> Сборка .deb (unifypy, strip=on)"
PATH="$PROJECT_ROOT/.venv/bin:$PATH" \
  "$PYTHON" -m unifypy . --config "$STRIP_CONFIG" --format deb

echo "==> Замена .desktop и иконки (unifypy их больше не ставит)"
for deb in "$PROJECT_ROOT"/installer/*.deb; do
  [[ -f "$deb" ]] || continue
  bash "$PROJECT_ROOT/packaging/fix-deb-desktop.sh" "$deb"
done

# dist/ остаётся распакованным бандлом: обрезаем его так же, как payload .deb,
# иначе артефакт в dist/ и в пакете расходятся по размеру и содержимому.
if [[ -d "$PROJECT_ROOT/dist/TermRu/_internal" ]]; then
  echo "==> Обрезка dist/TermRu"
  "$PYTHON" "$PROJECT_ROOT/packaging/trim_bundle.py" "$PROJECT_ROOT/dist/TermRu"
fi

echo "==> Артефакты"
mkdir -p "$PROJECT_ROOT/dist"
for deb in "$PROJECT_ROOT"/installer/*.deb; do
  [[ -f "$deb" ]] || continue
  # unifypy называет файл по "name" (TermRu_<version>_amd64.deb), а пакет и
  # привычное имя файла — termru: приводим к нижнему регистру.
  target="$PROJECT_ROOT/dist/$(basename "$deb" | sed 's/^TermRu_/termru_/')"
  cp -f "$deb" "$target"
done
ls -lh "$PROJECT_ROOT"/dist/*.deb

echo "==> Проверка размера (предел 100 МБ)"
for deb in "$PROJECT_ROOT"/dist/*.deb; do
  [[ -f "$deb" ]] || continue
  size_mb=$(( $(stat -c%s "$deb") / 1024 / 1024 ))
  echo "    $(basename "$deb"): ${size_mb} МиБ"
  if (( size_mb > 100 )); then
    echo "ОШИБКА: пакет больше 100 МБ (${size_mb} МиБ) — проверьте exclude_module" >&2
    exit 1
  fi
done
