#!/usr/bin/env bash
#
# Пост-обработка .deb: заменяет .desktop на наш и пересобирает пакет.
#
# Зачем: unifypy ставит собственный .desktop (Exec=/usr/local/bin/termru,
# Terminal=false) — клик по иконке запускает CLI без аргументов и окна не
# появляется. Кроме того, при create_desktop_file=false unifypy не кладёт и
# иконку (она копируется тем же методом), поэтому скрипт переустанавливает обе.
#
# Использование:
#   bash packaging/fix-deb-desktop.sh installer/termru_0.1.0_amd64.deb dist/termru_0.1.0_amd64.deb
#   bash packaging/fix-deb-desktop.sh dist/termru_0.1.0_amd64.deb   # перезапись на месте
#
# Скрипт идемпотентен: повторный запуск даёт тот же результат.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP_SRC="$SCRIPT_DIR/termru.desktop"
ICON_SRC="$SCRIPT_DIR/icon.png"
APP_DIR="/opt/termru"

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <input.deb> [output.deb]" >&2
  exit 2
fi

DEB_IN="$1"
DEB_OUT="${2:-$1}"

if [[ ! -f "$DEB_IN" ]]; then
  echo "ОШИБКА: файл не найден: $DEB_IN" >&2
  exit 1
fi
for required in "$DESKTOP_SRC" "$ICON_SRC"; do
  if [[ ! -f "$required" ]]; then
    echo "ОШИБКА: не найден обязательный файл: $required" >&2
    exit 1
  fi
done
if ! command -v dpkg-deb >/dev/null 2>&1; then
  echo "ОШИБКА: нужен dpkg-deb (пакет dpkg-dev)" >&2
  exit 1
fi

WORK="$(mktemp -d)"
# Собираем рядом с целевым файлом: внутри $WORK собирать нельзя — dpkg-deb
# упаковал бы архив в самого себя.
OUT_TMP="${DEB_OUT}.tmp.$$"
trap 'rm -rf "$WORK"; rm -f "$OUT_TMP"' EXIT

dpkg-deb -R "$DEB_IN" "$WORK"

# Ровно один .desktop, наш, с путём установки unifypy.
rm -f "$WORK"/usr/share/applications/*.desktop
install -Dm644 "$DESKTOP_SRC" "$WORK/usr/share/applications/termru.desktop"
sed -i "s|^Exec=.*|Exec=${APP_DIR}/termru --help|" \
  "$WORK/usr/share/applications/termru.desktop"

# Иконка: и в hicolor (стандарт), и в pixmaps (как делал unifypy).
install -Dm644 "$ICON_SRC" "$WORK/usr/share/icons/hicolor/256x256/apps/termru.png"
install -Dm644 "$ICON_SRC" "$WORK/usr/share/pixmaps/termru.png"

dpkg-deb --root-owner-group --build "$WORK" "$OUT_TMP" >/dev/null
mv -f "$OUT_TMP" "$DEB_OUT"

echo "Готово: $DEB_OUT (Exec=${APP_DIR}/termru --help)"
