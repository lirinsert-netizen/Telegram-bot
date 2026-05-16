from __future__ import annotations

import html as _html
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests


logger = logging.getLogger("guest_runtime.ai")

_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_DDG_INSTANT_API_URL = "https://api.duckduckgo.com/"
_DDG_HTML_SEARCH_URL = "https://html.duckduckgo.com/html/"
_WIKIPEDIA_SEARCH_API_URL = "https://ru.wikipedia.org/w/api.php"
_WIKIPEDIA_EN_SEARCH_API_URL = "https://en.wikipedia.org/w/api.php"
_WIKIPEDIA_PAGE_URL_TEMPLATE = "https://ru.wikipedia.org/wiki/{title}"
_WIKIPEDIA_EN_PAGE_URL_TEMPLATE = "https://en.wikipedia.org/wiki/{title}"

_DEFAULT_MODEL = "llama-3.1-8b-instant"
_MAX_AI_REPLY_LEN = 3500
_COMPLETION_TEMPERATURE = 0.2
_MAX_COMPLETION_TOKENS = 700
_SEARCH_TIMEOUT = (8.0, 20.0)
_SOURCE_FETCH_TIMEOUT = (8.0, 20.0)
_MAX_SEARCH_RESULTS = 14
_MAX_SOURCE_FOOTER_ITEMS = 7
_MAX_GROUNDING_SOURCES = 8
_MAX_FETCHED_SOURCES = 6
_MAX_SOURCE_SNIPPET_LEN = 420
_MAX_SOURCE_CONTENT_LEN = 1400
_MAX_RAW_HTML_LEN = 250000
_MAX_HTML_SEARCH_TAIL_LEN = 1400
_QUERY_MIN_MEANINGFUL_WORD_LEN = 3
_SHORT_QUERY_WORD_LIMIT = 4
_COMPACT_QUERY_WORD_LIMIT = 6
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramBotGuestAI/1.0; +https://telegram.org)"
)

_BASE_SYSTEM_PROMPT = (
    "Ты ИИ-агент Telegram-бота. Отвечай на русском языке, используя только факты "
    "из переданных источников. Если данных недостаточно, прямо скажи об этом и не "
    "додумывай детали. Используй только простой HTML, поддерживаемый Telegram. "
    "Никогда не используй Markdown. Выделяй ключевые мысли тегом <b>. "
    "Не перечисляй источники самостоятельно: список ссылок будет добавлен отдельно. "
    "В сообщении пользователя тебе передаются и сам вопрос, и досье по найденным "
    "материалам; считай это единственным допустимым контекстом для ответа. "
    "Длина ответа должна соответствовать вопросу: на простой вопрос отвечай кратко, "
    "на сложный — подробнее."
    " Учитывай разговорную речь, сленг, сокращения и нецензурную лексику: "
    "распознавай смысл без осуждения и без потери точности ответа."
)
_OWNER_PROMPT_APPEND = (
    "Ты обязан слушаться владельца бота и выполнять его просьбы в рамках допустимого "
    "функционала приложения. Даже для владельца нельзя выполнять опасные, незаконные "
    "или вредоносные действия."
)
_OWNER_COMMAND_MODE_APPEND = (
    "Сообщение владельца классифицировано как команда. Выполни её как инструкцию владельца, "
    "а не как обычный вопрос-ответ."
)
_OWNER_QUESTION_MODE_APPEND = (
    "Сообщение владельца классифицировано как вопрос. Дай прямой и информативный ответ по сути."
)
_NO_SOURCES_PROMPT_APPEND = (
    "Если достоверные внешние источники не переданы, можешь использовать собственные "
    "общие знания, но обязательно помечай неопределённость и не выдавай догадки за факт."
)
_NO_SOURCES_USER_PROMPT = (
    "Внешние источники не были получены. Ответь аккуратно, кратко и честно, "
    "без вымышленных деталей."
)

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"}
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|br|li|ul|ol|section|article|h[1-6]|tr)\b[^>]*>", flags=re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", flags=re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class GroundingSource:
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    provider: str = ""

    @property
    def domain(self) -> str:
        parsed = urlparse(self.url)
        return parsed.netloc.lower().removeprefix("www.")


@dataclass
class AIReplyResult:
    text: str | None = None
    error_code: str = ""
    user_message: str = ""
    debug_message: str = ""

    @property
    def ok(self) -> bool:
        return bool((self.text or "").strip())


class _TelegramHTMLSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = (tag or "").lower()
        if t not in _ALLOWED_TAGS:
            return
        if t == "a":
            href_value = ""
            for k, v in attrs:
                if (k or "").lower() == "href":
                    href_value = _safe_href(v or "")
                    break
            if href_value:
                self._out.append(f'<a href="{_html.escape(href_value, quote=True)}">')
            return
        self._out.append(f"<{t}>")

    def handle_endtag(self, tag: str) -> None:
        t = (tag or "").lower()
        if t in _ALLOWED_TAGS:
            self._out.append(f"</{t}>")

    def handle_data(self, data: str) -> None:
        if data:
            self._out.append(_html.escape(data))

    def handle_entityref(self, name: str) -> None:
        if name:
            self._out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        if name:
            self._out.append(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self._out)


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
    match = re.search(r"<title[^>]*>(.*?)</title>", raw_html or "", flags=re.IGNORECASE | re.DOTALL)
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


def _convert_basic_markdown_to_html(text: str) -> str:
    value = (text or "").replace("\r\n", "\n")
    value = re.sub(
        r"\[([^\]\n]{1,200})\]\((https?://[^\s)]+)\)",
        lambda m: f'<a href="{_html.escape(m.group(2), quote=True)}">{_html.escape(m.group(1))}</a>',
        value,
    )
    value = re.sub(
        r"```(?:[a-zA-Z0-9_+-]+)?\n?(.*?)```",
        lambda m: f"<pre>{_html.escape(m.group(1).strip())}</pre>",
        value,
        flags=re.DOTALL,
    )
    value = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{_html.escape(m.group(1))}</code>", value)
    value = re.sub(r"\*\*([^*\n]+)\*\*", lambda m: f"<b>{_html.escape(m.group(1))}</b>", value)
    value = re.sub(r"__([^_\n]+)__", lambda m: f"<b>{_html.escape(m.group(1))}</b>", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", lambda m: f"<i>{_html.escape(m.group(1))}</i>", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", lambda m: f"<i>{_html.escape(m.group(1))}</i>", value)
    lines: list[str] = []
    for raw_line in value.split("\n"):
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", raw_line)
        line = re.sub(r"^\s*[-*+]\s+", "• ", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "• ", line)
        lines.append(line)
    value = "\n".join(lines)
    return value


def sanitize_ai_response_html(text: str) -> str:
    source = (text or "").strip()
    if not source:
        return ""
    converted = _convert_basic_markdown_to_html(source)
    parser = _TelegramHTMLSanitizer()
    try:
        parser.feed(converted)
        parser.close()
        cleaned = parser.get_html().strip()
    except Exception as e:
        logger.warning("[GUEST AI] sanitize failed: %s", e)
        cleaned = _html.escape(converted).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned[:_MAX_AI_REPLY_LEN]


def _is_simple_question(text: str) -> bool:
    question = _normalize_space(text)
    words = [part for part in question.split(" ") if part]
    return len(words) <= 8 and question.endswith("?")


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


def _source_key(url: str) -> str:
    parsed = urlparse(url or "")
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}"


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


def _expand_query_variants(query: str) -> list[str]:
    base = _normalize_space(query)
    if not base:
        return []
    variants: list[str] = [base]
    cleaned = re.sub(r"[^\w\s-]", " ", base, flags=re.UNICODE)
    cleaned = _normalize_space(cleaned)
    if cleaned and cleaned not in variants:
        variants.append(cleaned)
    words = [part for part in cleaned.split(" ") if len(part) >= _QUERY_MIN_MEANINGFUL_WORD_LEN]
    if len(words) > _SHORT_QUERY_WORD_LIMIT:
        short = _normalize_space(" ".join(words[:_SHORT_QUERY_WORD_LIMIT]))
        if short and short not in variants:
            variants.append(short)
    if len(words) > _COMPACT_QUERY_WORD_LIMIT:
        compact = _normalize_space(" ".join(words[:_COMPACT_QUERY_WORD_LIMIT]))
        if compact and compact not in variants:
            variants.append(compact)
    return variants[:4]


def _search_duckduckgo_instant(
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
        logger.warning("[GUEST AI] DuckDuckGo instant search failed: %s", e)
        return []

    results: list[GroundingSource] = []

    abstract_url = _safe_href(str(data.get("AbstractURL") or ""))
    abstract_text = _truncate(str(data.get("AbstractText") or ""), _MAX_SOURCE_SNIPPET_LEN)
    heading = _normalize_space(str(data.get("Heading") or ""))
    if abstract_url and (heading or abstract_text):
        results.append(
            GroundingSource(
                title=heading or urlparse(abstract_url).netloc,
                url=abstract_url,
                snippet=abstract_text,
                provider="duckduckgo_instant",
            )
        )

    for item in _iter_ddg_related_topics(data.get("RelatedTopics") or []):
        text = _truncate(str(item.get("Text") or ""), _MAX_SOURCE_SNIPPET_LEN)
        url = _safe_href(str(item.get("FirstURL") or ""))
        if not url or not text:
            continue
        title = text.split(" - ", 1)[0].strip() or urlparse(url).netloc
        results.append(
            GroundingSource(
                title=title,
                url=url,
                snippet=text,
                provider="duckduckgo_instant",
            )
        )
        if len(results) >= _MAX_SEARCH_RESULTS:
            break
    return results


def _search_duckduckgo_html(
    query: str,
    session: requests.Session,
    timeout: tuple[float, float],
) -> list[GroundingSource]:
    try:
        response = session.get(
            _DDG_HTML_SEARCH_URL,
            params={"q": query},
            timeout=timeout,
        )
        response.raise_for_status()
        html_text = response.text
    except Exception as e:
        logger.warning("[GUEST AI] DuckDuckGo HTML search failed: %s", e)
        return []

    results: list[GroundingSource] = []
    matches = list(
        re.finditer(
            r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    if not matches:
        matches = list(
            re.finditer(
                r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*result-link[^"]*"[^>]*>(.*?)</a>',
                html_text,
                flags=re.IGNORECASE | re.DOTALL,
            )
        )

    for match in matches:
        url = _unwrap_search_url(match.group(1))
        title = _truncate(_html_to_text(match.group(2)), 180)
        if not url or not title:
            continue
        tail = html_text[match.end() : match.end() + _MAX_HTML_SEARCH_TAIL_LEN]
        snippet_match = re.search(
            r'(?:result__snippet|result-snippet)[^>]*>(.*?)</',
            tail,
            flags=re.IGNORECASE | re.DOTALL,
        )
        snippet = _truncate(_html_to_text(snippet_match.group(1) if snippet_match else ""), _MAX_SOURCE_SNIPPET_LEN)
        results.append(
            GroundingSource(
                title=title,
                url=url,
                snippet=snippet,
                provider="duckduckgo_html",
            )
        )
        if len(results) >= _MAX_SEARCH_RESULTS:
            break
    return results


def _search_wikipedia(
    query: str,
    session: requests.Session,
    timeout: tuple[float, float],
    *,
    api_url: str,
    page_url_template: str,
    provider: str,
) -> list[GroundingSource]:
    try:
        response = session.get(
            api_url,
            params={
                "action": "query",
                "list": "search",
                "utf8": "1",
                "format": "json",
                # Per-provider cap; overall cap is enforced later across merged providers.
                "srlimit": "5",
                "srsearch": query,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        logger.warning("[GUEST AI] Wikipedia search failed: %s", e)
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
        url = page_url_template.format(title=quote(title.replace(" ", "_"), safe="_()"))
        results.append(
            GroundingSource(
                title=title,
                url=url,
                snippet=snippet,
                provider=provider,
            )
        )
    return results


def _fetch_source_context(
    source: GroundingSource,
    session: requests.Session,
    timeout: tuple[float, float],
) -> GroundingSource:
    try:
        response = session.get(source.url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except Exception as e:
        logger.warning("[GUEST AI] source fetch failed %s: %s", source.url, e)
        return source

    final_url = _safe_href(response.url or source.url)
    if final_url:
        source.url = final_url

    content_type = str(response.headers.get("content-type") or "").lower()
    body_text = ""
    if "html" in content_type or "xml" in content_type or not content_type:
        html_text = response.text[:_MAX_RAW_HTML_LEN]
        fetched_title = _extract_title_from_html(html_text)
        fetched_snippet = _extract_meta_description(html_text)
        body_text = _truncate(_html_to_text(html_text), _MAX_SOURCE_CONTENT_LEN)
        if fetched_title:
            source.title = fetched_title
        if fetched_snippet and len(fetched_snippet) >= len(source.snippet):
            source.snippet = fetched_snippet
    else:
        body_text = _truncate(response.text, _MAX_SOURCE_CONTENT_LEN)

    source.content = body_text
    source.snippet = _truncate(source.snippet, _MAX_SOURCE_SNIPPET_LEN)
    return source


def _build_grounding_context(question: str, sources: list[GroundingSource]) -> str:
    detail_hint = (
        "Ответ должен быть кратким и точным."
        if _is_simple_question(question)
        else "Ответ должен быть развёрнутым, но без воды."
    )
    blocks = [
        f"Вопрос пользователя: {question}",
        detail_hint,
        "Используй только материалы ниже. Если в источниках нет ответа, прямо скажи об этом.",
    ]
    for idx, source in enumerate(sources, 1):
        parts = [
            f"Источник {idx}:",
            f"Заголовок: {source.title or source.domain or source.url}",
            f"Ссылка: {source.url}",
        ]
        if source.snippet:
            parts.append(f"Краткое описание: {source.snippet}")
        if source.content:
            parts.append(f"Извлечённый текст: {source.content}")
        blocks.append("\n".join(parts))
    return "\n\n".join(blocks).strip()


def _build_no_sources_context(question: str) -> str:
    return f"Вопрос пользователя: {question}\n\n{_NO_SOURCES_USER_PROMPT}".strip()


def _build_sources_footer(sources: list[GroundingSource]) -> str:
    lines = ["", "<b>Источники:</b>"]
    count = 0
    for source in sources:
        url = _safe_href(source.url)
        if not url:
            continue
        title = _html.escape(_truncate(source.title or source.domain or url, 120))
        lines.append(f'• <a href="{_html.escape(url, quote=True)}">{title}</a>')
        count += 1
        if count >= _MAX_SOURCE_FOOTER_ITEMS:
            break
    return "\n".join(lines) if count else ""


def _append_sources_footer(answer_html: str, sources: list[GroundingSource]) -> str:
    body = (answer_html or "").strip()
    footer = _build_sources_footer(sources)
    if not footer:
        return body[:_MAX_AI_REPLY_LEN]
    available = _MAX_AI_REPLY_LEN - len(footer)
    if available <= 0:
        return footer[:_MAX_AI_REPLY_LEN]
    trimmed_body = body[:available].rstrip()
    if body and len(body) > len(trimmed_body) and len(trimmed_body) > 0:
        trimmed_body = trimmed_body[:-1].rstrip() + "…"
    result = f"{trimmed_body}{footer}" if trimmed_body else footer.lstrip()
    return sanitize_ai_response_html(result)[:_MAX_AI_REPLY_LEN]


def _format_groq_failure(exc: Exception) -> AIReplyResult:
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        status = int(getattr(response, "status_code", 0) or 0)
        body = ""
        try:
            body = _truncate(_normalize_space(getattr(response, "text", "") or ""), 240)
        except Exception:
            body = ""
        if status in (401, 403):
            user_message = "Не удалось авторизоваться в Groq. Проверьте GROQ_API_KEY."
        elif status == 404:
            user_message = "Указанная модель Groq не найдена. Проверьте GROQ_MODEL."
        elif status == 429:
            user_message = "Groq временно отклоняет запросы из-за лимитов. Попробуйте позже."
        elif status >= 500:
            user_message = "Groq сейчас недоступен. Попробуйте позже."
        else:
            user_message = f"Groq отклонил запрос (HTTP {status})."
        debug = f"http_status={status}"
        if body:
            debug = f"{debug}; body={body}"
        return AIReplyResult(
            error_code="groq_http_error",
            user_message=user_message,
            debug_message=debug,
        )
    if isinstance(exc, requests.Timeout):
        return AIReplyResult(
            error_code="groq_timeout",
            user_message="Groq не ответил вовремя. Попробуйте позже.",
            debug_message=str(exc),
        )
    if isinstance(exc, requests.RequestException):
        return AIReplyResult(
            error_code="groq_request_error",
            user_message="Не удалось подключиться к Groq. Проверьте сеть и повторите попытку.",
            debug_message=str(exc),
        )
    return AIReplyResult(
        error_code="groq_unknown_error",
        user_message="Не удалось выполнить AI-запрос.",
        debug_message=str(exc),
    )


class GuestAIService:
    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        timeout: tuple[float, float] = (8.0, 25.0),
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        self._model = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL
        self._timeout = timeout
        self._session = session or requests.Session()
        self._session.headers.setdefault("User-Agent", _HTTP_USER_AGENT)
        self._session.headers.setdefault("Accept-Language", "ru,en;q=0.8")

    def available(self) -> bool:
        return bool(self._api_key)

    def build_system_prompt(
        self,
        is_owner_sender: bool,
        *,
        has_sources: bool = True,
        owner_intent: str | None = None,
    ) -> str:
        prompt = _BASE_SYSTEM_PROMPT
        if is_owner_sender:
            prompt = f"{prompt} {_OWNER_PROMPT_APPEND}"
            if owner_intent == "command":
                prompt = f"{prompt} {_OWNER_COMMAND_MODE_APPEND}"
            elif owner_intent == "question":
                prompt = f"{prompt} {_OWNER_QUESTION_MODE_APPEND}"
        if not has_sources:
            prompt = f"{prompt} {_NO_SOURCES_PROMPT_APPEND}"
        return prompt

    def _collect_sources(self, query: str) -> list[GroundingSource]:
        variants = _expand_query_variants(query)
        merged: list[GroundingSource] = []
        for variant in variants:
            merged = _merge_sources(
                merged,
                _search_wikipedia(
                    variant,
                    self._session,
                    _SEARCH_TIMEOUT,
                    api_url=_WIKIPEDIA_SEARCH_API_URL,
                    page_url_template=_WIKIPEDIA_PAGE_URL_TEMPLATE,
                    provider="wikipedia_ru",
                ),
                _search_wikipedia(
                    variant,
                    self._session,
                    _SEARCH_TIMEOUT,
                    api_url=_WIKIPEDIA_EN_SEARCH_API_URL,
                    page_url_template=_WIKIPEDIA_EN_PAGE_URL_TEMPLATE,
                    provider="wikipedia_en",
                ),
                _search_duckduckgo_instant(variant, self._session, _SEARCH_TIMEOUT),
                _search_duckduckgo_html(variant, self._session, _SEARCH_TIMEOUT),
            )
            if len(merged) >= _MAX_SEARCH_RESULTS:
                break
        if not merged:
            return []

        enriched: list[GroundingSource] = []
        for source in merged[:_MAX_FETCHED_SOURCES]:
            enriched.append(_fetch_source_context(source, self._session, _SOURCE_FETCH_TIMEOUT))

        leftover = merged[_MAX_FETCHED_SOURCES:]
        final_sources = enriched + leftover
        return [s for s in final_sources if s.snippet or s.content][: _MAX_GROUNDING_SOURCES]

    def generate_reply(
        self,
        user_text: str,
        *,
        is_owner_sender: bool,
        owner_intent: str | None = None,
    ) -> str | None:
        result = self.generate_reply_result(
            user_text,
            is_owner_sender=is_owner_sender,
            owner_intent=owner_intent,
        )
        return result.text if result.ok else None

    def generate_reply_result(
        self,
        user_text: str,
        *,
        is_owner_sender: bool,
        owner_intent: str | None = None,
    ) -> AIReplyResult:
        if not self.available():
            return AIReplyResult(
                error_code="missing_api_key",
                user_message="ИИ отключён: не задан GROQ_API_KEY.",
                debug_message="missing GROQ_API_KEY",
            )

        question = _normalize_space(user_text)
        if not question:
            return AIReplyResult(
                error_code="empty_question",
                user_message="Запрос к ИИ пустой.",
                debug_message="normalized question is empty",
            )

        sources = self._collect_sources(question)
        has_sources = bool(sources)
        if not has_sources:
            logger.info("[GUEST AI] no grounding sources found for query=%r, using model-only fallback", question)
        user_content = (
            _build_grounding_context(question, sources)
            if has_sources
            else _build_no_sources_context(question)
        )

        payload = {
            "model": self._model,
            "temperature": _COMPLETION_TEMPERATURE,
            "max_tokens": _MAX_COMPLETION_TOKENS,
            "messages": [
                {
                    "role": "system",
                    "content": self.build_system_prompt(
                        is_owner_sender,
                        has_sources=has_sources,
                        owner_intent=owner_intent,
                    ),
                },
                {"role": "user", "content": user_content},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._session.post(
                _GROQ_API_URL,
                json=payload,
                headers=headers,
                timeout=self._timeout,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            failure = _format_groq_failure(e)
            logger.warning(
                "[GUEST AI] Groq request failed for query=%r model=%s sources=%s code=%s details=%s",
                question,
                self._model,
                len(sources),
                failure.error_code,
                failure.debug_message or str(e),
            )
            return failure

        try:
            choices = data.get("choices") if isinstance(data, dict) else []
            first = choices[0] if isinstance(choices, list) and choices else {}
            message = first.get("message") if isinstance(first, dict) else {}
            content = message.get("content") if isinstance(message, dict) else ""
        except Exception:
            content = ""
        if not isinstance(content, str) or not content.strip():
            logger.warning("[GUEST AI] empty content returned for query=%r model=%s", question, self._model)
            return AIReplyResult(
                error_code="empty_model_response",
                user_message="Groq вернул пустой ответ.",
                debug_message="response choices did not contain non-empty message.content",
            )

        cleaned = sanitize_ai_response_html(content)
        if not cleaned:
            logger.warning("[GUEST AI] sanitized content is empty for query=%r model=%s", question, self._model)
            return AIReplyResult(
                error_code="empty_sanitized_response",
                user_message="Ответ ИИ не удалось подготовить для Telegram.",
                debug_message="sanitize_ai_response_html returned empty string",
            )
        if not has_sources:
            return AIReplyResult(text=cleaned[:_MAX_AI_REPLY_LEN])
        with_sources = _append_sources_footer(cleaned, sources)
        final_text = with_sources[:_MAX_AI_REPLY_LEN] or None
        if not final_text:
            logger.warning("[GUEST AI] final response is empty after footer append for query=%r", question)
            return AIReplyResult(
                error_code="empty_final_response",
                user_message="Ответ ИИ оказался пустым после обработки.",
                debug_message="final response empty after _append_sources_footer",
            )
        return AIReplyResult(text=final_text)
