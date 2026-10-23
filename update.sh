#!/usr/bin/env bash
# VetDigest — обновление существующей установки до v2 (аддитивно, с бэкапом).
# Запуск: sudo bash update.sh   (из папки со свежим кодом)
set -euo pipefail
D=/opt/vetdigest
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
msg(){ printf "\033[1;36m[vetdigest]\033[0m %s\n" "$*"; }
[[ $EUID -eq 0 ]] || { echo "Запустите через sudo"; exit 1; }
[[ -d $D ]] || { echo "Нет $D — сначала install.sh"; exit 1; }

TS=$(date +%Y%m%d-%H%M%S)
BK=$D/backup-$TS
msg "Бэкап → $BK"
mkdir -p "$BK"
cp -a $D/vetdigest.py $D/.env "$BK"/ 2>/dev/null || true
cp -a $D/data/sent_papers.json $D/data/stats.json "$BK"/ 2>/dev/null || true
cp -a /etc/systemd/system/vetdigest.service /etc/systemd/system/vetdigest.timer "$BK"/ 2>/dev/null || true

msg "Копирую код v2"
install -m 755 "$SRC/vetdigest.py" $D/vetdigest.py
install -m 755 "$SRC/vetdigest_feedback.py" $D/vetdigest_feedback.py
install -m 644 "$SRC/requirements.txt" $D/requirements.txt
install -m 644 "$SRC/.env.example" $D/.env.example
install -m 644 "$SRC/systemd/vetdigest.service" /etc/systemd/system/vetdigest.service
install -m 644 "$SRC/systemd/vetdigest.timer" /etc/systemd/system/vetdigest.timer
install -m 644 "$SRC/systemd/vetdigest-feedback.service" /etc/systemd/system/vetdigest-feedback.service

msg "Дописываю новые переменные в .env (существующие не трогаю)"
for kv in "SECONDARY_CHAT_ID=" "S2_API_KEY=" "NCBI_API_KEY=" \
          "VETDIGEST_MAX_LLM=50" "VETDIGEST_MAX_CARDS=15" "VETDIGEST_MIN_SCORE=6"; do
  k=${kv%%=*}
  grep -q "^$k=" $D/.env || echo "$kv" >> $D/.env
done
# v1-инструкция могла называть ключ PERPLEXITY_API_KEY — поддерживаются оба имени
sed -i 's/\r$//' $D/.env

"$D/venv/bin/pip" install --quiet -r $D/requirements.txt
chown -R vetdigest:vetdigest $D
chmod 600 $D/.env

systemctl daemon-reload
systemctl enable --now vetdigest.timer >/dev/null
systemctl enable vetdigest-feedback.service >/dev/null
systemctl restart vetdigest-feedback.service

msg "Проверка доступов:"
sudo -u vetdigest $D/venv/bin/python $D/vetdigest.py --check || true
msg "Готово. Следующий запуск:"
systemctl list-timers vetdigest.timer --no-pager | head -3
msg "Откат: sudo cp $BK/vetdigest.py $D/ && sudo systemctl disable --now vetdigest-feedback"
