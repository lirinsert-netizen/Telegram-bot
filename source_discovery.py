"""Source discovery, ingestion, knowledge-base and retrieval pipeline.

This module provides an extensible pipeline for finding, fetching, scoring
and storing web sources that ground the guest-bot AI answers.

Public surface used by guest_ai_service:
    GroundingSource, AnswerMetadata
    IngestionPipeline, DiscoveryPipeline
    KnowledgeBase
    BackgroundEnrichmentScheduler
    RetrievalLayer
    WikipediaProvider, FandomProvider,
    DuckDuckGoInstantProvider, DuckDuckGoHTMLProvider,
    NewsSearchProvider, RSSProvider
    _normalize_space, _truncate, _safe_href   (re-exported utilities)
"""
from __future__ import annotations

import datetime
import email.utils
import html as _html
import logging
import math
import re
import threading
import time
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

logger = logging.getLogger("guest_runtime.discovery")

# ── HTTP / search constants ────────────────────────────────────────────────────
_DDG_INSTANT_API_URL = "https://api.duckduckgo.com/"
_DDG_HTML_SEARCH_URL = "https://html.duckduckgo.com/html/"
_WIKIPEDIA_SEARCH_API_URL = "https://ru.wikipedia.org/w/api.php"
_WIKIPEDIA_PAGE_URL_TEMPLATE = "https://ru.wikipedia.org/wiki/{title}"

_SEARCH_TIMEOUT: tuple[float, float] = (8.0, 20.0)
_SOURCE_FETCH_TIMEOUT: tuple[float, float] = (8.0, 20.0)
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramBotGuestAI/1.0; +https://telegram.org)"
)

_MAX_SEARCH_RESULTS = 8
_MAX_SOURCE_SNIPPET_LEN = 420
_MAX_SOURCE_CONTENT_LEN = 1400
_MAX_RAW_HTML_LEN = 250_000
_MAX_HTML_SEARCH_TAIL_LEN = 1_400
_MAX_CHUNK_LEN = 450
_MAX_CHUNKS_PER_SOURCE = 6
_MAX_FETCHED_SOURCES = 4
_MAX_GROUNDING_SOURCES = 5

# ── TTL (seconds) ──────────────────────────────────────────────────────────────
_TTL_NEWS: float = 6 * 3_600       # 6 h
_TTL_WIKI: float = 7 * 86_400      # 7 d
_TTL_FANDOM: float = 3 * 86_400    # 3 d
_TTL_DEFAULT: float = 86_400       # 24 h

# ── Knowledge-base limits ──────────────────────────────────────────────────────
_KB_MAX_TOPICS = 200
_KB_MAX_SOURCES_PER_TOPIC = 8
_KB_MIN_SOURCES_FOR_REFRESH = 2
_KB_MAX_AGE_RATIO_FOR_REFRESH = 0.75   # refresh when TTL 75 % consumed
_BACKGROUND_REFRESH_INTERVAL = 20 * 60  # 20 min


# ── Enums ──────────────────────────────────────────────────────────────────────
class SourceType(Enum):
    SEARCH = "search"
    ENCYCLOPEDIA = "encyclopedia"
    FANDOM = "fandom"
    NEWS = "news"
    RSS = "rss"


# ── Core dataclasses ───────────────────────────────────────────────────────────
@dataclass
class GroundingSource:
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    provider: str = ""
    # enrichment fields
    source_type: SourceType = SourceType.SEARCH
    freshness_ts: float = 0.0        # unix timestamp of publication (0 = unknown)
    quality_score: float = 0.0       # 0..1 composite quality
    chunks: list[str] = field(default_factory=list)
    is_news: bool = False
    historical_hits: int = 0
    ingested_at: float = field(default_factory=time.time)

    @property
    def domain(self) -> str:
        parsed = urlparse(self.url)
        return parsed.netloc.lower().removeprefix("www.")


@dataclass
class AnswerMetadata:
    confidence: str = "medium"   # "high" | "medium" | "low"
    answer_type: str = "general" # "reference" | "news" | "general"
    controversial: bool = False


@dataclass
class _TopicRecord:
    sources: list[GroundingSource] = field(default_factory=list)
    query_count: int = 0
    last_queried: float = 0.0
    last_refreshed: float = 0.0


# ── HTML utilities (also re-exported for guest_ai_service) ────────────────────
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|section|article|h[1-6]|tr)\b[^>]*>",
    flags=re.IGNORECASE,
)
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>",
    flags=re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _safe_href(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if parsed.scheme.lower() in {"http", "https"}:
        return raw
    return ""


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _truncate(value: str, limit: int) -> str:
    text = _normalize_space(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _html_to_text(raw_html: str) -> str:
    html_value = str(raw_html or "")
    if not html_value:
        return ""
    cleaned = _COMMENT_RE.sub(" ", html_value)
    cleaned = _SCRIPT_STYLE_RE.sub(" ", cleaned)
    cleaned = _BLOCK_TAG_RE.sub("\n", cleaned)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = _html.unescape(cleaned)
    cleaned = re.sub(r"[ \t\f\v]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _extract_title_from_html(raw_html: str) -> str:
    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        raw_html or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    return _normalize_space(_html_to_text(match.group(1) if match else ""))


def _extract_meta_description(raw_html: str) -> str:
    patterns = (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html or "", flags=re.IGNORECASE | re.DOTALL)
        if match:
            return _truncate(_html_to_text(match.group(1)), _MAX_SOURCE_SNIPPET_LEN)
    return ""


def _extract_pub_date(raw_html: str) -> float:
    """Try to extract publication unix-timestamp from HTML meta/time tags."""
    patterns = (
        r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([\d\-T:Z+.]+)',
        r'<meta[^>]+content=["\']([\d\-T:Z+.]+)[^>]+property=["\']article:published_time["\']',
        r'<meta[^>]+name=["\']date["\'][^>]+content=["\']([\d\-T:Z+.]+)',
        r'<time[^>]+datetime=["\']([\d\-T:Z+.]+)',
    )
    for pattern in patterns:
        match = re.search(pattern, raw_html or "", flags=re.IGNORECASE)
        if not match:
            continue
        date_str = match.group(1).strip()
        # ISO 8601 variants
        for fmt in (
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M",
            "%Y-%m-%d",
        ):
            try:
                clean = date_str[: len(fmt)].replace("Z", "+00:00")
                dt = datetime.datetime.strptime(clean, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                return dt.timestamp()
            except (ValueError, TypeError):
                pass
        # Fallback: just YYYY-MM-DD
        try:
            parts = date_str[:10].split("-")
            if len(parts) == 3:
                dt = datetime.datetime(
                    int(parts[0]), int(parts[1]), int(parts[2]),
                    tzinfo=datetime.timezone.utc,
                )
                return dt.timestamp()
        except Exception:
            pass
    return 0.0


# ── URL / search helpers ───────────────────────────────────────────────────────
def _source_key(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


def _unwrap_search_url(url: str) -> str:
    raw = _html.unescape(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in {"duckduckgo.com", "html.duckduckgo.com"} and parsed.path.startswith("/l/"):
        query = parse_qs(parsed.query)
        for key in ("uddg", "rut"):
            values = query.get(key) or []
            if values:
                candidate = unquote(values[0]).strip()
                if _safe_href(candidate):
                    return candidate
    return _safe_href(raw)


def _iter_ddg_related_topics(items: list) -> list[dict]:
    found: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("Topics"), list):
            found.extend(_iter_ddg_related_topics(item.get("Topics") or []))
            continue
        found.append(item)
    return found


def _merge_sources(*groups: list[GroundingSource]) -> list[GroundingSource]:
    merged: list[GroundingSource] = []
    seen: set[str] = set()
    for group in groups:
        for source in group:
            url = _safe_href(source.url)
            if not url:
                continue
            key = _source_key(url)
            if not key or key in seen:
                continue
            seen.add(key)
            source.url = url
            merged.append(source)
            if len(merged) >= _MAX_SEARCH_RESULTS:
                return merged
    return merged


# ── Domain quality tiers ───────────────────────────────────────────────────────
_DOMAIN_QUALITY: dict[str, float] = {
    # encyclopedias
    "wikipedia.org": 1.0,
    "britannica.com": 0.95,
    "merriam-webster.com": 0.95,
    "encyclopediaofmath.org": 0.90,
    # major news
    "bbc.com": 0.90,
    "bbc.co.uk": 0.90,
    "reuters.com": 0.90,
    "apnews.com": 0.90,
    "nytimes.com": 0.85,
    "theguardian.com": 0.85,
    "ria.ru": 0.80,
    "tass.ru": 0.80,
    "interfax.ru": 0.78,
    "lenta.ru": 0.75,
    "rbc.ru": 0.75,
    "kommersant.ru": 0.75,
    "gazeta.ru": 0.72,
    "meduza.io": 0.72,
    "vedomosti.ru": 0.72,
    "cnn.com": 0.78,
    "nbcnews.com": 0.78,
    "washingtonpost.com": 0.82,
    "bloomberg.com": 0.83,
    "forbes.com": 0.75,
    "techcrunch.com": 0.75,
    # fandom / wikis
    "fandom.com": 0.75,
    "wikia.com": 0.72,
    "wiki.gg": 0.70,
    # science
    "arxiv.org": 0.88,
    "pubmed.ncbi.nlm.nih.gov": 0.88,
    "nature.com": 0.90,
    "sciencedirect.com": 0.85,
}

_NEWS_DOMAINS: frozenset[str] = frozenset({
    "bbc.com", "bbc.co.uk", "reuters.com", "apnews.com", "nytimes.com",
    "theguardian.com", "ria.ru", "tass.ru", "lenta.ru", "rbc.ru",
    "kommersant.ru", "gazeta.ru", "meduza.io", "interfax.ru", "vedomosti.ru",
    "cnn.com", "nbcnews.com", "foxnews.com", "washingtonpost.com",
    "forbes.com", "bloomberg.com", "techcrunch.com",
})

_ENCYCLOPEDIA_DOMAINS: frozenset[str] = frozenset({
    "wikipedia.org", "britannica.com", "merriam-webster.com",
    "encyclopediaofmath.org",
})

_FANDOM_DOMAINS: frozenset[str] = frozenset({
    "fandom.com", "wikia.com", "wiki.gg",
})

_BLOCKLIST_DOMAINS: frozenset[str] = frozenset({
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com",
    "youtube.com", "youtu.be", "vk.com", "ok.ru", "t.me", "telegram.me",
    "ads.google.com", "doubleclick.net", "google.com", "bing.com",
    "amazon.com", "ebay.com", "aliexpress.com",
})


def _get_domain_quality(domain: str) -> float:
    d = domain.lower().removeprefix("www.")
    if d in _DOMAIN_QUALITY:
        return _DOMAIN_QUALITY[d]
    parts = d.split(".")
    if len(parts) > 2:
        parent = ".".join(parts[-2:])
        if parent in _DOMAIN_QUALITY:
            return _DOMAIN_QUALITY[parent]
    if d.endswith(".edu") or d.endswith(".ac.uk"):
        return 0.70
    if d.endswith(".gov"):
        return 0.75
    if d.endswith(".org"):
        return 0.65
    return 0.50


def _classify_domain(domain: str) -> SourceType:
    d = domain.lower().removeprefix("www.")
    for enc_d in _ENCYCLOPEDIA_DOMAINS:
        if d == enc_d or d.endswith("." + enc_d):
            return SourceType.ENCYCLOPEDIA
    for fan_d in _FANDOM_DOMAINS:
        if d == fan_d or d.endswith("." + fan_d):
            return SourceType.FANDOM
    for news_d in _NEWS_DOMAINS:
        if d == news_d or d.endswith("." + news_d):
            return SourceType.NEWS
    return SourceType.SEARCH


# ── Fandom franchise detection ─────────────────────────────────────────────────
_FRANCHISE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(marvel|avengers?|iron\s*man|spider[- ]?man|thor\b|hulk|captain\s*america|капитан\s*америка|мстители)\b", re.I), "Marvel"),
    (re.compile(r"\b(batman|superman|wonder\s*woman|justice\s*league|лига\s*справедливости)\b", re.I), "DC Comics"),
    (re.compile(r"\b(harry\s*potter|гарри\s*поттер|hogwarts|хогвартс|dumbledore|voldemort|волдеморт)\b", re.I), "Harry Potter"),
    (re.compile(r"\b(star\s*wars?|звёздные\s*войны|звездные\s*войны|jedi|джедай|sith|ситх|luke\s*skywalker)\b", re.I), "Star Wars"),
    (re.compile(r"\b(pokemon|покемон|pikachu|пикачу|mewtwo|мьюту)\b", re.I), "Pokémon"),
    (re.compile(r"\b(naruto|наруто|sasuke|саске|hokage|хокаге)\b", re.I), "Naruto"),
    (re.compile(r"\b(one\s*piece|ван\s*пис|luffy|луффи|zoro|зоро)\b", re.I), "One Piece"),
    (re.compile(r"\b(attack\s*on\s*titan|атака\s*титанов|shingeki|шингеки|eren\b|эрен)\b", re.I), "Attack on Titan"),
    (re.compile(r"\b(dragon\s*ball|драгон\s*болл|goku|гоку|vegeta|вегета)\b", re.I), "Dragon Ball"),
    (re.compile(r"\b(game\s*of\s*thrones|игра\s*престолов|westeros|вестерос|stark\b|старк|lannister|ланнистер)\b", re.I), "Game of Thrones"),
    (re.compile(r"\b(lord\s*of\s*the\s*rings|властелин\s*колец|hobbit|хоббит|gandalf|гэндальф|frodo|фродо)\b", re.I), "Tolkien"),
    (re.compile(r"\b(witcher|ведьмак|geralt|геральт|ciri\b|цири)\b", re.I), "The Witcher"),
    (re.compile(r"\b(minecraft)\b", re.I), "Minecraft"),
    (re.compile(r"\b(fortnite)\b", re.I), "Fortnite"),
    (re.compile(r"\b(league\s*of\s*legends|лига\s*легенд)\b", re.I), "League of Legends"),
    (re.compile(r"\b(world\s*of\s*warcraft|warcraft|варкрафт)\b", re.I), "Warcraft"),
    (re.compile(r"\b(final\s*fantasy)\b", re.I), "Final Fantasy"),
    (re.compile(r"\b(sonic\b|соник)\b", re.I), "Sonic"),
    (re.compile(r"\b(doctor\s*who|доктор\s*кто)\b", re.I), "Doctor Who"),
    (re.compile(r"\b(star\s*trek|звёздный\s*путь)\b", re.I), "Star Trek"),
    (re.compile(r"\b(elder\s*scrolls|skyrim|скайрим|morrowind|oblivion)\b", re.I), "The Elder Scrolls"),
    (re.compile(r"\b(fallout|фоллаут)\b", re.I), "Fallout"),
    (re.compile(r"\b(overwatch|овервотч)\b", re.I), "Overwatch"),
    (re.compile(r"\b(genshin|геншин)\b", re.I), "Genshin Impact"),
    (re.compile(r"\b(transformers|трансформеры)\b", re.I), "Transformers"),
    (re.compile(r"\b(digimon|дигимон)\b", re.I), "Digimon"),
    (re.compile(r"\b(bleach\b|блич|ichigo|ичиго)\b", re.I), "Bleach"),
    (re.compile(r"\b(fairy\s*tail|хвост\s*феи)\b", re.I), "Fairy Tail"),
    (re.compile(r"\b(my\s*little\s*pony|мой\s*маленький\s*пони)\b", re.I), "My Little Pony"),
    (re.compile(r"\b(halo\b)\b", re.I), "Halo"),
]


def _detect_franchise(query: str) -> str | None:
    """Return franchise name if the query matches a known franchise, else None."""
    for pattern, franchise in _FRANCHISE_PATTERNS:
        if pattern.search(query):
            return franchise
    return None


# ── Abstract provider ──────────────────────────────────────────────────────────
class SourceProvider(ABC):
    source_type: SourceType = SourceType.SEARCH
    priority: int = 50  # lower = higher priority in pipeline

    @abstractmethod
    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]: ...


# ── Concrete providers ─────────────────────────────────────────────────────────
class DuckDuckGoInstantProvider(SourceProvider):
    source_type = SourceType.SEARCH
    priority = 30

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        try:
            response = session.get(
                _DDG_INSTANT_API_URL,
                params={
                    "q": query,
                    "format": "json",
                    "no_redirect": "1",
                    "no_html": "1",
                    "skip_disambig": "0",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("[DISCOVERY] DDG instant failed: %s", e)
            return []

        results: list[GroundingSource] = []
        abstract_url = _safe_href(str(data.get("AbstractURL") or ""))
        abstract_text = _truncate(str(data.get("AbstractText") or ""), _MAX_SOURCE_SNIPPET_LEN)
        heading = _normalize_space(str(data.get("Heading") or ""))
        if abstract_url and (heading or abstract_text):
            st = _classify_domain(urlparse(abstract_url).netloc.lower().removeprefix("www."))
            results.append(GroundingSource(
                title=heading or urlparse(abstract_url).netloc,
                url=abstract_url,
                snippet=abstract_text,
                provider="duckduckgo_instant",
                source_type=st,
            ))

        for item in _iter_ddg_related_topics(data.get("RelatedTopics") or []):
            text = _truncate(str(item.get("Text") or ""), _MAX_SOURCE_SNIPPET_LEN)
            url = _safe_href(str(item.get("FirstURL") or ""))
            if not url or not text:
                continue
            title = text.split(" - ", 1)[0].strip() or urlparse(url).netloc
            st = _classify_domain(urlparse(url).netloc.lower().removeprefix("www."))
            results.append(GroundingSource(
                title=title, url=url, snippet=text,
                provider="duckduckgo_instant", source_type=st,
            ))
            if len(results) >= _MAX_SEARCH_RESULTS:
                break
        return results


class DuckDuckGoHTMLProvider(SourceProvider):
    source_type = SourceType.SEARCH
    priority = 40

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        try:
            response = session.get(_DDG_HTML_SEARCH_URL, params={"q": query}, timeout=timeout)
            response.raise_for_status()
            html_text = response.text
        except Exception as e:
            logger.warning("[DISCOVERY] DDG HTML failed: %s", e)
            return []

        results: list[GroundingSource] = []
        matches = list(re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text, flags=re.IGNORECASE | re.DOTALL,
        ))
        if not matches:
            matches = list(re.finditer(
                r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</a>',
                html_text, flags=re.IGNORECASE | re.DOTALL,
            ))

        for match in matches:
            url = _unwrap_search_url(match.group(1))
            title = _truncate(_html_to_text(match.group(2)), 180)
            if not url or not title:
                continue
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            if domain in _BLOCKLIST_DOMAINS:
                continue
            tail = html_text[match.end(): match.end() + _MAX_HTML_SEARCH_TAIL_LEN]
            snippet_match = re.search(
                r'(?:result__snippet|result-snippet)[^>]*>(.*?)</',
                tail, flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = _truncate(
                _html_to_text(snippet_match.group(1) if snippet_match else ""),
                _MAX_SOURCE_SNIPPET_LEN,
            )
            st = _classify_domain(domain)
            results.append(GroundingSource(
                title=title, url=url, snippet=snippet,
                provider="duckduckgo_html", source_type=st,
            ))
            if len(results) >= _MAX_SEARCH_RESULTS:
                break
        return results


class WikipediaProvider(SourceProvider):
    source_type = SourceType.ENCYCLOPEDIA
    priority = 10

    def __init__(self, lang: str = "ru") -> None:
        self._lang = lang

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        api_url = f"https://{self._lang}.wikipedia.org/w/api.php"
        page_tpl = f"https://{self._lang}.wikipedia.org/wiki/{{title}}"
        try:
            response = session.get(
                api_url,
                params={
                    "action": "query",
                    "list": "search",
                    "utf8": "1",
                    "format": "json",
                    "srlimit": "3",
                    "srsearch": query,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.warning("[DISCOVERY] Wikipedia search failed: %s", e)
            return []

        items = ((data or {}).get("query") or {}).get("search") or []
        results: list[GroundingSource] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = _normalize_space(str(item.get("title") or ""))
            if not title:
                continue
            snippet = _truncate(_html_to_text(str(item.get("snippet") or "")), _MAX_SOURCE_SNIPPET_LEN)
            url = page_tpl.format(title=quote(title.replace(" ", "_"), safe="_()"))
            results.append(GroundingSource(
                title=title, url=url, snippet=snippet,
                provider="wikipedia", source_type=SourceType.ENCYCLOPEDIA,
            ))
        return results


class FandomProvider(SourceProvider):
    """Search fandom.com/wikia for franchise-specific queries.

    Only activates when the query contains a recognized franchise signal.
    Results are ranked above general web results by the DiscoveryPipeline.
    """
    source_type = SourceType.FANDOM
    priority = 20

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        franchise = _detect_franchise(query)
        if not franchise:
            return []
        fandom_query = f"{franchise} {query} site:fandom.com"
        try:
            response = session.get(_DDG_HTML_SEARCH_URL, params={"q": fandom_query}, timeout=timeout)
            response.raise_for_status()
            html_text = response.text
        except Exception as e:
            logger.warning("[DISCOVERY] Fandom search failed: %s", e)
            return []

        results: list[GroundingSource] = []
        matches = list(re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text, flags=re.IGNORECASE | re.DOTALL,
        ))
        for match in matches:
            url = _unwrap_search_url(match.group(1))
            if not url:
                continue
            parsed_netloc = urlparse(url).netloc.lower()
            if "fandom.com" not in parsed_netloc and "wikia.com" not in parsed_netloc:
                continue
            title = _truncate(_html_to_text(match.group(2)), 180)
            tail = html_text[match.end(): match.end() + _MAX_HTML_SEARCH_TAIL_LEN]
            snippet_match = re.search(
                r'(?:result__snippet|result-snippet)[^>]*>(.*?)</',
                tail, flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = _truncate(
                _html_to_text(snippet_match.group(1) if snippet_match else ""),
                _MAX_SOURCE_SNIPPET_LEN,
            )
            results.append(GroundingSource(
                title=title or url, url=url, snippet=snippet,
                provider="fandom", source_type=SourceType.FANDOM,
            ))
            if len(results) >= 4:
                break
        return results


class NewsSearchProvider(SourceProvider):
    """Search news sites via DuckDuckGo HTML with a recency-biased query."""
    source_type = SourceType.NEWS
    priority = 35

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        year = datetime.datetime.now(datetime.timezone.utc).year
        news_query = f"{query} {year}"
        try:
            response = session.get(_DDG_HTML_SEARCH_URL, params={"q": news_query}, timeout=timeout)
            response.raise_for_status()
            html_text = response.text
        except Exception as e:
            logger.warning("[DISCOVERY] News search failed: %s", e)
            return []

        results: list[GroundingSource] = []
        matches = list(re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text, flags=re.IGNORECASE | re.DOTALL,
        ))
        for match in matches:
            url = _unwrap_search_url(match.group(1))
            if not url:
                continue
            domain = urlparse(url).netloc.lower().removeprefix("www.")
            if domain not in _NEWS_DOMAINS:
                continue
            title = _truncate(_html_to_text(match.group(2)), 180)
            tail = html_text[match.end(): match.end() + _MAX_HTML_SEARCH_TAIL_LEN]
            snippet_match = re.search(
                r'(?:result__snippet|result-snippet)[^>]*>(.*?)</',
                tail, flags=re.IGNORECASE | re.DOTALL,
            )
            snippet = _truncate(
                _html_to_text(snippet_match.group(1) if snippet_match else ""),
                _MAX_SOURCE_SNIPPET_LEN,
            )
            results.append(GroundingSource(
                title=title or url, url=url, snippet=snippet,
                provider="news_search", source_type=SourceType.NEWS, is_news=True,
            ))
            if len(results) >= 4:
                break
        return results


class RSSProvider(SourceProvider):
    """Probes a domain for RSS/Atom feeds and parses items into GroundingSources.

    The ``search`` method is a no-op (no general-query interface for RSS);
    use ``probe_domain`` explicitly when enriching a known news domain.
    """
    source_type = SourceType.RSS
    priority = 45

    _RSS_PATHS = (
        "/rss", "/feed", "/atom.xml", "/rss.xml",
        "/feed.xml", "/feeds/all.rss", "/feeds/posts/default",
    )

    def search(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        return []

    def probe_domain(
        self,
        base_url: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        """Try common feed paths on *base_url*'s domain."""
        parsed = urlparse(base_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in self._RSS_PATHS:
            url = base + path
            try:
                resp = session.get(url, timeout=(4.0, 8.0), allow_redirects=True)
                if resp.status_code != 200:
                    continue
                ctype = resp.headers.get("content-type", "")
                looks_like_feed = any(x in ctype for x in ("xml", "rss", "atom"))
                if not looks_like_feed and not resp.text.strip().startswith("<"):
                    continue
                items = self._parse_feed(resp.text, base_url)
                if items:
                    return items
            except Exception:
                continue
        return []

    def _parse_feed(self, feed_text: str, base_url: str) -> list[GroundingSource]:
        try:
            root = ET.fromstring(feed_text)
        except ET.ParseError:
            return []

        results: list[GroundingSource] = []
        tag_lower = root.tag.lower()

        # ── RSS 2.0 ───────────────────────────────────────────────────────────
        if "rss" in tag_lower or root.find("channel") is not None:
            channel = root.find("channel") or root
            for item in channel.findall("item")[:6]:
                title_el = item.find("title")
                link_el = item.find("link")
                desc_el = item.find("description")
                pub_el = item.find("pubDate")
                title = _normalize_space(title_el.text or "") if title_el is not None else ""
                url = _safe_href(_normalize_space(link_el.text or "") if link_el is not None else "")
                if not url:
                    continue
                snippet = _truncate(
                    _html_to_text(desc_el.text or "") if desc_el is not None else "",
                    _MAX_SOURCE_SNIPPET_LEN,
                )
                freshness_ts = 0.0
                if pub_el is not None and pub_el.text:
                    try:
                        freshness_ts = float(
                            email.utils.parsedate_to_datetime(pub_el.text).timestamp()
                        )
                    except Exception:
                        pass
                results.append(GroundingSource(
                    title=title or url, url=url, snippet=snippet,
                    provider="rss", source_type=SourceType.RSS,
                    is_news=True, freshness_ts=freshness_ts,
                ))

        # ── Atom ──────────────────────────────────────────────────────────────
        elif "feed" in tag_lower or root.find("{http://www.w3.org/2005/Atom}entry") is not None:
            atom_ns = "http://www.w3.org/2005/Atom"
            for entry in root.findall(f"{{{atom_ns}}}entry")[:6]:
                title_el = entry.find(f"{{{atom_ns}}}title")
                link_el = entry.find(f"{{{atom_ns}}}link")
                summary_el = entry.find(f"{{{atom_ns}}}summary") or entry.find(f"{{{atom_ns}}}content")
                updated_el = entry.find(f"{{{atom_ns}}}updated")
                title = _normalize_space(title_el.text or "") if title_el is not None else ""
                url = _safe_href(link_el.get("href", "") if link_el is not None else "")
                if not url:
                    continue
                snippet = _truncate(
                    _html_to_text(summary_el.text or "") if summary_el is not None else "",
                    _MAX_SOURCE_SNIPPET_LEN,
                )
                freshness_ts = 0.0
                if updated_el is not None and updated_el.text:
                    try:
                        freshness_ts = datetime.datetime.fromisoformat(
                            updated_el.text.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        pass
                results.append(GroundingSource(
                    title=title or url, url=url, snippet=snippet,
                    provider="rss_atom", source_type=SourceType.RSS,
                    is_news=True, freshness_ts=freshness_ts,
                ))

        return results


# ── Controversy detector ───────────────────────────────────────────────────────
_CONTROVERSY_RE = re.compile(
    r"\b(спорн|неоднознач|противоречив|дискуссионн|disputed|controversial|debated|contested)\w*\b",
    re.IGNORECASE,
)


# ── Freshness / ranking helpers ────────────────────────────────────────────────
def _compute_freshness_score(freshness_ts: float, is_news: bool) -> float:
    if not freshness_ts:
        return 0.60  # unknown → neutral
    age_secs = max(0.0, time.time() - freshness_ts)
    half_life = 3 * 3_600 if is_news else 30 * 86_400  # 3 h vs 30 d
    score = math.exp(-math.log(2) * age_secs / half_life)
    return round(min(1.0, max(0.0, score)), 3)


def _compute_relevance(query: str, source: GroundingSource) -> float:
    query_words = set(re.findall(r"\w+", query.lower()))
    stop = {
        "и", "в", "на", "что", "это", "а", "с", "для", "по", "не", "как",
        "the", "a", "an", "is", "of", "to", "in", "and", "or", "at",
    }
    query_words -= stop
    if not query_words:
        return 0.50
    haystack = f"{source.title} {source.snippet} {source.content}".lower()
    hay_words = set(re.findall(r"\w+", haystack))
    overlap = len(query_words & hay_words) / len(query_words)
    return round(min(1.0, overlap), 3)


def _rank_sources(query: str, sources: list[GroundingSource]) -> list[GroundingSource]:
    """Sort sources by a composite score: relevance, freshness, quality, completeness, history."""
    def _score(s: GroundingSource) -> float:
        relevance = _compute_relevance(query, s)
        freshness = _compute_freshness_score(s.freshness_ts, s.is_news)
        quality = s.quality_score if s.quality_score else _get_domain_quality(s.domain)
        completeness = min(1.0, (len(s.content) + len(s.snippet)) / 800.0)
        hist = min(1.0, s.historical_hits / 10.0)
        return (
            0.35 * relevance
            + 0.30 * freshness
            + 0.20 * quality
            + 0.10 * completeness
            + 0.05 * hist
        )

    return sorted(sources, key=_score, reverse=True)


# ── Ingestion pipeline ─────────────────────────────────────────────────────────
class IngestionPipeline:
    """Fetch, clean, chunk and quality-score a single GroundingSource."""

    def ingest(
        self,
        source: GroundingSource,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> GroundingSource | None:
        url = _safe_href(source.url)
        if not url:
            return None
        domain = urlparse(url).netloc.lower().removeprefix("www.")
        if domain in _BLOCKLIST_DOMAINS:
            return None

        try:
            response = session.get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
        except Exception as e:
            logger.debug("[INGESTION] fetch failed %s: %s", url, e)
            # Keep with existing snippet / content so the source isn't lost
            source.quality_score = self._compute_quality(source)
            return source

        final_url = _safe_href(response.url or url)
        if final_url:
            source.url = final_url

        content_type = str(response.headers.get("content-type") or "").lower()
        body_text = ""
        if "html" in content_type or "xml" in content_type or not content_type:
            raw_html = response.text[:_MAX_RAW_HTML_LEN]
            fetched_title = _extract_title_from_html(raw_html)
            fetched_snippet = _extract_meta_description(raw_html)
            body_text = _truncate(_html_to_text(raw_html), _MAX_SOURCE_CONTENT_LEN)
            if fetched_title:
                source.title = fetched_title
            if fetched_snippet and len(fetched_snippet) >= len(source.snippet):
                source.snippet = fetched_snippet
            if not source.freshness_ts:
                source.freshness_ts = _extract_pub_date(raw_html)
        else:
            body_text = _truncate(response.text, _MAX_SOURCE_CONTENT_LEN)

        source.content = body_text
        source.snippet = _truncate(source.snippet, _MAX_SOURCE_SNIPPET_LEN)
        source.chunks = self._chunk_text(body_text)
        source.is_news = source.is_news or domain in _NEWS_DOMAINS
        source.source_type = _classify_domain(domain)
        source.ingested_at = time.time()
        source.quality_score = self._compute_quality(source)
        return source

    def _chunk_text(self, text: str) -> list[str]:
        """Split *text* into sentence-aware chunks of ≤ _MAX_CHUNK_LEN chars."""
        if not text:
            return []
        sentences = re.split(r"(?<=[.!?…])\s+", text)
        chunks: list[str] = []
        current = ""
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(current) + len(sentence) + 1 <= _MAX_CHUNK_LEN:
                current = f"{current} {sentence}".strip() if current else sentence
            else:
                if current:
                    chunks.append(current)
                if len(sentence) > _MAX_CHUNK_LEN:
                    # Hard-split very long sentences
                    for i in range(0, len(sentence), _MAX_CHUNK_LEN):
                        part = sentence[i: i + _MAX_CHUNK_LEN].strip()
                        if part:
                            chunks.append(part)
                    current = ""
                else:
                    current = sentence
            if len(chunks) >= _MAX_CHUNKS_PER_SOURCE:
                current = ""
                break
        if current and len(chunks) < _MAX_CHUNKS_PER_SOURCE:
            chunks.append(current)
        return chunks

    def _compute_quality(self, source: GroundingSource) -> float:
        domain_q = _get_domain_quality(source.domain)
        text_len = len(source.content) + len(source.snippet)
        completeness = min(1.0, text_len / 800.0)
        freshness = _compute_freshness_score(source.freshness_ts, source.is_news)
        score = domain_q * 0.45 + completeness * 0.35 + freshness * 0.20
        return round(min(1.0, max(0.0, score)), 3)


# ── Answer classification ──────────────────────────────────────────────────────
def _classify_answer(sources: list[GroundingSource]) -> AnswerMetadata:
    if not sources:
        return AnswerMetadata(confidence="low", answer_type="general", controversial=False)

    news_count = sum(
        1 for s in sources if s.source_type in (SourceType.NEWS, SourceType.RSS) or s.is_news
    )
    enc_count = sum(
        1 for s in sources if s.source_type in (SourceType.ENCYCLOPEDIA, SourceType.FANDOM)
    )
    total = len(sources)
    avg_quality = sum(s.quality_score for s in sources) / total

    if avg_quality >= 0.65 and total >= 3:
        confidence = "high"
    elif total >= 1:
        confidence = "medium"
    else:
        confidence = "low"

    if news_count > enc_count and news_count > 0:
        answer_type = "news"
    elif enc_count > 0:
        answer_type = "reference"
    else:
        answer_type = "general"

    controversial = (
        sum(
            1 for s in sources
            if _CONTROVERSY_RE.search(s.snippet or s.content or "")
        ) >= 2
    )

    return AnswerMetadata(
        confidence=confidence,
        answer_type=answer_type,
        controversial=controversial,
    )


# ── Knowledge base ─────────────────────────────────────────────────────────────
def _normalize_topic(query: str) -> str:
    words = sorted(re.findall(r"\w+", query.lower()))
    return " ".join(words[:8])


def _source_ttl(source: GroundingSource) -> float:
    if source.source_type in (SourceType.NEWS, SourceType.RSS) or source.is_news:
        return _TTL_NEWS
    if source.source_type == SourceType.ENCYCLOPEDIA:
        return _TTL_WIKI
    if source.source_type == SourceType.FANDOM:
        return _TTL_FANDOM
    return _TTL_DEFAULT


class KnowledgeBase:
    """Thread-safe in-memory store of topics and their grounding sources."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._topics: dict[str, _TopicRecord] = {}

    def record_query(self, query: str) -> None:
        key = _normalize_topic(query)
        with self._lock:
            if key not in self._topics:
                self._topics[key] = _TopicRecord()
            rec = self._topics[key]
            rec.query_count += 1
            rec.last_queried = time.time()
            self._enforce_limits()

    def get_live_sources(self, query: str) -> list[GroundingSource]:
        """Return non-expired cached sources for *query*."""
        key = _normalize_topic(query)
        now = time.time()
        with self._lock:
            rec = self._topics.get(key)
            if not rec:
                return []
            return [s for s in rec.sources if (now - s.ingested_at) <= _source_ttl(s)]

    def update_topic(self, query: str, sources: list[GroundingSource]) -> None:
        key = _normalize_topic(query)
        now = time.time()
        with self._lock:
            rec = self._topics.setdefault(key, _TopicRecord())
            existing_keys: set[str] = {_source_key(s.url) for s in rec.sources}
            for s in sources:
                sk = _source_key(s.url)
                if not sk:
                    continue
                if sk not in existing_keys:
                    rec.sources.append(s)
                    existing_keys.add(sk)
                else:
                    # Update in-place
                    for i, es in enumerate(rec.sources):
                        if _source_key(es.url) == sk:
                            rec.sources[i] = s
                            break
            rec.sources = _rank_sources(key, rec.sources)[:_KB_MAX_SOURCES_PER_TOPIC]
            rec.last_refreshed = now

    def increment_source_hits(self, url: str) -> None:
        sk = _source_key(url)
        if not sk:
            return
        with self._lock:
            for rec in self._topics.values():
                for s in rec.sources:
                    if _source_key(s.url) == sk:
                        s.historical_hits += 1
                        return

    def topics_needing_refresh(self) -> list[str]:
        now = time.time()
        result: list[str] = []
        with self._lock:
            for key, rec in self._topics.items():
                if not rec.sources:
                    if rec.query_count > 0:
                        result.append(key)
                    continue
                live = sum(
                    1 for s in rec.sources
                    if (now - s.ingested_at) <= _source_ttl(s) * _KB_MAX_AGE_RATIO_FOR_REFRESH
                )
                if live < _KB_MIN_SOURCES_FOR_REFRESH:
                    result.append(key)
        return result

    def evict_expired(self) -> None:
        now = time.time()
        with self._lock:
            for rec in self._topics.values():
                # Hard evict at 2× TTL to retain a grace window
                rec.sources = [
                    s for s in rec.sources
                    if (now - s.ingested_at) <= _source_ttl(s) * 2
                ]

    def high_frequency_topics(self, top_n: int = 5) -> list[str]:
        with self._lock:
            return sorted(
                self._topics.keys(),
                key=lambda k: self._topics[k].query_count,
                reverse=True,
            )[:top_n]

    def _enforce_limits(self) -> None:
        if len(self._topics) <= _KB_MAX_TOPICS:
            return
        sorted_keys = sorted(
            self._topics.keys(),
            key=lambda k: (self._topics[k].query_count, self._topics[k].last_queried),
        )
        for k in sorted_keys[: len(self._topics) - _KB_MAX_TOPICS]:
            del self._topics[k]


# ── Discovery pipeline ─────────────────────────────────────────────────────────
class DiscoveryPipeline:
    """Orchestrates provider searches, ingestion and deduplication."""

    def __init__(
        self,
        providers: list[SourceProvider],
        ingestion: IngestionPipeline,
    ) -> None:
        self._providers = sorted(providers, key=lambda p: p.priority)
        self._ingestion = ingestion

    def discover_on_demand(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        """Run all providers for *query*, ingest and rank results."""
        franchise = _detect_franchise(query)
        if franchise:
            # Fandom providers first for franchise queries
            ordered = sorted(
                self._providers,
                key=lambda p: (0 if p.source_type == SourceType.FANDOM else 1, p.priority),
            )
        else:
            ordered = self._providers

        raw_groups: list[list[GroundingSource]] = []
        for provider in ordered:
            results = provider.search(query, session, timeout)
            if results:
                raw_groups.append(results)

        merged = _merge_sources(*raw_groups)
        if not merged:
            return []

        ingested: list[GroundingSource] = []
        for source in merged[:_MAX_FETCHED_SOURCES]:
            result = self._ingestion.ingest(source, session, timeout)
            if result and (result.snippet or result.content or result.chunks):
                ingested.append(result)

        leftover = [s for s in merged[_MAX_FETCHED_SOURCES:] if s.snippet]
        all_sources = ingested + leftover
        return _rank_sources(query, all_sources)[:_MAX_GROUNDING_SOURCES]

    def discover_proactive(
        self,
        topic_key: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> list[GroundingSource]:
        """Background enrichment pass for *topic_key*."""
        return self.discover_on_demand(topic_key, session, timeout)


# ── Background enrichment scheduler ───────────────────────────────────────────
class BackgroundEnrichmentScheduler:
    """Daemon thread that periodically refreshes stale topics in the KnowledgeBase."""

    def __init__(
        self,
        pipeline: DiscoveryPipeline,
        kb: KnowledgeBase,
        session: requests.Session,
        interval: float = _BACKGROUND_REFRESH_INTERVAL,
    ) -> None:
        self._pipeline = pipeline
        self._kb = kb
        self._session = session
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending: list[str] = []
        self._pending_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="GuestAIBackgroundEnrichment",
        )
        self._thread.start()
        logger.info("[SCHEDULER] started (interval=%ds)", int(self._interval))

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    def schedule_topic(self, topic: str) -> None:
        key = _normalize_topic(topic)
        with self._pending_lock:
            if key not in self._pending:
                self._pending.append(key)

    def _run(self) -> None:
        # 30-second grace period after startup
        if self._stop.wait(timeout=30.0):
            return
        while not self._stop.is_set():
            try:
                self._kb.evict_expired()
                with self._pending_lock:
                    pending = self._pending[:3]
                    del self._pending[:3]
                for key in pending:
                    if self._stop.is_set():
                        break
                    self._refresh_topic(key)
                stale = self._kb.topics_needing_refresh()
                # Prioritise high-frequency topics
                by_freq = sorted(
                    stale,
                    key=lambda k: self._kb._topics.get(k, _TopicRecord()).query_count,
                    reverse=True,
                )
                for key in by_freq[:2]:
                    if self._stop.is_set():
                        break
                    self._refresh_topic(key)
            except Exception as e:
                logger.warning("[SCHEDULER] cycle error: %s", e)
            self._stop.wait(timeout=self._interval)

    def _refresh_topic(self, topic_key: str) -> None:
        logger.debug("[SCHEDULER] proactive refresh topic=%r", topic_key)
        try:
            sources = self._pipeline.discover_proactive(
                topic_key, self._session, _SEARCH_TIMEOUT,
            )
            if sources:
                self._kb.update_topic(topic_key, sources)
                logger.info("[SCHEDULER] refreshed topic=%r sources=%d", topic_key, len(sources))
        except Exception as e:
            logger.warning("[SCHEDULER] refresh failed topic=%r: %s", topic_key, e)


# ── Multi-layer retrieval ──────────────────────────────────────────────────────
class RetrievalLayer:
    """Three-layer context gathering:
    1. Locally cached KB sources (fast, no network if fresh).
    2. Live on-demand discovery (always runs to get latest data).
    3. Fallback flag when both layers return nothing.
    """

    def __init__(
        self,
        pipeline: DiscoveryPipeline,
        kb: KnowledgeBase,
        scheduler: BackgroundEnrichmentScheduler,
    ) -> None:
        self._pipeline = pipeline
        self._kb = kb
        self._scheduler = scheduler

    def gather(
        self,
        query: str,
        session: requests.Session,
        timeout: tuple[float, float],
    ) -> tuple[list[GroundingSource], AnswerMetadata]:
        # Layer 1: cached
        kb_sources = self._kb.get_live_sources(query)
        # Layer 2: live discovery
        live_sources = self._pipeline.discover_on_demand(query, session, timeout)

        # Merge with deduplication; live sources take priority
        seen: set[str] = set()
        combined: list[GroundingSource] = []
        for s in live_sources + kb_sources:
            sk = _source_key(s.url)
            if sk and sk not in seen:
                seen.add(sk)
                combined.append(s)

        ranked = _rank_sources(query, combined)[:_MAX_GROUNDING_SOURCES]
        metadata = _classify_answer(ranked)

        # Update KB and schedule proactive enrichment when needed
        if live_sources:
            self._kb.record_query(query)
            self._kb.update_topic(query, live_sources)
            if len(live_sources) < _KB_MIN_SOURCES_FOR_REFRESH:
                self._scheduler.schedule_topic(query)

        return ranked, metadata
