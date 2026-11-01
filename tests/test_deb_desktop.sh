#!/usr/bin/env bash
#
# Тесты пост-обработки .deb (packaging/fix-deb-desktop.sh).
#
# Фикстура — готовый .deb: сначала tests/fixtures/termru_*.deb, затем
# dist/termru_*.deb (собирается `packaging/build-deb.sh`). Если пакета нет
# (чистый клон без сборки), тест скипается с кодом 0 — 70 МБ в репозиторий
# не кладём.
#
# Запуск: bash tests/test_deb_desktop.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIX_SCRIPT="$ROOT/packaging/fix-deb-desktop.sh"
pass=0
fail=0

ok()  { pass=$((pass + 1)); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"; }

find_deb() {
  local candidate
  # Имя пакета берётся из "package" в unifypy.json (termru), а файл unifypy
  # называет по "name" — TermRu_<version>_amd64.deb. Пускаем оба варианта.
  for candidate in "$ROOT"/tests/fixtures/[Tt]erm[Rr]u_*.deb "$ROOT"/dist/[Tt]erm[Rr]u_*.deb; do
    if [[ -f "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

if ! DEB="$(find_deb)"; then
  echo "SKIP: .deb не найден. Соберите его: packaging/build-deb.sh"
  exit 0
fi
echo "Фикстура: $DEB"

if [[ ! -f "$FIX_SCRIPT" ]]; then
  echo "ОШИБКА: нет $FIX_SCRIPT" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
OUT="$WORK/fixed.deb"

count_desktop() {
  # Сколько .desktop лежит в пакете (0, если grep ничего не нашёл).
  dpkg-deb -c "$1" | grep -c '\.desktop$' || true
}

echo "== первый прогон =="
if bash "$FIX_SCRIPT" "$DEB" "$OUT" >"$WORK/fix1.log" 2>&1; then
  ok "скрипт завершился успешно"
else
  bad "скрипт упал:"; sed 's/^/        /' "$WORK/fix1.log"
fi
[[ -f "$OUT" ]] || { bad "выходной .deb не создан"; exit 1; }
ok "выходной .deb создан"

count="$(count_desktop "$OUT")"
[[ "$count" == "1" ]] && ok "ровно один .desktop ($count)" || bad "ожидался 1 .desktop, найдено: $count"

dpkg-deb -x "$OUT" "$WORK/extract"
DESKTOP="$WORK/extract/usr/share/applications/termru.desktop"
if [[ -f "$DESKTOP" ]]; then
  ok "termru.desktop на месте"
  if grep -qx 'Exec=/opt/termru/TermRu' "$DESKTOP"; then
    ok "Exec=/opt/termru/TermRu"
  else
    bad "Exec неверный: $(grep '^Exec=' "$DESKTOP" || echo '<нет>')"
  fi
  grep -qx 'Terminal=false' "$DESKTOP" && ok "Terminal=false" || bad "Terminal не false"
  grep -qx 'Icon=termru' "$DESKTOP" && ok "Icon=termru" || bad "Icon не termru"
else
  bad "termru.desktop отсутствует"
fi

[[ -f "$WORK/extract/usr/share/pixmaps/termru.png" ]] \
  && ok "иконка в /usr/share/pixmaps" || bad "иконки в pixmaps нет"
[[ -f "$WORK/extract/usr/share/icons/hicolor/256x256/apps/termru.png" ]] \
  && ok "иконка в hicolor" || bad "иконки в hicolor нет"
[[ -x "$WORK/extract/opt/termru/TermRu" ]] \
  && ok "GUI-бинарник на месте и исполняемый" || bad "бинарника TermRu нет"
[[ -x "$WORK/extract/usr/local/bin/termru-cli" ]] \
  && ok "CLI-алиас /usr/local/bin/termru-cli" || bad "termru-cli нет"

echo "== идемпотентность =="
OUT2="$WORK/fixed2.deb"
if bash "$FIX_SCRIPT" "$OUT" "$OUT2" >"$WORK/fix2.log" 2>&1; then
  ok "повторный прогон успешен"
else
  bad "повторный прогон упал:"; sed 's/^/        /' "$WORK/fix2.log"
fi
count2="$(count_desktop "$OUT2")"
[[ "$count2" == "1" ]] && ok "после повтора один .desktop" || bad "после повтора .desktop: $count2"
grep -qx 'Exec=/opt/termru/TermRu' <(dpkg-deb --fsys-tarfile "$OUT2" \
  | tar -xO ./usr/share/applications/termru.desktop) \
  && ok "Exec не изменился после повтора" || bad "Exec изменился после повтора"

echo "== перезапись на месте (без второго аргумента) =="
cp "$DEB" "$WORK/inplace.deb"
if bash "$FIX_SCRIPT" "$WORK/inplace.deb" >"$WORK/fix3.log" 2>&1; then
  ok "запуск без output-аргумента успешен"
else
  bad "запуск без output-аргумента упал:"; sed 's/^/        /' "$WORK/fix3.log"
fi
count3="$(count_desktop "$WORK/inplace.deb")"
[[ "$count3" == "1" ]] && ok "на месте тоже один .desktop" || bad "на месте .desktop: $count3"

echo
echo "ИТОГ: $pass ok, $fail fail"
[[ "$fail" -eq 0 ]]
