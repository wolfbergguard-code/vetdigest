#!/usr/bin/env python3
"""
VetDigest v2 — еженедельный научный дайджест для ветеринарного кардиолога.

Три тематические оси:
  * vet_cardio — ветеринарная кардиология (собаки, кошки)
  * ai         — внедрение ИИ / ML в кардиологию и ветеринарную визуализацию
  * pathophys  — патофизиологические эксперименты (модели на животных,
                 ex vivo / in vivo, in silico)

Источники (бесплатные):
  * PubMed E-utilities — тематические запросы + фильтр [Journal]
    по приоритетному списку журналов (ветеринарные, кардиология человека,
    фундаментальные)
  * Semantic Scholar Graph API (bulk search) — ключевые запросы по тем же осям

LLM: Perplexity Sonar API — оценка релевантности 0–10, ось, перевод
названия и полный перевод абстракта на русский. Без объяснений «почему».

Доставка (Telegram Bot API напрямую):
  * основной чат (TELEGRAM_CHAT_ID) — карточка на статью: название,
    библиография NLM, перевод абстракта, ссылки + кнопки оценки
    (👍 / 🤔 / 👎). Оценки ловит vetdigest_feedback.py.
  * второй чат (SECONDARY_CHAT_ID, необязательно) — только название,
    ссылка и перевод абстракта, без кнопок.

Запуск:
    python3 vetdigest.py                # обычный запуск
    python3 vetdigest.py --dry-run      # собрать, но не слать в Telegram
    python3 vetdigest.py --check        # проверить токены и доступы
    python3 vetdigest.py --window-days N
    python3 vetdigest.py --no-llm       # без Sonar (отладка)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import json
import logging
import logging.handlers
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
DIGEST_DIR = DATA_DIR / "digests"
PAYLOAD_DIR = DATA_DIR / "payloads"

for d in (DATA_DIR, LOG_DIR, DIGEST_DIR, PAYLOAD_DIR):
    d.mkdir(parents=True, exist_ok=True)

SENT_PAPERS_FILE = DATA_DIR / "sent_papers.json"
STATS_FILE = DATA_DIR / "stats.json"
CARDS_FILE = DATA_DIR / "cards.json"        # key -> метаданные карточки
RATINGS_FILE = DATA_DIR / "ratings.json"    # key -> оценка пользователя
LOCK_FILE = DATA_DIR / ".state.lock"
SECONDARY_QUEUE_FILE = DATA_DIR / "secondary_queue.json"  # отложенная отправка во второй чат

GLOBAL_BUDGET_SEC = 2400  # 40 минут на весь прогон
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 40
DEFAULT_WINDOW_DAYS = 7
FALLBACK_WINDOW_DAYS = 14
MIN_ARTICLES_BEFORE_WIDEN = 8

MAX_ARTICLES_TO_LLM = int(os.getenv("VETDIGEST_MAX_LLM", "50"))
MAX_ARTICLES_IN_DIGEST = int(os.getenv("VETDIGEST_MAX_CARDS", "15"))
MIN_SCORE = int(os.getenv("VETDIGEST_MIN_SCORE", "6"))

PPLX_ENDPOINT = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL = "sonar"
TG_API_ROOT = "https://api.telegram.org"
TG_LIMIT = 3900

AXES = {
    "vet_cardio": "🫀 Ветеринарная кардиология",
    "ai": "🤖 ИИ и ML",
    "pathophys": "🔬 Патофизиология и эксперимент",
}
AXIS_ORDER = ["vet_cardio", "ai", "pathophys"]

RATING_BUTTONS = [("2", "👍 Полезно"), ("1", "🤔 Частично"), ("0", "👎 Мимо")]

# ---------------------------------------------------------------------------
# Журналы (список пользователя)
# ---------------------------------------------------------------------------

# Ветеринарные: берём статьи по кардиологии / ИИ / эксперименту
VET_JOURNALS = [
    "J Vet Cardiol",          # берём всё
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
VET_TAKE_ALL = {"J Vet Cardiol"}

# Кардиология человека (трансляционная) — патофизиология / ИИ
HUMAN_CARDIO_JOURNALS = [
    "N Engl J Med",
    "Am J Cardiol",
    "Circulation",
    "Circ Res",
    "Pulm Circ",
    "Circ Heart Fail",
    "Circ Arrhythm Electrophysiol",
    "Circ Cardiovasc Imaging",
    "J Am Heart Assoc",
    "Hypertension",
]

# Фундаментальные / биофизика — только сердечно-сосудистое
FUNDAMENTAL_JOURNALS = [
    "Nature",
    "Cell",
    "Cell Metab",
    "Cell Rep Med",
    "Biophys J",
]

ALL_PRIORITY_JOURNALS = VET_JOURNALS + HUMAN_CARDIO_JOURNALS + FUNDAMENTAL_JOURNALS

# ---------------------------------------------------------------------------
# PubMed-фрагменты запросов
# ---------------------------------------------------------------------------

PM_SPECIES = "(dog OR dogs OR canine OR cat OR cats OR feline)"
PM_CARDIO = (
    "(heart OR cardiac OR cardio* OR myocard* OR echocardiograph* OR arrhythm* "
    "OR electrocardiogra* OR \"pulmonary hypertension\" OR thromboemb* OR valv* "
    "OR murmur OR hypertension OR hemodynam* OR haemodynam* OR \"NT-proBNP\" "
    "OR troponin OR cardiomyopath*)"
)
PM_AI = (
    "(\"artificial intelligence\" OR \"machine learning\" OR \"deep learning\" "
    "OR \"neural network\" OR \"neural networks\" OR convolutional "
    "OR \"computer vision\" OR \"large language model\" OR \"large language models\" "
    "OR \"radiomics\")"
)
PM_EXPERIMENT = (
    "(\"animal model\" OR \"animal models\" OR \"large animal\" OR experimental "
    "OR \"in vivo\" OR \"ex vivo\" OR \"isolated heart\" OR Langendorff "
    "OR \"in silico\" OR \"computational model\" OR \"computational modeling\" "
    "OR mechanistic OR pathophysiolog* OR cardiomyocyte*)"
)
PM_MODEL_SPECIES = (
    "(canine OR dog OR dogs OR porcine OR swine OR pig OR pigs OR ovine OR sheep "
    "OR feline OR cat OR rabbit OR rabbits)"
)

# (topic_id, axis, query, retmax)
TOPIC_QUERIES: list[tuple[str, str, str, int]] = [
    ("cardiomyopathy", "vet_cardio",
     '(cardiomyopathy OR HCM OR DCM OR ARVC OR "restrictive cardiomyopathy") AND ' + PM_SPECIES, 30),
    ("mmvd", "vet_cardio",
     '("myxomatous mitral valve" OR MMVD OR "mitral regurgitation" OR "degenerative valve") AND ' + PM_SPECIES, 30),
    ("heart_failure", "vet_cardio",
     '("heart failure" OR "congestive heart failure" OR pimobendan OR furosemide OR torsemide '
     'OR spironolactone) AND ' + PM_SPECIES, 30),
    ("pulmonary_hypertension", "vet_cardio",
     '("pulmonary hypertension" OR sildenafil OR tadalafil) AND ' + PM_SPECIES, 20),
    ("congenital", "vet_cardio",
     '("patent ductus arteriosus" OR "pulmonic stenosis" OR "subaortic stenosis" '
     'OR "congenital heart") AND ' + PM_SPECIES, 20),
    ("myocarditis_endocarditis", "vet_cardio",
     '(myocarditis OR endocarditis OR pericardial OR "pericardial effusion") AND ' + PM_SPECIES, 20),
    ("thromboembolism", "vet_cardio",
     '("arterial thromboembolism" OR thromboembolism OR thrombosis OR clopidogrel '
     'OR rivaroxaban) AND ' + PM_SPECIES, 20),
    ("ecg", "vet_cardio",
     '(electrocardiography OR arrhythmia OR Holter OR ECG OR "atrial fibrillation") AND ' + PM_SPECIES, 30),
    ("imaging", "vet_cardio",
     '(echocardiography OR "speckle tracking" OR strain OR "cardiac MRI" OR "cardiac CT" '
     'OR "vertebral heart" OR VHS OR VLAS OR "thoracic radiograph") AND ' + PM_SPECIES, 30),
    ("biomarkers_bp", "vet_cardio",
     '("NT-proBNP" OR troponin OR "systemic hypertension" OR "blood pressure") AND ' + PM_SPECIES, 20),
    # --- ИИ ---
    ("ai_vet", "ai",
     PM_AI + " AND (veterinary OR " + PM_SPECIES + ")", 40),
    ("ai_cardio_imaging", "ai",
     PM_AI + ' AND (echocardiograph* OR electrocardiogra* OR "cardiac magnetic resonance" '
     'OR "chest radiograph" OR "cardiac imaging") AND (clinical OR implementation OR validation '
     'OR deployment OR "external validation")', 30),
    # --- Патофизиология ---
    ("pathophys_large_animal", "pathophys",
     PM_CARDIO + " AND " + PM_MODEL_SPECIES + " AND " + PM_EXPERIMENT, 40),
    ("pathophys_in_silico", "pathophys",
     '("in silico" OR "computational model" OR "digital twin" OR simulation) AND '
     '(ventricular OR atrial OR cardiac OR myocardial) AND (electrophysiolog* OR arrhythm* '
     'OR mechanics OR hemodynam*)', 25),
]

# ---------------------------------------------------------------------------
# Semantic Scholar bulk-запросы (синтаксис: + AND, | OR, "фраза")
# ---------------------------------------------------------------------------

S2_QUERIES: list[tuple[str, str, str]] = [
    ("s2_vet_cardio", "vet_cardio",
     '(dog | dogs | canine | cat | cats | feline) + (cardiomyopathy | echocardiography | '
     '"mitral valve" | arrhythmia | "pulmonary hypertension" | "heart failure" | myocarditis | '
     'thromboembolism | electrocardiography | "heart disease")'),
    ("s2_ai_vet", "ai",
     '("deep learning" | "machine learning" | "artificial intelligence" | "neural network" | '
     '"computer vision") + (veterinary | canine | feline | dog | cat)'),
    ("s2_ai_cardio", "ai",
     '("deep learning" | "artificial intelligence") + (echocardiography | electrocardiogram | '
     '"cardiac magnetic resonance" | "chest radiograph") + (validation | implementation | clinical)'),
    ("s2_pathophys", "pathophys",
     '(canine | porcine | swine | ovine | "large animal" | "animal model") + '
     '(myocardial | cardiac | "heart failure" | arrhythmia | hemodynamic) + '
     '(experimental | "in vivo" | "ex vivo" | mechanism)'),
    ("s2_in_silico", "pathophys",
     '("in silico" | "computational model" | "digital twin") + (cardiac | ventricular | atrial) + '
     '(electrophysiology | arrhythmia | mechanics)'),
]
S2_FIELDS = ("title,abstract,externalIds,venue,year,authors,publicationDate,journal,"
             "openAccessPdf,url")
S2_PER_QUERY_CAP = 120

# ---------------------------------------------------------------------------
# Регулярки для эвристического пред-ранжирования
# ---------------------------------------------------------------------------

RE_CARDIO = re.compile(
    r"\b(heart|cardi\w*|myocard\w*|echocardiograph\w*|arrhythm\w*|electrocardiogra\w*|"
    r"ecg|valv\w*|mitral|aortic|pulmonary hypertension|thromboemb\w*|hemodynam\w*|"
    r"haemodynam\w*|ventric\w*|atri(al|um)|vhs|vlas|nt-probnp|troponin)\b", re.I)
RE_AI = re.compile(
    r"\b(artificial intelligence|machine learning|deep learning|neural networks?|"
    r"convolutional|computer vision|transformer|large language models?|radiomics|"
    r"yolo\w*|segmentation|classifier)\b", re.I)
RE_EXPERIMENT = re.compile(
    r"\b(animal models?|large animal|experimental|in vivo|ex vivo|isolated heart|"
    r"langendorff|in silico|computational model\w*|digital twin|cardiomyocytes?|"
    r"mechanis\w+|pathophysiolog\w*)\b", re.I)
RE_PET = re.compile(r"\b(dogs?|canine|cats?|feline|puppies|puppy|kittens?|"
                    r"companion animals?|small animals?)\b", re.I)
RE_FARM_ONLY = re.compile(r"\b(horses?|equine|bovine|cattle|cows?|calves|poultry|"
                          r"chickens?|broilers?|goats?|fish|shrimp|bees?)\b", re.I)

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

def setup_logging(tag: str = "run") -> logging.Logger:
    logger = logging.getLogger("vetdigest")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{tag}_{date_str}.log", maxBytes=5 * 1024 * 1024,
        backupCount=12, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# Хранилище состояния (общий доступ с vetdigest_feedback.py)
# ---------------------------------------------------------------------------

@contextmanager
def state_lock():
    LOCK_FILE.touch(exist_ok=True)
    with LOCK_FILE.open("r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Модель статьи
# ---------------------------------------------------------------------------

@dataclass
class Article:
    pmid: str | None = None
    doi: str | None = None
    s2_id: str | None = None
    title: str = ""
    abstract: str = ""
    journal: str = ""
    year: str = ""
    month: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    authors: list[str] = field(default_factory=list)
    sources: set[str] = field(default_factory=set)   # pubmed / s2
    axis_hint: str = ""
    topic_hint: str = ""
    oa_pdf: str = ""
    s2_url: str = ""
    prescore: float = 0.0

    # от LLM
    score: int | None = None
    axis: str | None = None
    title_ru: str | None = None
    abstract_ru: str | None = None

    def uid(self) -> str:
        if self.doi:
            return f"doi:{self.doi.lower()}"
        if self.pmid:
            return f"pmid:{self.pmid}"
        return "t:" + norm_title_hash(self.title)

    def key(self) -> str:
        """Короткий ключ для callback_data Telegram (≤ 64 байт)."""
        return hashlib.sha1(self.uid().encode("utf-8")).hexdigest()[:12]

    def link(self) -> str:
        if self.doi:
            return f"https://doi.org/{self.doi}"
        if self.pmid:
            return f"https://pubmed.ncbi.nlm.nih.gov/{self.pmid}/"
        return self.s2_url or ""


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def norm_title_hash(t: str) -> str:
    return hashlib.sha1(norm_title(t).encode("utf-8")).hexdigest()[:16]


class Budget:
    def __init__(self, seconds: int) -> None:
        self.deadline = time.monotonic() + seconds

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0


def http_get(url: str, params: dict | None, headers: dict | None,
             logger: logging.Logger, retries: int = 3,
             backoff: tuple = (20, 45, 90),
             budget: Budget | None = None) -> requests.Response | None:
    for attempt in range(retries):
        if budget and budget.expired():
            return None
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        except requests.RequestException as exc:
            logger.warning("GET %s: сетевая ошибка %s (попытка %d)", url, exc, attempt + 1)
            if attempt < retries - 1:
                time.sleep(backoff[min(attempt, len(backoff) - 1)])
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = backoff[min(attempt, len(backoff) - 1)]
            if attempt < retries - 1:
                logger.warning("GET %s → HTTP %d, жду %d с (попытка %d)",
                               url, resp.status_code, wait, attempt + 1)
                time.sleep(wait)
            else:
                logger.warning("GET %s → HTTP %d, попытки исчерпаны", url, resp.status_code)
            continue
        return resp
    return None


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

NCBI_EMAIL = ""
NCBI_TOOL = "VetRadiometryDigest"
NCBI_API_KEY = ""


def _ncbi_params(extra: dict) -> dict:
    p = {"tool": NCBI_TOOL, "email": NCBI_EMAIL}
    if NCBI_API_KEY:
        p["api_key"] = NCBI_API_KEY
    p.update(extra)
    return p


def pubmed_esearch(query: str, mindate: str, maxdate: str, logger: logging.Logger,
                   retmax: int, budget: Budget) -> list[str]:
    params = _ncbi_params({
        "db": "pubmed", "term": query, "sort": "date", "retmode": "json",
        "retmax": retmax, "datetype": "edat",
        "mindate": mindate.replace("-", "/"), "maxdate": maxdate.replace("-", "/"),
    })
    resp = http_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                    params, None, logger, budget=budget)
    if resp is None or resp.status_code != 200:
        return []
    try:
        return resp.json().get("esearchresult", {}).get("idlist", [])
    except Exception:
        return []


MONTHS = {"jan": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr", "may": "May",
          "jun": "Jun", "jul": "Jul", "aug": "Aug", "sep": "Sep", "oct": "Oct",
          "nov": "Nov", "dec": "Dec"}


def pubmed_efetch(pmids: list[str], logger: logging.Logger, budget: Budget) -> list[Article]:
    if not pmids:
        return []
    resp = http_get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                    _ncbi_params({"db": "pubmed", "id": ",".join(pmids),
                                  "rettype": "abstract", "retmode": "xml"}),
                    None, logger, budget=budget)
    if resp is None or resp.status_code != 200:
        return []
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        logger.warning("PubMed efetch parse error: %s", exc)
        return []

    out: list[Article] = []
    for pub in root.iter("PubmedArticle"):
        a = Article(sources={"pubmed"})
        a.pmid = (pub.findtext(".//MedlineCitation/PMID") or "").strip() or None
        for aid in pub.findall(".//PubmedData/ArticleIdList/ArticleId"):
            if aid.attrib.get("IdType") == "doi" and aid.text:
                a.doi = aid.text.strip()
            if aid.attrib.get("IdType") == "pmc" and aid.text:
                a.oa_pdf = f"https://pmc.ncbi.nlm.nih.gov/articles/{aid.text.strip()}/"
        t_el = pub.find(".//ArticleTitle")
        a.title = " ".join("".join(t_el.itertext()).split()) if t_el is not None else ""
        parts = []
        for ab in pub.findall(".//Abstract/AbstractText"):
            label = ab.attrib.get("Label")
            txt = " ".join("".join(ab.itertext()).split())
            if txt:
                parts.append(f"{label}: {txt}" if label else txt)
        a.abstract = "\n".join(parts).strip()
        a.journal = pub.findtext(".//Journal/ISOAbbreviation") or \
            pub.findtext(".//Journal/Title") or ""
        a.year = pub.findtext(".//JournalIssue/PubDate/Year") or \
            (pub.findtext(".//JournalIssue/PubDate/MedlineDate") or "")[:4]
        m = (pub.findtext(".//JournalIssue/PubDate/Month") or "")[:3].lower()
        a.month = MONTHS.get(m, "")
        a.volume = pub.findtext(".//JournalIssue/Volume") or ""
        a.issue = pub.findtext(".//JournalIssue/Issue") or ""
        a.pages = pub.findtext(".//Pagination/MedlinePgn") or ""
        for au in pub.findall(".//AuthorList/Author"):
            ln = au.findtext("LastName") or ""
            ini = au.findtext("Initials") or ""
            coll = au.findtext("CollectiveName") or ""
            if ln:
                a.authors.append(f"{ln} {ini}".strip())
            elif coll:
                a.authors.append(coll)
        out.append(a)
    return out


def journal_query(journal: str, group: str) -> str:
    j = f'"{journal}"[Journal]'
    if group == "vet":
        if journal in VET_TAKE_ALL:
            return j
        return f"{j} AND ({PM_CARDIO} OR {PM_AI})"
    if group == "human":
        if journal == "Circ Res":
            return j
        if journal == "N Engl J Med":
            return f"{j} AND {PM_CARDIO} AND (\"Journal Article\"[pt] NOT \"Comment\"[pt])"
        return f"{j} AND ({PM_AI} OR {PM_EXPERIMENT} OR {PM_MODEL_SPECIES})"
    # fundamental
    return f"{j} AND (heart OR cardiac OR cardiomyocyte* OR myocard* OR arrhythm*)"


def collect_from_pubmed(mindate: str, maxdate: str, budget: Budget,
                        logger: logging.Logger, counters: dict) -> list[Article]:
    hint: dict[str, tuple[str, str]] = {}   # pmid -> (axis, topic)
    pmids: list[str] = []
    sleep = 0.12 if NCBI_API_KEY else 0.4

    def add(ids: list[str], axis: str, topic: str):
        for p in ids:
            if p not in hint:
                hint[p] = (axis, topic)
                pmids.append(p)

    for topic, axis, q, retmax in TOPIC_QUERIES:
        if budget.expired():
            break
        ids = pubmed_esearch(q, mindate, maxdate, logger, retmax, budget)
        logger.info("PubMed[topic:%s]: %d PMID", topic, len(ids))
        add(ids, axis, topic)
        time.sleep(sleep)

    for group, journals in (("vet", VET_JOURNALS), ("human", HUMAN_CARDIO_JOURNALS),
                            ("fund", FUNDAMENTAL_JOURNALS)):
        for j in journals:
            if budget.expired():
                break
            ids = pubmed_esearch(journal_query(j, group), mindate, maxdate, logger, 25, budget)
            if ids:
                logger.info("PubMed[journal:%s]: %d PMID", j, len(ids))
            axis = "vet_cardio" if group == "vet" else "pathophys"
            add(ids, axis, f"journal:{j}")
            time.sleep(sleep)

    counters["pubmed_pmids"] = len(pmids)
    logger.info("PubMed: уникальных PMID = %d", len(pmids))

    arts: list[Article] = []
    for i in range(0, len(pmids), 100):
        if budget.expired():
            break
        chunk = pmids[i:i + 100]
        for a in pubmed_efetch(chunk, logger, budget):
            if a.pmid in hint:
                a.axis_hint, a.topic_hint = hint[a.pmid]
            arts.append(a)
        time.sleep(sleep)
    logger.info("PubMed efetch: %d записей", len(arts))
    return arts


# ---------------------------------------------------------------------------
# Semantic Scholar
# ---------------------------------------------------------------------------

def collect_from_s2(mindate: str, maxdate: str, budget: Budget,
                    logger: logging.Logger, counters: dict) -> list[Article]:
    key = os.getenv("S2_API_KEY", "").strip()
    headers = {"User-Agent": f"{NCBI_TOOL}/{VERSION} (mailto:{NCBI_EMAIL})"}
    if key:
        headers["x-api-key"] = key
    out: list[Article] = []
    failures = 0
    for qid, axis, q in S2_QUERIES:
        if budget.expired() or failures >= 2:
            if failures >= 2:
                logger.warning("Semantic Scholar: 2 неудачи подряд, пропускаю остаток")
            break
        params = {"query": q, "fields": S2_FIELDS,
                  "publicationDateOrYear": f"{mindate}:{maxdate}"}
        resp = http_get("https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                        params, headers, logger, retries=3, backoff=(15, 40, 80),
                        budget=budget)
        if resp is None or resp.status_code != 200:
            failures += 1
            logger.warning("S2[%s]: нет ответа (%s)", qid,
                           resp.status_code if resp is not None else "timeout/429")
            time.sleep(3)
            continue
        failures = 0
        try:
            data = resp.json().get("data", []) or []
        except ValueError:
            data = []
        n = 0
        for p in data[:S2_PER_QUERY_CAP]:
            ab = (p.get("abstract") or "").strip()
            if not ab:
                continue
            ext = p.get("externalIds") or {}
            j = p.get("journal") or {}
            a = Article(sources={"s2"}, axis_hint=axis, topic_hint=qid)
            a.s2_id = p.get("paperId")
            a.doi = (ext.get("DOI") or "").strip() or None
            a.pmid = str(ext.get("PubMed")) if ext.get("PubMed") else None
            a.title = " ".join((p.get("title") or "").split())
            a.abstract = ab
            a.journal = j.get("name") or p.get("venue") or ""
            a.volume = (j.get("volume") or "").strip()
            pg = (j.get("pages") or "").strip()
            a.pages = pg.replace(" ", "")
            a.year = str(p.get("year") or "")
            pd = p.get("publicationDate") or ""
            if len(pd) >= 7:
                a.month = list(MONTHS.values())[int(pd[5:7]) - 1]
            a.authors = [au.get("name", "") for au in (p.get("authors") or []) if au.get("name")]
            oa = p.get("openAccessPdf") or {}
            a.oa_pdf = oa.get("url") or ""
            a.s2_url = p.get("url") or ""
            out.append(a)
            n += 1
        logger.info("S2[%s]: %d статей с абстрактом (из %d)", qid, n, len(data))
        time.sleep(1.5 if not key else 1.05)
    counters["s2"] = len(out)
    return out


# ---------------------------------------------------------------------------
# Слияние, фильтр, пред-ранжирование
# ---------------------------------------------------------------------------

def merge_articles(arts: list[Article]) -> list[Article]:
    by_key: dict[str, Article] = {}
    order: list[Article] = []
    for a in arts:
        keys = [k for k in (
            f"doi:{a.doi.lower()}" if a.doi else None,
            f"pmid:{a.pmid}" if a.pmid else None,
            "t:" + norm_title_hash(a.title) if a.title else None) if k]
        existing = next((by_key[k] for k in keys if k in by_key), None)
        if existing is None:
            order.append(a)
            for k in keys:
                by_key[k] = a
            continue
        # дополняем существующую запись (PubMed приоритетнее)
        existing.sources |= a.sources
        for attr in ("doi", "pmid", "s2_id", "oa_pdf", "s2_url"):
            if not getattr(existing, attr) and getattr(a, attr):
                setattr(existing, attr, getattr(a, attr))
        if len(a.abstract) > len(existing.abstract) and "pubmed" not in existing.sources:
            existing.abstract = a.abstract
        for k in keys:
            by_key.setdefault(k, existing)
    return order


def is_priority_journal(journal: str) -> bool:
    j = norm_title(journal)
    for pj in ALL_PRIORITY_JOURNALS:
        if norm_title(pj) == j:
            return True
    return False


def prefilter_and_rank(arts: list[Article]) -> list[Article]:
    kept = []
    for a in arts:
        blob = f"{a.title} {a.abstract}"
        cardio = bool(RE_CARDIO.search(blob))
        ai = bool(RE_AI.search(blob))
        exp = bool(RE_EXPERIMENT.search(blob))
        pet = bool(RE_PET.search(blob))
        farm = bool(RE_FARM_ONLY.search(blob))
        if len(a.abstract) < 80:
            continue
        if not (cardio or (ai and pet)):
            continue
        if farm and not pet and not exp:
            continue
        s = 0.0
        if a.journal.lower().startswith("j vet cardiol"):
            s += 5
        if is_priority_journal(a.journal):
            s += 2
        if pet:
            s += 3
        if cardio:
            s += 2
        if ai:
            s += 2
        if exp:
            s += 1
        if len(a.sources) > 1:
            s += 1
        a.prescore = s
        kept.append(a)
    kept.sort(key=lambda x: -x.prescore)
    return kept


# ---------------------------------------------------------------------------
# Оценки пользователя → подсказки для LLM
# ---------------------------------------------------------------------------

def rating_examples(limit: int = 8) -> tuple[list[str], list[str]]:
    ratings = load_json(RATINGS_FILE, {})
    items = sorted(ratings.values(), key=lambda r: r.get("ts", 0), reverse=True)
    good = [f"{r.get('title','')} ({r.get('journal','')})" for r in items if r.get("rating") == 2]
    bad = [f"{r.get('title','')} ({r.get('journal','')})" for r in items if r.get("rating") == 0]
    return good[:limit], bad[:limit]


def last_week_rating_summary() -> str:
    ratings = load_json(RATINGS_FILE, {})
    since = time.time() - 8 * 86400
    recent = [r for r in ratings.values() if r.get("ts", 0) >= since]
    if not recent:
        return ""
    c = {2: 0, 1: 0, 0: 0}
    for r in recent:
        c[r.get("rating", 1)] = c.get(r.get("rating", 1), 0) + 1
    return f"Ваши оценки за неделю: 👍 {c[2]} · 🤔 {c[1]} · 👎 {c[0]}"


# ---------------------------------------------------------------------------
# Sonar
# ---------------------------------------------------------------------------

BASE_PROMPT = (
    "Ты — научный ассистент ветеринарного кардиолога. Его интересы: "
    "(1) клиническая ветеринарная кардиология собак и кошек (кардиомиопатии, MMVD, "
    "лёгочная гипертензия, аритмии, ЭхоКГ, рентгенография сердца, биомаркеры); "
    "(2) внедрение ИИ/ML в кардиологию и ветеринарную визуализацию (детекция опорных "
    "точек, VHS/VLAS, анализ ЭхоКГ и ЭКГ, валидация и клиническое внедрение моделей); "
    "(3) патофизиологические эксперименты на сердце: крупные животные модели (собака, "
    "свинья, овца), ex vivo/in vivo, механика миокарда, электрофизиология, in silico "
    "моделирование аритмий. "
    "По названию и абстракту статьи верни JSON: "
    "score — целое 0–10, насколько статья полезна ему (9–10: ключевая для его практики "
    "или исследований; 6–8: полезная; 3–5: косвенная; 0–2: нерелевантна, например "
    "только лошади/КРС/птица, онкология без сердца, хирургия опорно-двигательного аппарата); "
    "axis — vet_cardio | ai | pathophys; "
    "title_ru — перевод названия на русский; "
    "abstract_ru — ПОЛНЫЙ профессиональный перевод абстракта на русский без сокращений, "
    "с сохранением структуры разделов (Введение/Цель, Материалы и методы, Результаты, "
    "Выводы), чисел, единиц и статистики. Аббревиатуры HCM, DCM, ARVC, MMVD, VHS, VLAS, "
    "NT-proBNP, LA/Ao, ECG, AI/ML оставляй как есть. Ничего не придумывай сверх абстракта. "
    "Ответ — только JSON, без markdown."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "axis": {"type": "string", "enum": AXIS_ORDER},
        "title_ru": {"type": "string"},
        "abstract_ru": {"type": "string"},
    },
    "required": ["score", "axis", "title_ru", "abstract_ru"],
}


def build_system_prompt() -> str:
    good, bad = rating_examples()
    extra = ""
    if good:
        extra += "\nСтатьи, которые пользователь оценил как полезные:\n- " + "\n- ".join(good)
    if bad:
        extra += "\nСтатьи, которые пользователь оценил как бесполезные:\n- " + "\n- ".join(bad)
    if extra:
        extra = ("\n\nУчитывай предпочтения пользователя при выставлении score "
                 "(похожее на полезное — выше, на бесполезное — ниже):" + extra)
    return BASE_PROMPT + extra


def _parse_json_loose(content: str) -> dict | None:
    content = content.strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        return json.loads(content)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


class SonarFatal(RuntimeError):
    pass


def llm_annotate(a: Article, pplx_key: str, system_prompt: str,
                 logger: logging.Logger, payload_log: Path, cost: dict) -> bool:
    body = {
        "model": PPLX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Journal: {a.journal}\nTitle: {a.title}\n\n"
                                        f"Abstract:\n{a.abstract[:6000]}"},
        ],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "paper", "schema": RESPONSE_SCHEMA}},
        "disable_search": True,
        "web_search_options": {"search_context_size": "low"},
        "temperature": 0.1,
        "max_tokens": 3500,
    }
    headers = {"Authorization": f"Bearer {pplx_key}", "Content-Type": "application/json"}
    for attempt in range(3):
        try:
            resp = requests.post(PPLX_ENDPOINT, json=body, headers=headers,
                                 timeout=(CONNECT_TIMEOUT, 120))
        except requests.RequestException as exc:
            logger.warning("Sonar: сетевая ошибка %s (попытка %d)", exc, attempt + 1)
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code in (401, 402, 403):
            logger.error("Sonar HTTP %d: %s", resp.status_code, resp.text[:200])
            raise SonarFatal(f"sonar_{resp.status_code}")
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = (20, 45, 90)[attempt]
            logger.warning("Sonar HTTP %d, жду %d с", resp.status_code, wait)
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            logger.warning("Sonar HTTP %d: %s", resp.status_code, resp.text[:300])
            return False
        try:
            data = resp.json()
        except ValueError:
            continue
        with payload_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"uid": a.uid(), "response": data}, ensure_ascii=False) + "\n")
        try:
            usd = (data.get("usage") or {}).get("cost", {}).get("total_cost")
            if isinstance(usd, (int, float)):
                cost["usd"] = cost.get("usd", 0.0) + usd
        except AttributeError:
            pass
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError):
            continue
        parsed = _parse_json_loose(content)
        if not parsed or "abstract_ru" not in parsed:
            logger.warning("Sonar: невалидный JSON (finish=%s), повтор %d",
                           choice.get("finish_reason"), attempt + 1)
            continue
        try:
            a.score = max(0, min(10, int(parsed.get("score", 0))))
        except (TypeError, ValueError):
            a.score = 0
        ax = parsed.get("axis")
        a.axis = ax if ax in AXES else (a.axis_hint or "vet_cardio")
        a.title_ru = (parsed.get("title_ru") or "").strip()
        a.abstract_ru = (parsed.get("abstract_ru") or "").strip()
        return bool(a.abstract_ru)
    return False


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def nlm_bibliography(a: Article) -> str:
    authors = ", ".join(a.authors[:6])
    if len(a.authors) > 6:
        authors += ", et al"
    parts = [authors + "." if authors else "",
             f"{a.title.rstrip('.')}." if a.title else "",
             f"{a.journal}." if a.journal else ""]
    tail = " ".join(x for x in [a.year, a.month] if x)
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


def split_plain(text: str, limit: int) -> list[str]:
    """Режет простой текст на куски ≤ limit по абзацам/предложениям."""
    text = text.strip()
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    units = re.split(r"(?<=\n)|(?<=[.!?])\s+", text)
    for u in units:
        if not u:
            continue
        if len(u) > limit:
            for i in range(0, len(u), limit):
                piece = u[i:i + limit]
                if cur:
                    out.append(cur.strip())
                    cur = ""
                out.append(piece.strip())
            continue
        if len(cur) + len(u) + 1 > limit:
            out.append(cur.strip())
            cur = u
        else:
            cur = (cur + " " + u) if cur and not cur.endswith("\n") else cur + u
    if cur.strip():
        out.append(cur.strip())
    return out


def pack_blocks(blocks: list[str], limit: int = TG_LIMIT) -> list[str]:
    msgs, cur = [], ""
    for b in blocks:
        if cur and len(cur) + 2 + len(b) > limit:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def links_line(a: Article, ncbi_email: str) -> str:
    links = []
    if a.doi:
        links.append(f'🔗 <a href="https://doi.org/{esc(a.doi)}">Оригинал</a>')
    if a.pmid:
        links.append(f'📖 <a href="https://pubmed.ncbi.nlm.nih.gov/{esc(a.pmid)}/">PubMed</a>')
    if a.oa_pdf:
        links.append(f'📄 <a href="{esc(a.oa_pdf)}">Полный текст</a>')
    elif a.doi and ncbi_email:
        links.append(f'🔓 <a href="https://api.unpaywall.org/v2/{esc(a.doi)}?email={esc(ncbi_email)}">Unpaywall</a>')
    if not links and a.s2_url:
        links.append(f'🔗 <a href="{esc(a.s2_url)}">Semantic Scholar</a>')
    return " · ".join(links)


def abstract_blocks(a: Article) -> list[str]:
    return [esc(p) for p in split_plain(a.abstract_ru or a.abstract, 3000)]


def main_card_messages(a: Article, ncbi_email: str) -> list[str]:
    head = f"{AXES.get(a.axis or '', '')} · {a.score}/10\n<b>{esc(a.title)}</b>"
    if a.title_ru:
        head += f"\n{esc(a.title_ru)}"
    blocks = [head,
              "📚 <b>Библиография (NLM):</b>\n" + esc(nlm_bibliography(a)),
              "🌐 <b>Абстракт (RU):</b>"]
    ab = abstract_blocks(a)
    blocks[-1] += "\n" + ab[0]
    blocks += ab[1:]
    ll = links_line(a, ncbi_email)
    if ll:
        blocks.append(ll)
    return pack_blocks(blocks)


def secondary_card_messages(a: Article) -> list[str]:
    head = f"<b>{esc(a.title_ru or a.title)}</b>"
    if a.title_ru and a.title_ru != a.title:
        head += f"\n{esc(a.title)}"
    link = a.link()
    if link:
        head += f'\n<a href="{esc(link)}">{esc(link)}</a>'
    ab = abstract_blocks(a)
    blocks = [head, ab[0]] + ab[1:]
    return pack_blocks(blocks)


def rating_keyboard(key: str, chosen: str | None = None) -> dict:
    row = []
    for val, label in RATING_BUTTONS:
        text = f"✅ {label}" if chosen == val else label
        row.append({"text": text, "callback_data": f"r|{key}|{val}"})
    return {"inline_keyboard": [row]}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_call(token: str, method: str, payload: dict, logger: logging.Logger,
            retries: int = 3) -> dict | None:
    url = f"{TG_API_ROOT}/bot{token}/{method}"
    for attempt in range(retries):
        try:
            resp = requests.post(url, json=payload, timeout=(8, 40))
        except requests.RequestException as exc:
            logger.warning("Telegram %s: сетевая ошибка %s", method, exc)
            time.sleep(3 * (attempt + 1))
            continue
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and data.get("ok"):
            return data
        if resp.status_code == 429:
            retry = (data.get("parameters") or {}).get("retry_after", 5)
            logger.warning("Telegram 429, жду %d с", retry)
            time.sleep(retry + 1)
            continue
        logger.error("Telegram %s HTTP %d: %s", method, resp.status_code, resp.text[:300])
        return None
    return None


def tg_send(token: str, chat_id: str, text: str, logger: logging.Logger,
            keyboard: dict | None = None) -> int | None:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    thread = os.getenv("SECONDARY_THREAD_ID", "").strip()
    if thread and str(chat_id) == os.getenv("SECONDARY_CHAT_ID", "").strip():
        payload["message_thread_id"] = int(thread)
    if keyboard:
        payload["reply_markup"] = keyboard
    data = tg_call(token, "sendMessage", payload, logger)
    if data:
        return data["result"]["message_id"]
    return None


# ---------------------------------------------------------------------------
# Проверка доступов
# ---------------------------------------------------------------------------

def run_check(cfg: dict, logger: logging.Logger) -> int:
    ok = True
    print(f"VetDigest v{VERSION} — проверка\n")
    if not cfg["tg_token"]:
        print("✗ TELEGRAM_BOT_TOKEN пуст")
        return 1
    me = tg_call(cfg["tg_token"], "getMe", {}, logger, retries=1)
    if me:
        print(f"✓ Telegram-бот: @{me['result'].get('username')}")
    else:
        print("✗ Telegram getMe не прошёл — токен неверный")
        return 1
    wh = tg_call(cfg["tg_token"], "getWebhookInfo", {}, logger, retries=1)
    if wh and wh["result"].get("url"):
        print(f"✗ У бота установлен webhook ({wh['result']['url']}) — значит этот токен "
              "уже использует другой сервис. Кнопки оценки работать не смогут, "
              "нужен отдельный бот для дайджеста.")
        ok = False
    else:
        print("✓ Webhook не установлен")
    for name in ("tg_chat", "tg_chat2"):
        cid = cfg[name]
        label = "основной чат" if name == "tg_chat" else "второй чат"
        if not cid:
            print(f"– {label}: не задан")
            continue
        r = tg_call(cfg["tg_token"], "getChat", {"chat_id": cid}, logger, retries=1)
        if r:
            c = r["result"]
            th = os.getenv("SECONDARY_THREAD_ID", "").strip() if name == "tg_chat2" else ""
            print(f"✓ {label}: {cid} ({c.get('type')}, "
                  f"{c.get('title') or c.get('username') or c.get('first_name')})"
                  + (f", тема {th}" if th else ""))
        else:
            print(f"✗ {label}: {cid} недоступен — бот не добавлен или пользователь не нажал /start")
            ok = False
    if cfg["pplx_key"]:
        r = requests.post(PPLX_ENDPOINT, headers={"Authorization": f"Bearer {cfg['pplx_key']}"},
                          json={"model": PPLX_MODEL, "max_tokens": 32, "disable_search": True,
                                "messages": [{"role": "user", "content": "ping"}]},
                          timeout=(8, 60))
        print(("✓" if r.status_code == 200 else "✗") + f" Perplexity Sonar: HTTP {r.status_code}"
              + ("" if r.status_code == 200 else f" — {r.text[:200]}"))
        ok &= r.status_code == 200
    else:
        print("✗ PPLX_API_KEY пуст")
        ok = False
    print(("✓" if NCBI_API_KEY else "–") + " NCBI_API_KEY " + ("задан" if NCBI_API_KEY else "не задан"))
    try:
        r = requests.get("https://api.semanticscholar.org/graph/v1/paper/search/bulk",
                         params={"query": "canine cardiomyopathy", "fields": "title"},
                         timeout=(8, 30))
        print(("✓" if r.status_code == 200 else "!") + f" Semantic Scholar: HTTP {r.status_code}")
    except requests.RequestException as exc:
        print(f"! Semantic Scholar: {exc}")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Основной пайплайн
# ---------------------------------------------------------------------------

def load_cfg() -> dict:
    load_dotenv(BASE_DIR / ".env")
    global NCBI_EMAIL, NCBI_TOOL, NCBI_API_KEY
    NCBI_EMAIL = os.getenv("NCBI_EMAIL", "").strip()
    NCBI_TOOL = os.getenv("NCBI_TOOL", "VetRadiometryDigest").strip()
    NCBI_API_KEY = os.getenv("NCBI_API_KEY", "").strip()
    return {
        "pplx_key": (os.getenv("PPLX_API_KEY") or os.getenv("PERPLEXITY_API_KEY") or "").strip(),
        "tg_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        "tg_chat": os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        "tg_chat2": os.getenv("SECONDARY_CHAT_ID", "").strip(),
        # 1 = во второй чат не сразу, а очередью по отдельному таймеру (vetdigest-secondary.timer)
        "chat2_delayed": os.getenv("SECONDARY_DELAYED", "1").strip() not in ("0", "false", "no", ""),
    }


def send_secondary_queue(cfg: dict, logger: logging.Logger) -> int:
    """Отправка отложенного выпуска во второй чат (таймер vetdigest-secondary)."""
    with state_lock():
        q = load_json(SECONDARY_QUEUE_FILE, {})
    cards = q.get("cards") or []
    if not cards:
        logger.info("Очередь второго чата пуста — отправлять нечего")
        return 0
    chat = cfg["tg_chat2"] or q.get("chat", "")
    if not (cfg["tg_token"] and chat):
        logger.error("Не задан TELEGRAM_BOT_TOKEN или SECONDARY_CHAT_ID")
        return 2
    logger.info("Второй чат %s: отправляю %d карточек выпуска %s", chat, len(cards), q.get("date"))
    sent = 0
    left: list[list[str]] = []
    for idx, msgs in enumerate(cards):
        ok = True
        for m in msgs:
            if tg_send(cfg["tg_token"], chat, m, logger) is None:
                ok = False
                break
            time.sleep(0.35)
        if ok:
            sent += 1
        else:
            left = cards[idx:]
            break
    with state_lock():
        if left:
            save_json(SECONDARY_QUEUE_FILE, {**q, "cards": left})
        else:
            SECONDARY_QUEUE_FILE.unlink(missing_ok=True)
    logger.info("Второй чат: отправлено %d/%d", sent, len(cards))
    return 0 if not left else 1


def resend_secondary(cfg: dict, logger: logging.Logger, date: str = "") -> int:
    """Повторная отправка выпуска во второй чат из архива data/digests/digest_<date>.md."""
    files = sorted(DIGEST_DIR.glob("digest_*.md"))
    if date:
        files = [f for f in files if f.stem == f"digest_{date}"]
    if not files:
        logger.error("Архив выпуска не найден в %s", DIGEST_DIR)
        return 2
    src = files[-1]
    text = src.read_text(encoding="utf-8")
    cards: list[list[str]] = []
    for block in text.split("\n## ")[1:]:
        lines = block.split("\n")
        a = Article(title=lines[0].strip())
        a.title_ru = (lines[1].strip() if len(lines) > 1 else "") or None
        rest = lines[2:]
        try:
            i = next(n for n, l in enumerate(rest) if l.startswith("Ось:"))
        except StopIteration:
            i = -1
        body = rest[i + 1:] if i >= 0 else rest
        body = [l for l in body]
        while body and not body[-1].strip():
            body.pop()
        link = ""
        if body and body[-1].strip().startswith("http"):
            link = body.pop().strip()
        # первая непустая строка после «Ось:» — библиография NLM
        while body and not body[0].strip():
            body.pop(0)
        if body:
            body.pop(0)
        a.abstract_ru = "\n".join(body).strip() or None
        if link.startswith("https://doi.org/"):
            a.doi = link[len("https://doi.org/"):]
        elif "pubmed.ncbi.nlm.nih.gov/" in link:
            a.pmid = link.rstrip("/").rsplit("/", 1)[-1]
        else:
            a.s2_url = link
        cards.append(secondary_card_messages(a))
    if not cards:
        logger.error("В %s нет статей", src.name)
        return 2
    with state_lock():
        q = load_json(SECONDARY_QUEUE_FILE, {})
        if q.get("cards"):
            logger.info("В очереди уже есть %d неотправленных карточек — отправляю их", len(q["cards"]))
        else:
            save_json(SECONDARY_QUEUE_FILE, {"date": src.stem[7:], "chat": cfg["tg_chat2"], "cards": cards})
            logger.info("Из %s собрано карточек: %d", src.name, len(cards))
    return send_secondary_queue(cfg, logger)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-s2", action="store_true")
    ap.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
    ap.add_argument("--send-secondary", action="store_true",
                    help="отправить во второй чат выпуск, отложенный утренним прогоном")
    ap.add_argument("--resend-secondary", nargs="?", const="", default=None, metavar="ДАТА",
                    help="повторно отправить во второй чат выпуск из архива (последний или ГГГГ-ММ-ДД)")
    args = ap.parse_args()

    cfg = load_cfg()
    logger = setup_logging("check" if args.check else
                           ("secondary" if args.send_secondary or args.resend_secondary is not None else "run"))
    if args.check:
        return run_check(cfg, logger)
    if args.send_secondary:
        return send_secondary_queue(cfg, logger)
    if args.resend_secondary is not None:
        return resend_secondary(cfg, logger, args.resend_secondary)

    logger.info("=== VetDigest v%s (dry_run=%s) ===", VERSION, args.dry_run)

    if not args.dry_run:
        missing = [n for n, v in (("PPLX_API_KEY", cfg["pplx_key"]),
                                  ("TELEGRAM_BOT_TOKEN", cfg["tg_token"]),
                                  ("TELEGRAM_CHAT_ID", cfg["tg_chat"])) if not v]
        if missing:
            logger.error("Не заданы переменные: %s", ", ".join(missing))
            return 2
        if not tg_call(cfg["tg_token"], "getMe", {}, logger, retries=2):
            logger.error("Telegram getMe не прошёл — токен неверный, прерываю ДО сбора")
            return 3

    budget = Budget(GLOBAL_BUDGET_SEC)
    today = datetime.now(timezone.utc).date()
    maxdate = today.strftime("%Y-%m-%d")
    window = args.window_days
    counters: dict = {}

    def collect(win: int) -> list[Article]:
        mind = (today - timedelta(days=win)).strftime("%Y-%m-%d")
        logger.info("Окно поиска: %s .. %s", mind, maxdate)
        arts: list[Article] = []
        try:
            arts += collect_from_pubmed(mind, maxdate, budget, logger, counters)
        except Exception as exc:
            logger.exception("PubMed: %s", exc)
        if not args.no_s2:
            try:
                arts += collect_from_s2(mind, maxdate, budget, logger, counters)
            except Exception as exc:
                logger.exception("Semantic Scholar: %s", exc)
        return arts

    raw = collect(window)
    sent = set(load_json(SENT_PAPERS_FILE, {}).get("uids", []))

    def prepare(raw_list: list[Article]) -> list[Article]:
        merged = merge_articles(raw_list)
        logger.info("После слияния PubMed+S2: %d", len(merged))
        ranked = prefilter_and_rank(merged)
        logger.info("После тематического фильтра: %d", len(ranked))
        fresh = [a for a in ranked if a.uid() not in sent]
        logger.info("После исключения уже отправленных: %d", len(fresh))
        return fresh

    cands = prepare(raw)
    if len(cands) < MIN_ARTICLES_BEFORE_WIDEN and window == DEFAULT_WINDOW_DAYS:
        window = FALLBACK_WINDOW_DAYS
        logger.info("Мало кандидатов, расширяю окно до %d дней", window)
        cands = prepare(raw + collect(window))

    to_llm = cands[:MAX_ARTICLES_TO_LLM]
    logger.info("В Sonar отправляю: %d", len(to_llm))

    cost: dict = {}
    payload_log = PAYLOAD_DIR / f"pplx_{maxdate}.jsonl"
    annotated: list[Article] = []
    if cfg["pplx_key"] and not args.no_llm:
        sp = build_system_prompt()
        for i, a in enumerate(to_llm):
            if budget.expired():
                logger.warning("Бюджет времени истёк: обработано %d/%d", i, len(to_llm))
                break
            try:
                if llm_annotate(a, cfg["pplx_key"], sp, logger, payload_log, cost):
                    annotated.append(a)
            except SonarFatal:
                logger.error("Sonar недоступен (ключ/баланс) — останавливаю LLM")
                break
            time.sleep(0.3)
    else:
        for a in to_llm:
            a.score, a.axis = 7, a.axis_hint or "vet_cardio"
            a.abstract_ru = a.abstract
            annotated.append(a)

    selected = [a for a in annotated if (a.score or 0) >= MIN_SCORE]
    selected.sort(key=lambda x: (-(x.score or 0), -x.prescore))
    selected = selected[:MAX_ARTICLES_IN_DIGEST]
    selected.sort(key=lambda x: (AXIS_ORDER.index(x.axis) if x.axis in AXIS_ORDER else 9,
                                 -(x.score or 0)))
    logger.info("Отобрано к доставке: %d (порог %d/10). Стоимость Sonar ≈ $%.3f",
                len(selected), MIN_SCORE, cost.get("usd", 0.0))

    # --- Markdown-архив ---
    date_ru = today.strftime("%d.%m.%Y")
    md = [f"# Дайджест {date_ru}", ""]
    for a in selected:
        md += [f"## {a.title}", f"{a.title_ru or ''}", "",
               f"Ось: {AXES.get(a.axis or '', '')} · {a.score}/10", "",
               nlm_bibliography(a), "", a.abstract_ru or "", "", a.link(), ""]
    (DIGEST_DIR / f"digest_{maxdate}.md").write_text("\n".join(md), encoding="utf-8")

    counts = {ax: sum(1 for a in selected if a.axis == ax) for ax in AXIS_ORDER}
    header = (f"📋 <b>Научный дайджест · {date_ru}</b>\n"
              + " · ".join(f"{AXES[ax]}: {counts[ax]}" for ax in AXIS_ORDER)
              + f"\nОкно: {window} дн. · PubMed + Semantic Scholar")
    rs = last_week_rating_summary()
    if rs:
        header += f"\n{rs}"
    header += "\n\nОцените каждую находку кнопками под карточкой."

    if args.dry_run:
        logger.info("--dry-run: в Telegram не отправляю")
        for a in selected:
            m1 = main_card_messages(a, NCBI_EMAIL)
            m2 = secondary_card_messages(a)
            logger.info("  [%s %d/10] %s | осн.: %d сообщ. (%s) | второй: %d сообщ.",
                        a.axis, a.score or 0, a.title[:80], len(m1),
                        ",".join(str(len(x)) for x in m1), len(m2))
        return 0

    if not selected:
        tg_send(cfg["tg_token"], cfg["tg_chat"],
                f"📋 <b>Научный дайджест · {date_ru}</b>\n\nЗа неделю подходящих публикаций не найдено.",
                logger)
        return 0

    tg_send(cfg["tg_token"], cfg["tg_chat"], header, logger)

    chat2_ok = bool(cfg["tg_chat2"])
    if chat2_ok and not tg_call(cfg["tg_token"], "getChat", {"chat_id": cfg["tg_chat2"]},
                                logger, retries=1):
        logger.error("Второй чат %s недоступен — шлю только в основной", cfg["tg_chat2"])
        chat2_ok = False

    delivered = 0
    queue2: list[list[str]] = []
    for a in selected:
        msgs = main_card_messages(a, NCBI_EMAIL)
        msg_id = None
        for i, m in enumerate(msgs):
            kb = rating_keyboard(a.key()) if i == len(msgs) - 1 else None
            msg_id = tg_send(cfg["tg_token"], cfg["tg_chat"], m, logger, kb)
            if msg_id is None:
                break
            time.sleep(0.35)
        if msg_id is None:
            logger.error("Не удалось отправить карточку: %s", a.title[:80])
            continue
        delivered += 1

        if chat2_ok:
            if cfg["chat2_delayed"]:
                queue2.append(secondary_card_messages(a))
            else:
                for m in secondary_card_messages(a):
                    if tg_send(cfg["tg_token"], cfg["tg_chat2"], m, logger) is None:
                        break
                    time.sleep(0.35)

        with state_lock():
            cards = load_json(CARDS_FILE, {})
            cards[a.key()] = {"uid": a.uid(), "title": a.title, "journal": a.journal,
                              "axis": a.axis, "score": a.score, "date": maxdate,
                              "message_id": msg_id}
            save_json(CARDS_FILE, cards)
            sd = load_json(SENT_PAPERS_FILE, {"uids": [], "history": []})
            sd.setdefault("uids", []).append(a.uid())
            sd["uids"] = sorted(set(sd["uids"]))
            save_json(SENT_PAPERS_FILE, sd)

    with state_lock():
        sd = load_json(SENT_PAPERS_FILE, {"uids": [], "history": []})
        sd.setdefault("history", []).append({"date": maxdate, "sent": [a.uid() for a in selected]})
        save_json(SENT_PAPERS_FILE, sd)
        st = load_json(STATS_FILE, {"runs": []})
        st.setdefault("runs", []).append({
            "date": maxdate, "version": VERSION, "window_days": window,
            "counters": counters, "llm": len(to_llm), "annotated": len(annotated),
            "selected": len(selected), "delivered": delivered,
            "sonar_usd": round(cost.get("usd", 0.0), 4)})
        save_json(STATS_FILE, st)

    if queue2:
        with state_lock():
            prev = load_json(SECONDARY_QUEUE_FILE, {}).get("cards") or []  # недоотправленное ранее
            save_json(SECONDARY_QUEUE_FILE, {"date": maxdate, "chat": cfg["tg_chat2"], "cards": prev + queue2})
        logger.info("Второй чат: %d карточек поставлено в очередь (отправка --send-secondary)", len(queue2))

    logger.info("✅ Доставлено %d/%d карточек", delivered, len(selected))
    return 0 if delivered == len(selected) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        logging.getLogger("vetdigest").exception("Фатальная ошибка: %s", exc)
        sys.exit(1)
