#!/usr/bin/env bash
# =============================================================================
#  Обновление gate_system на Raspberry Pi из git-репозитория.
#
#  Запуск (на Pi, из папки проекта):   bash deploy/update.sh
#
#  1. забирает новый код (git pull);
#  2. если изменились зависимости или скрипт установки — запускает install.sh;
#  3. перезапускает сервис и показывает, что он поднялся.
#  Ваши данные (data/, logs/, photos/) и config/config.json не трогаются.
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

BEFORE="$(git rev-parse HEAD)"
echo "==> Получаю обновления…"
git pull --ff-only
AFTER="$(git rev-parse HEAD)"

if [[ "$BEFORE" == "$AFTER" ]]; then
    echo "Новых изменений нет."
else
    git --no-pager log --oneline "$BEFORE..$AFTER"
    if git diff --name-only "$BEFORE" "$AFTER" | grep -qE '(^|/)(requirements\.txt|deploy/install\.sh|deploy/gate_system\.service)$'; then
        echo "==> Изменились зависимости/установка — запускаю install.sh"
        bash deploy/install.sh
    fi
fi

echo "==> Перезапуск сервиса"
sudo systemctl restart gate_system
sleep 5
systemctl --no-pager --lines=15 status gate_system || true
