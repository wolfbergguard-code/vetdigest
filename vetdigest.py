#!/usr/bin/env python3
"""
VetRadiometry Weekly Digest — цикл сбора и доставки.

Источники (все бесплатны):
  * PubMed E-utilities (тематические запросы + фильтр по журналам)
  * CrossRef (обогащение библиографии)
  * bioRxiv API (препринты)
  * arXiv API (ML-препринты)

LLM-обработка: Perplexity Sonar API (модель "sonar", low search context).

Доставка: Telegram Bot API напрямую (без Pipedream).

Запуск:
    python3 vetdigest.py                  # обычный запуск
    python3 vetdigest.py --dry-run        # собрать, но НЕ слать в Telegram
    python3 vetdigest.py --window-days N  # окно поиска (по умолч. 7)

Переменные окружения (.env):
    PPLX_API_KEY        — API-ключ Perplexity
    TELEGRAM_BOT_TOKEN  — токен Telegram-бота
    TELEGRAM_CHAT_ID    — chat_id получателя
    NCBI_EMAIL          — email для NCBI/Unpaywall
    NCBI_TOOL           — идентификатор клиента NCBI (по умолч. VetRadiometryDigest)
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import logging.handlers
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
DIGEST_DIR = DATA_DIR / "digests"
PAYLOAD_DIR = DATA_DIR / "payloads"

for d in (DATA_DIR, LOG_DIR, DIGEST_DIR, PAYLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)

SENT_PAPERS_FILE = DATA_DIR / "sent_papers.json"
STATS_FILE = DATA_DIR / "stats.json"

GLOBAL_BUDGET_SEC = 1500  # 25 минут
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 25
DEFAULT_WINDOW_DAYS = 7
FALLBACK_WINDOW_DAYS = 14
MIN_ARTICLES_BEFORE_WIDEN = 5
MAX_ARTICLES_TO_LLM = 40   # cap на количество запросов к Sonar за один прогон
MAX_ARTICLES_IN_DIGEST = 15

PPLX_ENDPOINT = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL = "sonar"
TG_API_ROOT = "https://api.telegram.org"

# ---------------------------------------------------------------------------
# Приоритетные журналы (PubMed [Journal]-запросы)
# ---------------------------------------------------------------------------

VET_JOURNALS = [
    "J Vet Cardiol",
    "J Vet Intern Med",
    "J Feline Med Surg",
    "J Am Vet Med Assoc",
    "Am J Vet Res",
    "Vet Clin Pathol",
    "Vet Pathol",
    "Vet J",
    "Open Vet J",
    "J Am Anim Hosp Assoc",
    "J Vet Diagn Invest",
    "Vet Med Sci",
    "Vet Comp Oncol",
    "J Vet Sci",
    "Vet Sci",
    "Front Vet Sci",
    "BMC Vet Res",
    "Vet Radiol Ultrasound",
    "Res Vet Sci",
]

# Тематические PubMed-запросы (кошки/собаки)
TOPIC_QUERIES = [
    ("cardiomyopathy", "🫀 Кардиомиопатии",
     '("cardiomyopathy" OR "HCM" OR "DCM" OR "ARVC" OR "restrictive cardiomyopathy") '
     'AND (dog OR canine OR cat OR feline)'),
    ("pulmonary_hypertension", "🌬 Лёгочная гипертензия",
     '("pulmonary hypertension" OR sildenafil OR tadalafil) '
     'AND (dog OR canine OR cat OR feline)'),
    ("myocarditis", "🦠 Миокардиты",
     '(myocarditis) AND (dog OR canine OR cat OR feline)'),
    ("endocarditis", "💢 Эндокардиты",
     '("endocarditis" OR "vegetative endocarditis") AND (dog OR canine OR cat OR feline)'),
    ("thromboembolism", "🩸 Тромбоэмболии",
     '("arterial thromboembolism" OR FATE OR thromboembolism) '
     'AND (cat OR feline OR dog OR canine)'),
    ("ecg", "⚡ ЭКГ",
     '("electrocardiography" OR arrhythmia OR Holter OR ECG) '
     'AND (dog OR canine OR cat OR feline)'),
    ("echocardiography", "📡 ЭхоКГ",
     '("echocardiography" OR "speckle tracking" OR "strain imaging" OR "3D echocardiography") '
     'AND (dog OR canine OR cat OR feline)'),
    ("radiography_ml", "📷🤖 Радиография + ML",
     '(("VHS" OR "vertebral heart score" OR "VLAS" OR "vertebral left atrial size" '
     'OR "thoracic radiograph") AND (dog OR canine OR cat OR feline)) '
     'OR (("landmark detection" OR "keypoint detection" OR "YOLO" OR "deep learning") '
     'AND (veterinary OR canine OR feline))'),
]

# Порядок и эмодзи блоков в дайджесте
TOPIC_ORDER = [
    ("cardiomyopathy", "🫀 Кардиомиопатии"),
    ("pulmonary_hypertension", "🌬 Лёгочная гипертензия"),
    ("myocarditis", "🦠 Миокардиты"),
    ("endocarditis", "💢 Эндокардиты"),
    ("thromboembolism", "🩸 Тромбоэмболии"),
    ("ecg", "⚡ ЭКГ"),
    ("echocardiography", "📡 ЭхоКГ"),
    ("radiography", "📷 Радиография"),
    ("ml", "🤖 ML/ИИ"),
]

SPECIES_INCLUDE = {
    "dog", "dogs", "canine", "cat", "cats", "feline",
    "small animal", "companion animal", "puppy", "kitten"
}
SPECIES_EXCLUDE = {
    "horse", "horses", "equine", "bovine", "cattle", "cow", "cows",
    "porcine", "pig", "pigs", "swine", "sheep", "goat", "poultry", "chicken"
}

RELEVANCE_ORDER = {"high": 0, "medium": 1, "low": 2}

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

def setup_logging(dry_run: bool = False) -> logging.Logger:
    logger = logging.getLogger("vetdigest")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"run_{date_str}.log",
        maxBytes=5 * 1024 * 1024, backupCount=12, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    logger.info("=== VetDigest v%s начал работу (dry_run=%s) ===", VERSION, dry_run)
    return logger


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

@dataclass
class Article:
    pmid: str | None = None
    doi: str | None = None
    title: str = ""
    abstract: str = ""
    journal: str = ""
    year: str = ""
    month: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    authors: list[str] = field(default_factory=list)
    source: str = "pubmed"        # pubmed | biorxiv | arxiv
    topic_hint: str = ""

    # LLM-derived
    relevance: str | None = None
    topic_block: str | None = None
    russian_abstract: str | None = None
    why_relevant: str | None = None

    def uid(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.pmid:
            return f"pmid:{self.pmid}"
        seed = f"{self.title}|{','.join(self.authors[:1])}|{self.year}"
        return "sha1:" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


class Budget:
    """Отслеживает глобальный тайм-бюджет прогона."""
    def __init__(self, seconds: int) -> None:
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def rate_limited_get(url: str, params: dict | None, headers: dict | None,
                     logger: logging.Logger, retries: int = 3,
                     backoff_seq: tuple = (30, 60, 120)) -> requests.Response | None:
    """GET с ретраями на 429/5xx и экспоненциальным бэкоффом."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as exc:
            logger.warning("GET %s: сетевая ошибка %s (попытка %d)", url, exc, attempt + 1)
            if attempt < retries - 1:
                time.sleep(backoff_seq[min(attempt, len(backoff_seq) - 1)])
                continue
            return None

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = backoff_seq[min(attempt, len(backoff_seq) - 1)]
            logger.warning("GET %s → HTTP %d, жду %d с (попытка %d)",
                           url, resp.status_code, wait, attempt + 1)
            if attempt < retries - 1:
                time.sleep(wait)
                continue
            return None
        return resp
    return None


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

NCBI_EMAIL = ""
NCBI_TOOL = "VetRadiometryDigest"


def pubmed_esearch(query: str, mindate: str, maxdate: str,
                   logger: logging.Logger, retmax: int = 50) -> list[str]:
    params = {
        "db": "pubmed",
        "term": query,
        "sort": "date",
        "retmode": "json",
        "retmax": retmax,
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    resp = rate_limited_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params=params, headers=None, logger=logger
    )
    if resp is None or resp.status_code != 200:
        return []
    try:
        return resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []


def pubmed_efetch(pmids: list[str], logger: logging.Logger) -> list[Article]:
    if not pmids:
        return []
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "abstract",
        "retmode": "xml",
        "tool": NCBI_TOOL,
        "email": NCBI_EMAIL,
    }
    resp = rate_limited_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=params, headers=None, logger=logger
    )
    if resp is None or resp.status_code != 200:
        return []

    articles: list[Article] = []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning("PubMed efetch parse error: %s", exc)
        return []

    for pub in root.iter("PubmedArticle"):
        art = Article(source="pubmed")
        pmid = pub.findtext(".//PMID")
        if pmid:
            art.pmid = pmid.strip()

        # DOI
        for aid in pub.findall(".//ArticleId"):
            if aid.attrib.get("IdType") == "doi" and aid.text:
                art.doi = aid.text.strip()

        # Title / abstract
        art.title = " ".join((pub.findtext(".//ArticleTitle") or "").split())
        abstract_parts = []
        for ab in pub.findall(".//Abstract/AbstractText"):
            label = ab.attrib.get("Label")
            text = " ".join((ab.text or "").split())
            if label:
                abstract_parts.append(f"{label}: {text}")
            else:
                abstract_parts.append(text)
        art.abstract = "\n".join(x for x in abstract_parts if x).strip()

        # Journal
        art.journal = pub.findtext(".//Journal/ISOAbbreviation") or ""
        art.year = pub.findtext(".//JournalIssue/PubDate/Year") or ""
        art.month = pub.findtext(".//JournalIssue/PubDate/Month") or ""
        art.volume = pub.findtext(".//JournalIssue/Volume") or ""
        art.issue = pub.findtext(".//JournalIssue/Issue") or ""
        art.pages = pub.findtext(".//Pagination/MedlinePgn") or ""

        # Authors
        for au in pub.findall(".//AuthorList/Author"):
            ln = au.findtext("LastName") or ""
            ini = au.findtext("Initials") or ""
            if ln:
                art.authors.append(f"{ln} {ini}".strip())

        articles.append(art)

    return articles


def collect_from_pubmed(mindate: str, maxdate: str, budget: Budget,
                        logger: logging.Logger) -> list[Article]:
    """Собирает статьи из PubMed: сначала по темам, потом по журналам."""
    all_pmids: set[str] = set()
    topic_map: dict[str, str] = {}   # pmid -> topic_id

    # Тематические запросы
    for topic_id, _emoji, query in TOPIC_QUERIES:
        if budget.expired():
            break
        pmids = pubmed_esearch(query, mindate, maxdate, logger, retmax=30)
        logger.info("PubMed[topic:%s]: найдено %d PMID", topic_id, len(pmids))
        for p in pmids:
            all_pmids.add(p)
            topic_map.setdefault(p, topic_id)
        time.sleep(0.15)

    # Журнальные запросы
    for journal in VET_JOURNALS:
        if budget.expired():
            break
        query = f'"{journal}"[Journal]'
        pmids = pubmed_esearch(query, mindate, maxdate, logger, retmax=20)
        if pmids:
            logger.info("PubMed[journal:%s]: найдено %d PMID", journal, len(pmids))
        for p in pmids:
            all_pmids.add(p)
        time.sleep(0.15)

    if not all_pmids:
        return []

    logger.info("PubMed: всего уникальных PMID = %d, идём в efetch", len(all_pmids))

    # efetch пакетами по 50
    articles: list[Article] = []
    pmid_list = list(all_pmids)
    for i in range(0, len(pmid_list), 50):
        if budget.expired():
            break
        chunk = pmid_list[i:i + 50]
        arts = pubmed_efetch(chunk, logger)
        for a in arts:
            if a.pmid and a.pmid in topic_map:
                a.topic_hint = topic_map[a.pmid]
        articles.extend(arts)
        time.sleep(0.15)

    logger.info("PubMed efetch: получено %d полных записей", len(articles))
    return articles


# ---------------------------------------------------------------------------
# bioRxiv
# ---------------------------------------------------------------------------

BIORXIV_TERMS = re.compile(
    r"\b(cardio|myocard|echocard|arrhythm|hcm|dcm|thromboembol|"
    r"pulmonary hypertension|vhs|vertebral heart|vlas|"
    r"veterinary|canine|feline|dog|cat)\b",
    re.IGNORECASE,
)


def collect_from_biorxiv(mindate: str, maxdate: str, budget: Budget,
                         logger: logging.Logger) -> list[Article]:
    url = f"https://api.biorxiv.org/details/biorxiv/{mindate}/{maxdate}/0"
    resp = rate_limited_get(url, params=None, headers=None, logger=logger)
    if resp is None or resp.status_code != 200:
        return []
    try:
        data = resp.json()
    except ValueError:
        return []
    articles: list[Article] = []
    for item in data.get("collection", []):
        title = item.get("title", "")
        abstract = item.get("abstract", "")
        blob = f"{title} {abstract}"
        if not BIORXIV_TERMS.search(blob):
            continue
        art = Article(
            source="biorxiv",
            doi=item.get("doi"),
            title=title,
            abstract=abstract,
            journal="bioRxiv (preprint)",
            year=(item.get("date") or "")[:4],
            authors=[a.strip() for a in (item.get("authors") or "").split(";") if a.strip()],
        )
        articles.append(art)
    logger.info("bioRxiv: отобрано %d релевантных препринтов", len(articles))
    return articles


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

ARXIV_QUERY = (
    "(cat:cs.CV OR cat:eess.IV) AND "
    "(all:veterinary OR all:canine OR all:feline OR "
    "(all:dog AND (all:radiograph OR all:cardiothoracic OR all:landmark OR all:keypoint)))"
)


def collect_from_arxiv(mindate: str, budget: Budget,
                       logger: logging.Logger) -> list[Article]:
    params = {
        "search_query": ARXIV_QUERY,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": "30",
    }
    resp = rate_limited_get("http://export.arxiv.org/api/query",
                            params=params, headers=None, logger=logger)
    if resp is None or resp.status_code != 200:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return []

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    articles: list[Article] = []
    min_dt = datetime.strptime(mindate, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    for entry in root.findall("atom:entry", ns):
        pub = entry.findtext("atom:published", default="", namespaces=ns)
        try:
            dt = datetime.strptime(pub[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dt < min_dt:
            continue

        title = " ".join((entry.findtext("atom:title", "", ns) or "").split())
        abstract = " ".join((entry.findtext("atom:summary", "", ns) or "").split())
        art = Article(
            source="arxiv",
            title=title,
            abstract=abstract,
            journal="arXiv (preprint)",
            year=dt.strftime("%Y"),
            month=dt.strftime("%b"),
            topic_hint="radiography_ml",
        )
        # arXiv id → DOI-подобная ссылка
        arxiv_id = entry.findtext("atom:id", "", ns).rsplit("/", 1)[-1]
        art.doi = f"10.48550/arXiv.{arxiv_id.split('v')[0]}"
        for au in entry.findall("atom:author/atom:name", ns):
            if au.text:
                art.authors.append(au.text.strip())
        articles.append(art)

    logger.info("arXiv: отобрано %d препринтов cs.CV/eess.IV", len(articles))
    return articles


# ---------------------------------------------------------------------------
# Фильтрация
# ---------------------------------------------------------------------------

def species_filter(article: Article) -> bool:
    blob = f"{article.title} {article.abstract}".lower()
    has_include = any(k in blob for k in SPECIES_INCLUDE)
    has_exclude = any(k in blob for k in SPECIES_EXCLUDE)
    if has_include:
        return True
    if has_exclude:
        return False
    # Ни того, ни другого — оставляем (людские transational-статьи)
    return True


def dedup_filter(articles: list[Article], sent: set[str]) -> list[Article]:
    out = []
    for a in articles:
        if a.uid() in sent:
            continue
        if not a.abstract or len(a.abstract) < 40:
            continue
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# Sonar LLM
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "Ты — научный ассистент ветеринарного кардиолога Дмитрия Олейникова. "
    "Он разрабатывает VetRadiometry — Python/YOLOv8-приложение для автоматического "
    "анализа рентгенограмм сердца собак и кошек (VHS, VLAS, landmark-detection). "
    "Твоя задача — по данному абстракту англоязычной научной статьи: "
    "(1) оценить релевантность работе Дмитрия по шкале high/medium/low, "
    "(2) отнести к одному из 9 тематических блоков "
    "(cardiomyopathy, pulmonary_hypertension, myocarditis, endocarditis, "
    "thromboembolism, ecg, echocardiography, radiography, ml), "
    "(3) дать полный перевод абстракта на русский с сохранением структуры "
    "Background/Methods/Results/Conclusions → Введение/Методы/Результаты/Выводы, "
    "(4) в поле why_relevant объяснить в 1–2 предложениях, почему это ценно (или неценно). "
    "Аббревиатуры HCM, DCM, ARVC, VHS, VLAS, FATE, ECG/ЭКГ, HFpEF, CKCS, ML/AI оставлять как есть. "
    "Ответ верни строго в формате JSON без markdown-обёртки."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance": {"type": "string", "enum": ["high", "medium", "low"]},
        "topic_block": {
            "type": "string",
            "enum": ["cardiomyopathy", "pulmonary_hypertension", "myocarditis",
                     "endocarditis", "thromboembolism", "ecg",
                     "echocardiography", "radiography", "ml"],
        },
        "russian_abstract": {"type": "string"},
        "why_relevant": {"type": "string"},
    },
    "required": ["relevance", "topic_block", "russian_abstract", "why_relevant"],
}


def llm_annotate(article: Article, pplx_key: str, logger: logging.Logger,
                 payload_log: Path) -> bool:
    """Обогащает статью relevance/topic_block/russian_abstract/why_relevant."""
    body = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": (f"Title: {article.title}\n\n"
                         f"Journal: {article.journal}\n\n"
                         f"Abstract:\n{article.abstract}")}
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"schema": RESPONSE_SCHEMA}
        },
        "web_search_options": {"search_context_size": "low"},
        "temperature": 0.2,
        "max_tokens": 1500,
    }

    headers = {"Authorization": f"Bearer {pplx_key}",
               "Content-Type": "application/json"}

    for attempt in range(3):
        try:
            resp = requests.post(PPLX_ENDPOINT, json=body, headers=headers,
                                 timeout=(CONNECT_TIMEOUT, 60))
        except requests.RequestException as exc:
            logger.warning("Sonar network error: %s (попытка %d)", exc, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue

        if resp.status_code in (401, 402):
            logger.error("Sonar %d: %s — деньги на API-кошельке кончились?",
                         resp.status_code, resp.text[:200])
            raise RuntimeError("sonar_wallet_empty")

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = (30, 60, 120)[attempt]
            logger.warning("Sonar %d, жду %d с", resp.status_code, wait)
            time.sleep(wait)
            continue

        if resp.status_code != 200:
            logger.warning("Sonar HTTP %d: %s", resp.status_code, resp.text[:200])
            return False

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Sonar: ответ не JSON")
            return False

        with payload_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"uid": article.uid(), "response": data},
                               ensure_ascii=False) + "\n")

        try:
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            logger.warning("Sonar: не смог распарсить ответ (%s)", exc)
            return False

        article.relevance = parsed.get("relevance")
        article.topic_block = parsed.get("topic_block")
        article.russian_abstract = parsed.get("russian_abstract")
        article.why_relevant = parsed.get("why_relevant")
        return True

    return False


# ---------------------------------------------------------------------------
# Форматирование карточек
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    """Экранирует HTML-спецсимволы для Telegram HTML parse mode."""
    return html.escape(text or "", quote=False)


def nlm_bibliography(a: Article) -> str:
    authors = ", ".join(a.authors[:6])
    if len(a.authors) > 6:
        authors += ", et al"
    parts = [authors + "." if authors else ""]
    parts.append(f"{a.title.rstrip('.')}." if a.title else "")
    parts.append(f"{a.journal}." if a.journal else "")
    date_bits = " ".join(x for x in [a.year, a.month] if x).strip()
    tail = ""
    if date_bits:
        tail = date_bits
    if a.volume:
        tail += f";{a.volume}"
        if a.issue:
            tail += f"({a.issue})"
        if a.pages:
            tail += f":{a.pages}"
    if tail:
        parts.append(tail + ".")
    if a.doi:
        parts.append(f"doi:{a.doi}.")
    if a.pmid:
        parts.append(f"PMID: {a.pmid}.")
    return " ".join(p for p in parts if p)


def format_card(a: Article, ncbi_email: str) -> str:
    rel = a.relevance or "low"
    body = [
        f"<b>[{rel}]</b> {esc(a.title)}",
        "",
        "📚 <b>Библиография (NLM):</b>",
        esc(nlm_bibliography(a)),
        "",
        "🌐 <b>Абстракт (RU):</b>",
        esc(a.russian_abstract or a.abstract),
        "",
        "💡 <b>Почему релевантно:</b>",
        esc(a.why_relevant or "—"),
        "",
    ]
    links = []
    if a.doi:
        links.append(f'🔗 <a href="https://doi.org/{esc(a.doi)}">Оригинал</a>')
        links.append(f'🔓 <a href="https://api.unpaywall.org/v2/{esc(a.doi)}?email={esc(ncbi_email)}">Unpaywall</a>')
    if a.pmid:
        links.append(f'📖 <a href="https://pubmed.ncbi.nlm.nih.gov/{esc(a.pmid)}/">PubMed</a>')
    if links:
        body.append(" · ".join(links))
    return "\n".join(body).strip()


def chunk_messages(header: str, sections: list[tuple[str, list[str]]],
                   max_chars: int = 3900) -> list[str]:
    """Разбивает дайджест на сообщения ≤ max_chars, не рвя карточку."""
    chunks: list[str] = [header]
    current = ""
    separator = "\n\n──────\n\n"
    for section_title, cards in sections:
        if not cards:
            continue
        block = section_title
        if len(block) > max_chars:
            block = block[:max_chars]
        if current and len(current) + len(separator) + len(block) > max_chars:
            chunks.append(current)
            current = block
        else:
            current = current + (separator if current else "") + block

        for card in cards:
            if len(card) > max_chars:
                card = card[: max_chars - 20] + "…\n(усечено)"
            if len(current) + len(separator) + len(card) > max_chars:
                chunks.append(current)
                current = card
            else:
                current = current + separator + card

    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_send(bot_token: str, chat_id: str, text: str,
            logger: logging.Logger) -> bool:
    url = f"{TG_API_ROOT}/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, timeout=(5, 30))
        except requests.RequestException as exc:
            logger.warning("Telegram: сетевая ошибка %s", exc)
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 200:
            return True
        if resp.status_code == 429:
            retry = resp.json().get("parameters", {}).get("retry_after", 5)
            logger.warning("Telegram 429, жду %d с", retry)
            time.sleep(retry + 1)
            continue
        logger.error("Telegram HTTP %d: %s", resp.status_code, resp.text[:300])
        return False
    return False


# ---------------------------------------------------------------------------
# Основной пайплайн
# ---------------------------------------------------------------------------

def build_sections(articles: list[Article], ncbi_email: str
                   ) -> tuple[list[tuple[str, list[str]]], dict[str, int]]:
    """Группирует статьи по блокам, сортирует, форматирует карточки."""
    grouped: dict[str, list[Article]] = {tid: [] for tid, _ in TOPIC_ORDER}
    for a in articles:
        block = a.topic_block or "radiography"
        # radiography_ml из тем PubMed → ml (или radiography — LLM решит)
        if block not in grouped:
            block = "ml" if "ml" in block or "detect" in block else "radiography"
        grouped[block].append(a)

    sections: list[tuple[str, list[str]]] = []
    counts: dict[str, int] = {}
    for topic_id, emoji_name in TOPIC_ORDER:
        arts = grouped[topic_id]
        arts.sort(key=lambda x: (RELEVANCE_ORDER.get(x.relevance or "low", 3),
                                 -int(x.year or 0)))
        counts[topic_id] = len(arts)
        if not arts:
            continue
        title = f"─── {emoji_name} ───"
        cards = [format_card(a, ncbi_email) for a in arts]
        sections.append((title, cards))
    return sections, counts


def build_summary_header(date_str: str, counts: dict[str, int],
                         total: int, high: int, window_days: int,
                         msg_count: int) -> str:
    def n(t):
        return counts.get(t, 0)
    return (
        f"📋 <b>Еженедельный научный дайджест</b> · {date_str}\n\n"
        f"Сводка:\n"
        f"🫀 Кардиомиопатии ({n('cardiomyopathy')}) · "
        f"🌬 ЛГ ({n('pulmonary_hypertension')}) · "
        f"🦠 Миокардиты ({n('myocarditis')})\n"
        f"💢 Эндокардиты ({n('endocarditis')}) · "
        f"🩸 Тромбоэмболии ({n('thromboembolism')}) · "
        f"⚡ ЭКГ ({n('ecg')})\n"
        f"📡 ЭхоКГ ({n('echocardiography')}) · "
        f"📷 Радиография ({n('radiography')}) · "
        f"🤖 ML ({n('ml')})\n\n"
        f"📊 Итого: {total} статей · {high} высокой релевантности. "
        f"Окно: {window_days} дней.\n\n"
        f"Дайджест следует в {msg_count} сообщениях ниже."
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Собрать и обработать, но не слать в Telegram")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    parser.add_argument("--no-llm", action="store_true",
                        help="Не вызывать Sonar (для отладки)")
    args = parser.parse_args()

    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")

    pplx_key = os.getenv("PPLX_API_KEY", "").strip()
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    global NCBI_EMAIL, NCBI_TOOL
    NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()
    NCBI_TOOL = os.getenv("NCBI_TOOL", "VetRadiometryDigest").strip()

    logger = setup_logging(dry_run=args.dry_run)

    missing = [name for name, val in [
        ("PPLX_API_KEY", pplx_key),
        ("TELEGRAM_BOT_TOKEN", tg_token),
        ("TELEGRAM_CHAT_ID", tg_chat),
        ("NCBI_EMAIL", NCBI_EMAIL),
    ] if not val]
    if missing and not args.dry_run:
        logger.error("Отсутствуют переменные окружения: %s", ", ".join(missing))
        return 2
    if args.no_llm:
        pplx_key = ""

    budget = Budget(GLOBAL_BUDGET_SEC)

    # Окно
    today = datetime.now(timezone.utc).date()
    maxdate = today.strftime("%Y-%m-%d")
    mindate = (today - timedelta(days=args.window_days)).strftime("%Y-%m-%d")
    logger.info("Окно поиска: %s .. %s", mindate, maxdate)

    # Сбор
    articles: list[Article] = []
    try:
        articles.extend(collect_from_pubmed(mindate, maxdate, budget, logger))
    except Exception as exc:
        logger.exception("PubMed collect failed: %s", exc)

    try:
        articles.extend(collect_from_biorxiv(mindate, maxdate, budget, logger))
    except Exception as exc:
        logger.exception("bioRxiv collect failed: %s", exc)

    try:
        articles.extend(collect_from_arxiv(mindate, budget, logger))
    except Exception as exc:
        logger.exception("arXiv collect failed: %s", exc)

    logger.info("Собрано всего: %d статей", len(articles))

    # Расширение окна, если мало
    if len(articles) < MIN_ARTICLES_BEFORE_WIDEN and args.window_days == DEFAULT_WINDOW_DAYS:
        mindate2 = (today - timedelta(days=FALLBACK_WINDOW_DAYS)).strftime("%Y-%m-%d")
        logger.info("Расширяю окно до %d дней (%s..%s)",
                    FALLBACK_WINDOW_DAYS, mindate2, maxdate)
        try:
            more = collect_from_pubmed(mindate2, maxdate, budget, logger)
            seen = {a.pmid for a in articles if a.pmid}
            articles.extend([a for a in more if a.pmid not in seen])
        except Exception as exc:
            logger.exception("PubMed wide collect failed: %s", exc)
        window_days = FALLBACK_WINDOW_DAYS
    else:
        window_days = args.window_days

    # Фильтры
    articles = [a for a in articles if species_filter(a)]
    logger.info("После species-фильтра: %d", len(articles))

    sent = set(load_json(SENT_PAPERS_FILE, {}).get("uids", []))
    articles = dedup_filter(articles, sent)
    logger.info("После дедупликации: %d", len(articles))

    if len(articles) > MAX_ARTICLES_TO_LLM:
        logger.info("Обрезаю до %d статей для LLM", MAX_ARTICLES_TO_LLM)
        articles = articles[:MAX_ARTICLES_TO_LLM]

    # LLM аннотация
    payload_log = PAYLOAD_DIR / f"pplx_{maxdate}.jsonl"
    if pplx_key and not args.no_llm:
        for i, art in enumerate(articles):
            if budget.expired():
                logger.warning("Бюджет времени истёк, LLM обработано %d/%d",
                               i, len(articles))
                break
            try:
                ok = llm_annotate(art, pplx_key, logger, payload_log)
                if not ok:
                    art.relevance = "low"
                    art.topic_block = art.topic_hint.split("_")[0] or "radiography"
                    art.russian_abstract = art.abstract
                    art.why_relevant = "(LLM недоступен, оценка не выполнена)"
            except RuntimeError:
                logger.error("Останавливаю LLM — кошелёк Perplexity пуст")
                break
            time.sleep(0.5)
    else:
        for art in articles:
            art.relevance = "medium"
            art.topic_block = art.topic_hint.split("_")[0] or "radiography"
            art.russian_abstract = art.abstract
            art.why_relevant = "(LLM отключён)"

    # Оставляем high + medium
    selected = [a for a in articles if a.relevance in ("high", "medium")]
    selected.sort(key=lambda x: (RELEVANCE_ORDER.get(x.relevance or "low", 3),
                                 -int(x.year or 0)))
    if len(selected) > MAX_ARTICLES_IN_DIGEST:
        selected = selected[:MAX_ARTICLES_IN_DIGEST]

    logger.info("Итого к доставке: %d статей", len(selected))

    # Сборка
    sections, counts = build_sections(selected, NCBI_EMAIL)
    total = len(selected)
    high_count = sum(1 for a in selected if a.relevance == "high")

    date_ru = today.strftime("%d.%m.%Y")

    # Пустой дайджест — только заглушка
    if not selected:
        no_news = f"📋 Еженедельный научный дайджест · {date_ru}\n\nЗа неделю новых публикаций по вашим темам не найдено."
        markdown_path = DIGEST_DIR / f"digest_{maxdate}.md"
        markdown_path.write_text(no_news, encoding="utf-8")
        if not args.dry_run:
            tg_send(tg_token, tg_chat, esc(no_news), logger)
        logger.info("Пустой дайджест сохранён и/или отправлен")
        return 0

    # Считаем финальное число сообщений
    header = build_summary_header(date_ru, counts, total, high_count,
                                  window_days, 0)
    tentative_messages = chunk_messages(header, sections)
    # Заменим placeholder в шапке на актуальное число
    real_msg_count = len(tentative_messages) - 1  # без шапки
    header = build_summary_header(date_ru, counts, total, high_count,
                                  window_days, real_msg_count)
    messages = chunk_messages(header, sections)

    # Сохраняем полный MD
    md_lines = [f"# Дайджест {date_ru}",
                f"Статей: {total}, high: {high_count}, окно: {window_days} дней\n"]
    for section_title, cards in sections:
        md_lines.append(f"\n## {section_title}\n")
        for c in cards:
            md_lines.append(c + "\n")
    (DIGEST_DIR / f"digest_{maxdate}.md").write_text(
        "\n".join(md_lines), encoding="utf-8")

    # Доставка
    if args.dry_run:
        logger.info("--dry-run: НЕ отправляю в Telegram. Сообщений: %d",
                    len(messages))
        for i, m in enumerate(messages):
            logger.info("Msg %d: %d chars", i, len(m))
        return 0

    for i, m in enumerate(messages):
        ok = tg_send(tg_token, tg_chat, m, logger)
        if not ok:
            logger.error("Не удалось отправить сообщение %d, прерываю доставку", i)
            return 1
        time.sleep(0.35)

    # Обновляем дедуп
    sent_data = load_json(SENT_PAPERS_FILE, {"uids": [], "history": []})
    for a in selected:
        sent_data["uids"].append(a.uid())
    sent_data["uids"] = sorted(set(sent_data["uids"]))
    sent_data["history"].append({
        "date": maxdate, "sent": [a.uid() for a in selected]
    })
    save_json(SENT_PAPERS_FILE, sent_data)

    # Статистика
    stats = load_json(STATS_FILE, {"runs": []})
    stats["runs"].append({
        "date": maxdate,
        "collected": len(articles),
        "sent": total,
        "high": high_count,
        "window_days": window_days,
    })
    save_json(STATS_FILE, stats)

    logger.info("✅ Дайджест доставлен: %d статей в %d сообщениях", total, len(messages))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logging.getLogger("vetdigest").exception("Фатальная ошибка: %s", exc)
        sys.exit(1)
