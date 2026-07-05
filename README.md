# VetRadiometry Weekly Digest

Автономный бот, который каждый понедельник в 04:00 MSK собирает свежие
научные публикации по ветеринарной кардиологии (собаки/кошки) и медицинскому
машинному обучению, переводит абстракты на русский через Perplexity Sonar API
и присылает дайджест в Telegram.

**Владелец:** Дмитрий Олейников · Аахен, DE
**Проект-контекст:** VetRadiometry (YOLOv8 для рентгенограмм сердца)
**LLM:** только Perplexity Sonar API (API-кошелёк, не Computer-кредиты)

## Быстрая установка (Ubuntu 22.04/24.04, Debian 12)

```bash
# 1. Скачать бандл (замените URL на реальный релиз)
sudo curl -fsSL https://raw.githubusercontent.com/wolfberg-guard/vetdigest/main/install.sh -o /tmp/vetdigest-install.sh

# 2. Запустить установщик
sudo VETDIGEST_REPO_URL="https://github.com/wolfberg-guard/vetdigest.git" bash /tmp/vetdigest-install.sh

# 3. Вписать секреты (2 значения)
sudo nano /opt/vetdigest/.env

# 4. Стартовать таймер
sudo systemctl start vetdigest.timer

# 5. Пробный запуск сейчас (не ждать понедельника)
sudo -u vetdigest /opt/vetdigest/venv/bin/python /opt/vetdigest/vetdigest.py --dry-run
```

## Что делает бот

1. Каждый понедельник в 04:00 MSK через systemd-таймер запускается
   `vetdigest.py`.
2. Опрашивает **PubMed** (тематические запросы + фильтр по 19 приоритетным
   ветеринарным журналам), **bioRxiv**, **arXiv** — за последние 7 дней.
3. Фильтрует по видам: cats/dogs/canine/feline включает, horses/cattle/pigs
   исключает.
4. Для каждой оставшейся статьи делает один запрос к **Sonar API** (модель
   `sonar`, low search context — ~$0.006 за запрос): получает
   релевантность, тематический блок, полный русский перевод абстракта и
   краткий комментарий о применимости к VetRadiometry.
5. Отбирает `high` и `medium`, группирует по 9 темам, форматирует карточки
   с NLM-библиографией + ссылками (DOI, Unpaywall, PubMed).
6. Разбивает на сообщения ≤3900 символов и шлёт в Telegram напрямую через
   `https://api.telegram.org` (без Pipedream).

## Стоимость

- **Telegram:** бесплатно.
- **PubMed/CrossRef/bioRxiv/arXiv:** бесплатно.
- **Perplexity Sonar (`sonar`):** $1/1M input + $1/1M output + $5/1K searches
  (low context). При типичных 10–20 статьях/неделю:
  ≈ **$0.07–$0.13 за прогон**, **~$5 в год**.

## Файлы

```
/opt/vetdigest/
├── vetdigest.py              # основной скрипт
├── .env                      # секреты (chmod 600)
├── requirements.txt
├── venv/                     # python 3.10+
├── data/
│   ├── sent_papers.json      # дедуп по DOI/PMID
│   ├── stats.json            # счётчики прогонов
│   ├── digests/              # digest_YYYY-MM-DD.md
│   ├── payloads/             # pplx_YYYY-MM-DD.jsonl (сырые ответы Sonar)
│   └── logs/                 # ротация по неделям
└── systemd/                  # копии unit-файлов (оригиналы в /etc/systemd/system/)
```

## Команды администрирования

| Что нужно | Команда |
|-----------|---------|
| Запустить сейчас (реальный прогон) | `sudo systemctl start vetdigest.service` |
| Пробный сбор без Telegram | `sudo -u vetdigest /opt/vetdigest/venv/bin/python /opt/vetdigest/vetdigest.py --dry-run` |
| Смотреть live-лог | `sudo journalctl -u vetdigest.service -f` |
| Расписание таймера | `sudo systemctl list-timers vetdigest.timer` |
| Приостановить бота | `sudo systemctl stop vetdigest.timer && sudo systemctl disable vetdigest.timer` |
| Возобновить | `sudo systemctl enable --now vetdigest.timer` |
| Изменить секреты | `sudo nano /opt/vetdigest/.env && sudo chmod 600 /opt/vetdigest/.env` |
| Полное удаление | `sudo systemctl disable --now vetdigest.timer && sudo rm -rf /opt/vetdigest /etc/systemd/system/vetdigest.{service,timer} && sudo userdel vetdigest` |

## Расширение

- **Изменить расписание:** отредактировать `OnCalendar=` в
  `/etc/systemd/system/vetdigest.timer` + `sudo systemctl daemon-reload`
- **Добавить журнал:** дописать в `VET_JOURNALS` в `vetdigest.py`
- **Изменить окно:** запустить с `--window-days 14`
- **Отключить LLM (только сбор):** `--no-llm`

## Список приоритетных журналов

Ветеринарные (для мелких животных, первичный охват): J Vet Cardiol, JVIM,
J Feline Med Surg, JAVMA, AJVR, Vet Clin Pathol, Vet Pathol, Vet J,
Open Vet J, JAAHA, J Vet Diagn Invest, Vet Med Sci, Vet Comp Oncol,
J Vet Sci, Vet Sci, Front Vet Sci, BMC Vet Res, Vet Radiol Ultrasound,
Res Vet Sci.

Плюс тематические запросы захватывают также NEJM, Circulation, Circ Res,
JACC, Eur Heart J, Am J Cardiol, Pulm Circ, Nature, Cell, Cell Metab,
Biophys J, JAMA Cardiol, Med Image Anal, IEEE Trans Med Imaging,
Radiol AI, Nat Mach Intell — если их публикации подходят по ключевым
словам темы + вид животного.

## Безопасность

- `.env` — `chmod 600`, доступ только для пользователя `vetdigest`.
- systemd-юнит с `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes`.
- Никаких входящих портов — бот только исходящие HTTPS-запросы делает.

## Отладка

Если бот молчит по понедельникам:

```bash
# 1. Таймер активен?
sudo systemctl status vetdigest.timer

# 2. Последний прогон
sudo journalctl -u vetdigest.service -n 200 --no-pager

# 3. Лог самого скрипта
sudo tail -100 /opt/vetdigest/data/logs/run_$(date -u +%F).log

# 4. Проверить .env
sudo -u vetdigest bash -c 'cat /opt/vetdigest/.env | grep -v "^#"'

# 5. Ручной прогон в верб-режиме
sudo -u vetdigest /opt/vetdigest/venv/bin/python /opt/vetdigest/vetdigest.py
```

## Лицензия

Internal — Дмитрий Олейников. Не публиковать.
