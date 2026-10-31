#!/usr/bin/env bash
#
# Проверки состояния git-репозитория TermRu.
#
# Запуск: bash tests/test_git_repo.sh
#
# Не собирается pytest'ом намеренно: в CI тег v0.1.0-mvp может быть не получен
# (checkout без --tags), а в архиве без .git проверки неприменимы.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

pass=0
fail=0

ok()  { pass=$((pass + 1)); printf '  ok    %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf '  FAIL  %s\n' "$1"; }

if [[ "$(git rev-parse --is-inside-work-tree 2>/dev/null)" == "true" ]]; then
  ok "репозиторий существует (rev-parse --is-inside-work-tree = true)"
else
  bad "rev-parse --is-inside-work-tree не вернул true"
fi

if git ls-files | grep -qE '\.venv|\.deb$|\.tar\.gz$|\.spec$'; then
  bad "в индексе есть бинарники или артефакты сборки:"
  git ls-files | grep -E '\.venv|\.deb$|\.tar\.gz$|\.spec$' | sed 's/^/        /'
else
  ok "бинарников (.venv/.deb/.tar.gz/.spec) в индексе нет"
fi

if git ls-files | grep -qx '\.env'; then
  bad ".env попал в индекс"
else
  ok ".env не отслеживается"
fi

py_count="$(git ls-files | grep -cE '^src/.*\.py$' || true)"
if [[ "${py_count:-0}" -ge 5 ]]; then
  ok "исходников src/*.py: ${py_count} (>= 5)"
else
  bad "исходников src/*.py: ${py_count:-0} (< 5)"
fi

if git ls-files | grep -qx 'prompts/system_prompt.txt'; then
  ok "prompts/system_prompt.txt отслеживается"
else
  bad "prompts/system_prompt.txt не отслеживается"
fi

if [[ -n "$(git tag -l v0.1.0-mvp)" ]]; then
  ok "тег v0.1.0-mvp существует"
else
  bad "тег v0.1.0-mvp отсутствует"
fi

if [[ -z "$(git status --porcelain)" ]]; then
  ok "рабочее дерево чистое"
else
  bad "есть незакоммиченные изменения:"
  git status --porcelain | sed 's/^/        /'
fi

echo
echo "ИТОГ: $pass ok, $fail fail"
[[ "$fail" -eq 0 ]]
