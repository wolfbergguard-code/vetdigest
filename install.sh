#!/usr/bin/env bash
# VetRadiometry Digest — установщик для Ubuntu 22.04 / 24.04
# Запускать с sudo. Идемпотентно — можно перезапускать безопасно.

set -euo pipefail

INSTALL_DIR=/opt/vetdigest
SERVICE_USER=vetdigest
REPO_URL="${VETDIGEST_REPO_URL:-}"    # можно переопределить переменной окружения
BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

msg() { printf "\033[1;36m[vetdigest]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[vetdigest ERROR]\033[0m %s\n" "$*" >&2; }
require_root() {
    if [[ $EUID -ne 0 ]]; then
        err "Запустите скрипт от root: sudo bash $0"
        exit 1
    fi
}

require_root

# --- 1. Проверка ОС ---------------------------------------------------------
if [[ ! -f /etc/os-release ]]; then
    err "Не удалось определить ОС (нет /etc/os-release)"
    exit 1
fi
. /etc/os-release
case "${ID}${VERSION_ID%%.*}" in
    ubuntu22|ubuntu23|ubuntu24|debian11|debian12)
        msg "Обнаружена ${PRETTY_NAME}"
        ;;
    *)
        msg "ОС: ${PRETTY_NAME} — не тестировалась, но попробуем"
        ;;
esac

# --- 2. Зависимости ---------------------------------------------------------
msg "Обновляю apt и ставлю зависимости..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl tzdata

# --- 3. Часовой пояс --------------------------------------------------------
current_tz=$(timedatectl show --value -p Timezone 2>/dev/null || echo "unknown")
if [[ "$current_tz" != "Europe/Moscow" ]]; then
    msg "Текущий TZ: $current_tz — оставляю как есть; таймер использует Europe/Moscow независимо."
fi

# --- 4. Пользователь --------------------------------------------------------
if id -u "$SERVICE_USER" >/dev/null 2>&1; then
    msg "Пользователь $SERVICE_USER уже существует"
else
    msg "Создаю системного пользователя $SERVICE_USER"
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

# --- 5. Директории ----------------------------------------------------------
msg "Создаю $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"/{data/logs,data/digests,data/payloads}

# --- 6. Копируем исходники --------------------------------------------------
if [[ -f "$BUNDLE_ROOT/vetdigest.py" ]]; then
    msg "Копирую vetdigest.py из локального бандла"
    cp "$BUNDLE_ROOT/vetdigest.py" "$INSTALL_DIR/vetdigest.py"
    cp "$BUNDLE_ROOT/requirements.txt" "$INSTALL_DIR/requirements.txt"
    if [[ ! -f "$INSTALL_DIR/.env" ]]; then
        cp "$BUNDLE_ROOT/.env.example" "$INSTALL_DIR/.env"
    fi
    cp "$BUNDLE_ROOT/systemd/vetdigest.service" /etc/systemd/system/vetdigest.service
    cp "$BUNDLE_ROOT/systemd/vetdigest.timer"   /etc/systemd/system/vetdigest.timer
elif [[ -n "$REPO_URL" ]]; then
    msg "Клонирую $REPO_URL"
    apt-get install -y -qq git
    tmp=$(mktemp -d)
    git clone --depth 1 "$REPO_URL" "$tmp"
    cp "$tmp/vetdigest.py" "$INSTALL_DIR/vetdigest.py"
    cp "$tmp/requirements.txt" "$INSTALL_DIR/requirements.txt"
    if [[ ! -f "$INSTALL_DIR/.env" ]]; then
        cp "$tmp/.env.example" "$INSTALL_DIR/.env"
    fi
    cp "$tmp/systemd/vetdigest.service" /etc/systemd/system/vetdigest.service
    cp "$tmp/systemd/vetdigest.timer"   /etc/systemd/system/vetdigest.timer
    rm -rf "$tmp"
else
    err "Не найден локальный vetdigest.py, и VETDIGEST_REPO_URL не задан."
    exit 1
fi

# --- 7. Виртуальное окружение ----------------------------------------------
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    msg "Создаю Python virtualenv"
    python3 -m venv "$INSTALL_DIR/venv"
fi

msg "Устанавливаю Python-пакеты"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# --- 8. Права --------------------------------------------------------------
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
chmod 600 "$INSTALL_DIR/.env"
chmod 755 "$INSTALL_DIR/vetdigest.py"

# --- 9. Активируем systemd -------------------------------------------------
msg "Активирую systemd-таймер"
systemctl daemon-reload
systemctl enable vetdigest.timer >/dev/null

# Стартовать таймер только если .env заполнен (проверяем на плейсхолдер)
if grep -q "pplx-XXX" "$INSTALL_DIR/.env"; then
    err "======================================================================"
    err "УСТАНОВКА ЗАВЕРШЕНА, НО ФАЙЛ .env НЕ ЗАПОЛНЕН."
    err ""
    err "Отредактируйте секреты:"
    err "   sudo nano $INSTALL_DIR/.env"
    err ""
    err "Затем запустите таймер:"
    err "   sudo systemctl start vetdigest.timer"
    err ""
    err "Проверьте статус:"
    err "   sudo systemctl list-timers vetdigest.timer"
    err ""
    err "Ручной пробный запуск (сразу, без ожидания понедельника):"
    err "   sudo -u vetdigest $INSTALL_DIR/venv/bin/python $INSTALL_DIR/vetdigest.py --dry-run"
    err "======================================================================"
    exit 0
fi

systemctl start vetdigest.timer
msg "✅ Таймер vetdigest.timer активирован"
systemctl list-timers vetdigest.timer --no-pager || true

msg ""
msg "Установка завершена."
msg ""
msg "Следующие шаги:"
msg "  1. Проверить .env:        sudo cat $INSTALL_DIR/.env"
msg "  2. Пробный сбор (без TG): sudo -u vetdigest $INSTALL_DIR/venv/bin/python $INSTALL_DIR/vetdigest.py --dry-run"
msg "  3. Реальный запуск:       sudo systemctl start vetdigest.service"
msg "  4. Смотреть логи:         sudo journalctl -u vetdigest.service -f"
msg "  5. Расписание таймера:    sudo systemctl list-timers vetdigest.timer"
