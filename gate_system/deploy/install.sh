#!/usr/bin/env bash
# =============================================================================
#  Установка gate_system на Raspberry Pi 5 (Raspberry Pi OS 64-bit).
#
#  Запуск (на Pi, из папки проекта, ОБЫЧНЫМ пользователем, не root):
#      bash deploy/install.sh
#
#  Что делает:
#   1. ставит системные пакеты (apt);
#   2. ставит Python 3.12 через uv (в Pi OS может быть 3.11 или 3.13, а
#      Nomeroff-Net работает только на 3.11–3.12) и создаёт .venv;
#   3. ставит Python-зависимости из requirements.txt;
#   4. добавляет пользователя в группу gpio (управление реле);
#   5. заранее скачивает и проверяет модели распознавания;
#   6. ставит systemd-сервис с автозапуском при включении Pi;
#   7. включает аппаратный watchdog (Pi перезагрузится сам, если ОС зависнет)
#      и ограничивает размер системного журнала;
#   8. ставит Raspberry Pi Connect — удалённый терминал через браузер
#      (пропустить: GATE_NO_CONNECT=1 bash deploy/install.sh).
#
#  Скрипт можно запускать повторно (например, после обновления кода) — он
#  ничего не ломает и доустанавливает недостающее.
# =============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PY_VERSION="3.12"
SERVICE_NAME="gate_system"
TZ_DEFAULT="${GATE_TZ:-Europe/Moscow}"

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[!] %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m[x] %s\033[0m\n' "$*"; exit 1; }

[[ "$(id -u)" -ne 0 ]] || die "Запустите БЕЗ sudo: bash deploy/install.sh (sudo скрипт попросит сам)"
[[ "$(uname -m)" == "aarch64" ]] || warn "Это не 64-битный ARM ($(uname -m)). Скрипт рассчитан на Raspberry Pi OS 64-bit."
cd "$PROJECT_DIR"
say "Проект: $PROJECT_DIR, пользователь: $RUN_USER"

# --------------------------------------------------------------------------- 1
say "1/8 Системные пакеты"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl git build-essential python3-dev sqlite3 ffmpeg libgl1
# Имена некоторых пакетов отличаются между версиями Pi OS — ставим по отдельности,
# отсутствие любого из них не критично.
for pkg in libglib2.0-0t64 libglib2.0-0 libturbojpeg0 libopenblas0 liblgpio1 swig; do
    sudo apt-get install -y --no-install-recommends "$pkg" >/dev/null 2>&1 \
        && echo "  + $pkg" || echo "  - $pkg (нет в этой версии ОС — пропускаю)"
done

# Часовой пояс: время в Telegram и журнале должно быть местным.
CUR_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || echo unknown)"
if [[ "$CUR_TZ" == "Etc/UTC" || "$CUR_TZ" == "UTC" || "$CUR_TZ" == "unknown" ]]; then
    sudo timedatectl set-timezone "$TZ_DEFAULT" && echo "  часовой пояс: $TZ_DEFAULT"
else
    echo "  часовой пояс: $CUR_TZ (оставляю как есть)"
fi

# --------------------------------------------------------------------------- 2
say "2/8 Python $PY_VERSION (через uv) и виртуальное окружение .venv"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install "$PY_VERSION"
if [[ ! -x .venv/bin/python ]] || ! .venv/bin/python -c "import sys; assert sys.version_info[:2]==(3,12)" 2>/dev/null; then
    rm -rf .venv
    uv venv --python "$PY_VERSION" .venv
fi
.venv/bin/python --version

# --------------------------------------------------------------------------- 3
say "3/8 Python-зависимости (первый раз ~5–15 минут: torch и модели большие)"
PY=.venv/bin/python
TORCH_CPU_INDEX="https://download.pytorch.org/whl/cpu"
# PyTorch ставим ТОЛЬКО CPU-сборку: torch с PyPI на Linux тянет CUDA-библиотеки
# NVIDIA (несколько ГБ), на Raspberry Pi они бесполезны и занимают SD-карту.
TORCH_PIN="$(grep -E '^torch==' requirements.txt | cut -d' ' -f1)"
VISION_PIN="$(grep -E '^torchvision==' requirements.txt | cut -d' ' -f1)"
if ! uv pip install --python "$PY" --index-url "$TORCH_CPU_INDEX" "$TORCH_PIN" "$VISION_PIN"; then
    warn "Точной версии ($TORCH_PIN) в CPU-сборке нет — ставлю последнюю CPU-сборку PyTorch"
    uv pip install --python "$PY" --index-url "$TORCH_CPU_INDEX" torch torchvision
fi
# Остальное — без torch/torchvision (чтобы не заменить CPU-сборку на CUDA с PyPI).
grep -v -E '^(torch|torchvision)==' requirements.txt > /tmp/gate-requirements-pi.txt
uv pip install --python "$PY" -r /tmp/gate-requirements-pi.txt
if uv pip list --python "$PY" 2>/dev/null | grep -qiE '^(nvidia-|cuda-toolkit)'; then
    warn "В окружение попали CUDA-пакеты (не нужны на Pi). Удаляю…"
    uv pip list --python "$PY" | awk 'tolower($1) ~ /^(nvidia-|cuda-toolkit|cuda-bindings)/ {print $1}' \
        | xargs -r uv pip uninstall --python "$PY"
fi
"$PY" -c "import torch, cv2; print('  torch', torch.__version__, '| opencv', cv2.__version__)"
# Проверяем, что распознаватель импортируется (иначе ошибка всплыла бы только при загрузке моделей).
"$PY" -c "import nomeroff_net" 2>/tmp/gate-nomeroff-import.txt \
    || { tail -5 /tmp/gate-nomeroff-import.txt; die "Nomeroff-Net не импортируется — пришлите эти строки"; }
echo "  nomeroff-net импортируется"

# --------------------------------------------------------------------------- 4
say "4/8 Доступ к GPIO (реле)"
if getent group gpio >/dev/null; then
    sudo usermod -aG gpio,video "$RUN_USER"
    echo "  $RUN_USER добавлен в группы gpio, video"
else
    warn "Группы gpio нет — реле, возможно, потребует запуска от root"
fi

# --------------------------------------------------------------------------- 5
say "5/8 Папки, модели, конфиг"
mkdir -p data logs photos
[[ -f models/best.pt ]] || die "Нет models/best.pt (она лежит в git-репозитории) — проверьте, что проект скопирован целиком"
if [[ ! -f config/config.json ]]; then
    cp config/config.example.json config/config.json
    chmod 600 config/config.json
    warn "Создан config/config.json из примера — заполните его (nano config/config.json)"
fi
chmod 600 config/config.json   # в конфиге токен бота и пароли камер — читать может только владелец
.venv/bin/python tools/download_models.py || warn "Модели не загрузились — запустите позже: .venv/bin/python tools/download_models.py"

# --------------------------------------------------------------------------- 6
say "6/8 Автозапуск (systemd)"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$PROJECT_DIR|g" \
    deploy/gate_system.service | sudo tee /etc/systemd/system/$SERVICE_NAME.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable $SERVICE_NAME >/dev/null
echo "  сервис $SERVICE_NAME включён (стартует при загрузке Pi)"

# --------------------------------------------------------------------------- 7
say "7/8 Надёжность 24/7: аппаратный watchdog и размер журнала"
sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/journald.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' \
    | sudo tee /etc/systemd/system.conf.d/gate-watchdog.conf >/dev/null
printf '[Journal]\nSystemMaxUse=100M\n' \
    | sudo tee /etc/systemd/journald.conf.d/gate-journal.conf >/dev/null
sudo systemctl daemon-reexec
sudo systemctl restart systemd-journald
echo "  если ОС зависнет — Pi перезагрузится сам; журнал ограничен 100 МБ"

# --------------------------------------------------------------------------- 8
say "8/8 Удалённый доступ: Raspberry Pi Connect"
if [[ "${GATE_NO_CONNECT:-0}" == "1" ]]; then
    echo "  пропущено (GATE_NO_CONNECT=1)"
elif sudo apt-get install -y rpi-connect-lite >/dev/null 2>&1; then
    # linger: пользовательские службы (в т.ч. Connect) работают сразу после загрузки,
    # без входа в систему — важно после отключения электричества.
    sudo loginctl enable-linger "$RUN_USER"
    rpi-connect on >/dev/null 2>&1 || true
    echo "  установлен. Привязка к аккаунту — один раз командой: rpi-connect signin"
else
    warn "rpi-connect-lite не установился (нет в репозитории этой ОС?) — удалённый доступ настройте позже"
fi

# Подсказки по незаполненному конфигу.
grep -q "ТОКЕН_ОТ_BOTFATHER" config/config.json && warn "В config.json не задан telegram.token"
grep -q '"owner_id": 0' config/config.json && warn "В config.json не задан telegram.owner_id"
grep -q "ПАРОЛЬ@" config/config.json && warn "В config.json не заданы адреса/пароли камер"

cat <<EOF

===============================================================================
 Установка завершена.

 Дальше:
  1. Пропишите камеры и Telegram:   nano $PROJECT_DIR/config/config.json
  2. Проверьте реле (щёлкнет):       .venv/bin/python tools/relay_check.py
     и включите его в конфиге:      "relay": { "enabled": true, ... }
  3. Запуск:                          sudo systemctl start $SERVICE_NAME
  4. Живой журнал:                    journalctl -u $SERVICE_NAME -f
  5. Перезапуск / остановка:          sudo systemctl restart|stop $SERVICE_NAME
  6. Удалённый доступ (один раз):     rpi-connect signin  → откройте ссылку и войдите
                                      в Raspberry Pi ID; дальше: connect.raspberrypi.com
  7. Обновление кода в будущем:       bash deploy/update.sh

 ВАЖНО: чтобы права на GPIO применились, один раз перезагрузите Pi: sudo reboot
===============================================================================
EOF
