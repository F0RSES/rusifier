#!/usr/bin/env bash
#
# Пост-обработка .deb: наш .desktop, иконка и CLI-алиас.
#
# Зачем: unifypy ставит собственный .desktop (Exec=/usr/local/bin/termru), а при
# create_desktop_file=false не кладёт и иконку. Кроме того, сборка теперь
# GUI-первая: главный файл пакета — /opt/termru/TermRu, поэтому .desktop должен
# запускать именно его (Terminal=false), а для терминала добавляется
# /usr/local/bin/termru-cli.
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
APP_BINARY="TermRu"

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
sed -i "s|^Exec=.*|Exec=${APP_DIR}/${APP_BINARY}|" \
  "$WORK/usr/share/applications/termru.desktop"

# Иконка: и в hicolor (стандарт), и в pixmaps (как делал unifypy).
install -Dm644 "$ICON_SRC" "$WORK/usr/share/icons/hicolor/256x256/apps/termru.png"
install -Dm644 "$ICON_SRC" "$WORK/usr/share/pixmaps/termru.png"

# Обрезка бандла: PyInstaller тянет все Qt-плагины, включая GTK-тему (~26 МБ
# зависимостей) и плагины SQL/QML/TLS, которые редактору не нужны.
TRIM_PYTHON="${TRIM_PYTHON:-$(command -v python3 || echo "${PYTHON:-}")}"
if [[ -d "$WORK/opt/termru/_internal" && -n "$TRIM_PYTHON" ]]; then
  echo "==> Обрезка бандла (лишние Qt-плагины и библиотеки)"
  "$TRIM_PYTHON" "$SCRIPT_DIR/trim_bundle.py" "$WORK/opt/termru"
fi

# CLI-алиас: тот же бинарник, но аргументы включают консольный режим
# (termru_gui_entry.py: без аргументов — GUI, с аргументами — CLI).
CLI_LAUNCHER="$WORK/usr/local/bin/termru-cli"
install -d "$(dirname "$CLI_LAUNCHER")"
cat > "$CLI_LAUNCHER" <<LAUNCHER
#!/bin/bash
export LD_LIBRARY_PATH="${APP_DIR}/_internal:\${LD_LIBRARY_PATH}"
cd ${APP_DIR} || exit 1
exec "./${APP_BINARY}" "\$@"
LAUNCHER
chmod 755 "$CLI_LAUNCHER"

# Сжатие: zstd (по умолчанию в dpkg) даёт ~101 МиБ, xz -9 — ~94 МиБ при тех же
# файлах. Упаковка идёт дольше, но для установщика это разовая плата.
dpkg-deb --root-owner-group -Zxz -z9 --build "$WORK" "$OUT_TMP" >/dev/null
mv -f "$OUT_TMP" "$DEB_OUT"

echo "Готово: $DEB_OUT (Exec=${APP_DIR}/${APP_BINARY}, CLI=/usr/local/bin/termru-cli)"

