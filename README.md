# VetDigest v2

Еженедельный научный дайджест (понедельник, 04:00 МСК) для ветеринарного кардиолога.

## Что ищет
Три оси: 🫀 ветеринарная кардиология · 🤖 внедрение ИИ/ML · 🔬 патофизиологические эксперименты
(крупные животные модели, ex vivo / in vivo, in silico).

Источники: **PubMed** (14 тематических запросов + фильтр `[Journal]` по 34 приоритетным журналам)
и **Semantic Scholar** (5 bulk-запросов). Записи сливаются по DOI/PMID/названию.

Журналы:
- ветеринарные (статьи по сердцу/ИИ; J Vet Cardiol — целиком): J Vet Cardiol, JVIM, JFMS, JAVMA, AJVR,
  Vet Clin Pathol, Vet Pathol, Vet J, Open Vet J, JAAHA, J Vet Diagn Invest, Vet Med Sci, Vet Comp Oncol,
  J Vet Sci, Vet Sci, Front Vet Sci, BMC Vet Res, Vet Radiol Ultrasound, Res Vet Sci;
- кардиология человека (ИИ / эксперимент / модели; Circ Res — целиком): NEJM, Am J Cardiol, Circulation,
  Circ Res, Pulm Circ, Circ Heart Fail, Circ Arrhythm Electrophysiol, Circ Cardiovasc Imaging, JAHA, Hypertension;
- фундаментальные (только сердце): Nature, Cell, Cell Metab, Cell Rep Med, Biophys J.

## Как отбирает
Эвристический фильтр → до 50 статей в Perplexity Sonar (`disable_search`) → оценка 0–10, ось,
перевод названия и полный перевод абстракта. В дайджест — до 15 статей с оценкой ≥ 6.
Ваши оценки 👍/👎 подмешиваются в промпт как примеры предпочтений.

## Куда шлёт
- **TELEGRAM_CHAT_ID** — карточка: ось и балл, название (EN + RU), библиография NLM, перевод абстракта,
  ссылки + кнопки 👍 Полезно / 🤔 Частично / 👎 Мимо.
- **SECONDARY_CHAT_ID** — только название, ссылка и перевод абстракта, без кнопок.

## Службы
- `vetdigest.timer` → `vetdigest.service` — сбор и рассылка.
- `vetdigest-feedback.service` — постоянно слушает нажатия кнопок, команды `/chatid` и `/stats`.

## Команды
```bash
sudo -u vetdigest /opt/vetdigest/venv/bin/python /opt/vetdigest/vetdigest.py --check    # доступы
sudo -u vetdigest /opt/vetdigest/venv/bin/python /opt/vetdigest/vetdigest.py --dry-run  # без отправки
sudo systemctl start vetdigest.service                                                   # боевой прогон
sudo tail -f /opt/vetdigest/data/logs/run_$(date -u +%F).log
sudo systemctl status vetdigest-feedback --no-pager
```

## Данные
`/opt/vetdigest/data/`: `sent_papers.json` (уже отправленные), `cards.json`, `ratings.json` (ваши оценки),
`stats.json`, `digests/*.md`, `payloads/*.jsonl`, `logs/`.

## Установка / обновление
Новая установка: `sudo bash install.sh`. Обновление с v1: `sudo bash update.sh` (делает бэкап,
`.env` не перезаписывает, только дописывает новые переменные).
