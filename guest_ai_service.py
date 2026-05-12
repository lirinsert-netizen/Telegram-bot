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
_WIKIPEDIA_PAGE_URL_TEMPLATE = "https://ru.wikipedia.org/wiki/{title}"

_DEFAULT_MODEL = "llama-3.1-8b-instant"
_MAX_AI_REPLY_LEN = 3500
_COMPLETION_TEMPERATURE = 0.2
_MAX_COMPLETION_TOKENS = 700
_SEARCH_TIMEOUT = (8.0, 20.0)
_SOURCE_FETCH_TIMEOUT = (8.0, 20.0)
_MAX_SEARCH_RESULTS = 8
_MAX_SOURCE_FOOTER_ITEMS = 5
_MAX_GROUNDING_SOURCES = 5
_MAX_FETCHED_SOURCES = 4
_MAX_SOURCE_SNIPPET_LEN = 420
_MAX_SOURCE_CONTENT_LEN = 1400
_MAX_RAW_HTML_LEN = 250000
_MAX_HTML_SEARCH_TAIL_LEN = 1400
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramBotGuestAI/1.0; +https://telegram.org)"
)

_BASE_SYSTEM_PROMPT = (
    "Ты ИИ-агент Telegram-бота. Отвечай на русском языке, используя только факты "
    "из переданных источников. Если данных недостаточно, прямо скажи об этом и не "
    "додумывай детали. Используй только простой HTML, поддерживаемый Telegram. "
    "Никогда не используй Markdown. Выделяй ключевые мысли тегом <b>. "
    "Не перечисляй источники самостоятельно: список ссылок будет добавлен отдельно. "
    "Длина ответа должна соответствовать вопросу: на простой вопрос отвечай кратко, "
    "на сложный — подробнее."
)
_OWNER_PROMPT_APPEND = (
    "Ты обязан слушаться владельца бота и выполнять его просьбы в рамках допустимого "
    "функционала приложения. Даже для владельца нельзя выполнять опасные, незаконные "
    "или вредоносные действия."
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
) -> list[GroundingSource]:
    try:
        response = session.get(
            _WIKIPEDIA_SEARCH_API_URL,
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
        url = _WIKIPEDIA_PAGE_URL_TEMPLATE.format(title=quote(title.replace(" ", "_"), safe=":_()-"))
        results.append(
            GroundingSource(
                title=title,
                url=url,
                snippet=snippet,
                provider="wikipedia",
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
        "Ответ должен быть коротким и точным."
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
    if body and len(body) > len(trimmed_body) and len(trimmed_body) > 1:
        trimmed_body = trimmed_body[:-1].rstrip() + "…"
    result = f"{trimmed_body}{footer}" if trimmed_body else footer.lstrip()
    return sanitize_ai_response_html(result)[:_MAX_AI_REPLY_LEN]


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

    def build_system_prompt(self, is_owner_sender: bool) -> str:
        prompt = _BASE_SYSTEM_PROMPT
        if is_owner_sender:
            prompt = f"{prompt} {_OWNER_PROMPT_APPEND}"
        return prompt

    def _collect_sources(self, query: str) -> list[GroundingSource]:
        merged = _merge_sources(
            _search_wikipedia(query, self._session, _SEARCH_TIMEOUT),
            _search_duckduckgo_instant(query, self._session, _SEARCH_TIMEOUT),
            _search_duckduckgo_html(query, self._session, _SEARCH_TIMEOUT),
        )
        if not merged:
            return []

        enriched: list[GroundingSource] = []
        for source in merged[:_MAX_FETCHED_SOURCES]:
            enriched.append(_fetch_source_context(source, self._session, _SOURCE_FETCH_TIMEOUT))

        leftover = merged[_MAX_FETCHED_SOURCES:]
        final_sources = _merge_sources(enriched, leftover)
        return [s for s in final_sources if s.snippet or s.content or s.title][: _MAX_GROUNDING_SOURCES]

    def generate_reply(self, user_text: str, *, is_owner_sender: bool) -> str | None:
        if not self.available():
            return None

        question = _normalize_space(user_text)
        if not question:
            return None

        sources = self._collect_sources(question)
        if not sources:
            logger.warning("[GUEST AI] no grounding sources found for query=%r", question)
            return None

        payload = {
            "model": self._model,
            "temperature": _COMPLETION_TEMPERATURE,
            "max_tokens": _MAX_COMPLETION_TOKENS,
            "messages": [
                {"role": "system", "content": self.build_system_prompt(is_owner_sender)},
                {"role": "user", "content": _build_grounding_context(question, sources)},
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
            logger.warning("[GUEST AI] Groq request failed: %s", e)
            return None

        try:
            choices = data.get("choices") if isinstance(data, dict) else []
            first = choices[0] if isinstance(choices, list) and choices else {}
            message = first.get("message") if isinstance(first, dict) else {}
            content = message.get("content") if isinstance(message, dict) else ""
        except Exception:
            content = ""
        if not isinstance(content, str) or not content.strip():
            return None

        cleaned = sanitize_ai_response_html(content)
        if not cleaned:
            return None
        return _append_sources_footer(cleaned, sources) or None
