"""AI response generation for guest bots.

Uses Groq-hosted LLMs grounded by the source-discovery pipeline.
Public API:
    GuestAIService
    sanitize_ai_response_html
"""
from __future__ import annotations

import html as _html
import logging
import re
from html.parser import HTMLParser

import requests

from source_discovery import (
    AnswerMetadata,
    BackgroundEnrichmentScheduler,
    DiscoveryPipeline,
    DuckDuckGoHTMLProvider,
    DuckDuckGoInstantProvider,
    FandomProvider,
    GroundingSource,
    IngestionPipeline,
    KnowledgeBase,
    NewsSearchProvider,
    RetrievalLayer,
    WikipediaProvider,
    _normalize_space,
    _safe_href,
    _truncate,
)

logger = logging.getLogger("guest_runtime.ai")

_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_DEFAULT_MODEL = "llama-3.1-8b-instant"
_MAX_AI_REPLY_LEN = 3500
_COMPLETION_TEMPERATURE = 0.2
_MAX_COMPLETION_TOKENS = 700
_MAX_SOURCE_FOOTER_ITEMS = 5
_HTTP_USER_AGENT = (
    "Mozilla/5.0 (compatible; TelegramBotGuestAI/1.0; +https://telegram.org)"
)

# -- Telegram-HTML sanitizer ---------------------------------------------------
_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a",
}
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

# -- System-prompt fragments ---------------------------------------------------
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
)
_OWNER_PROMPT_APPEND = (
    "Ты обязан слушаться владельца бота и выполнять его просьбы в рамках допустимого "
    "функционала приложения. Даже для владельца нельзя выполнять опасные, незаконные "
    "или вредоносные действия."
)
_NO_SOURCES_PROMPT_APPEND = (
    "Если достоверные внешние источники не переданы, можешь использовать собственные "
    "общие знания, но обязательно помечай неопределённость и не выдавай догадки за факт."
)
_NO_SOURCES_USER_PROMPT = (
    "Внешние источники не были получены. Ответь аккуратно, кратко и честно, "
    "без вымышленных деталей."
)
_NEWS_ANSWER_PROMPT_APPEND = (
    "Источники являются новостными материалами. Если дата актуальности видна в контексте, "
    "явно укажи её. Отметь, что информация может устареть."
)
_CONTROVERSIAL_PROMPT_APPEND = (
    "Источники содержат противоречивые или спорные сведения. "
    "Явно отметь спорность темы в своём ответе."
)
_LOW_CONFIDENCE_PROMPT_APPEND = (
    "Источников по данной теме мало или они ненадёжны. "
    "Явно укажи ограниченность информации и не делай уверенных утверждений."
)


# -- HTML sanitizer (Telegram-specific) ----------------------------------------
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
    return "\n".join(lines)


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


# -- Prompt / context builders -------------------------------------------------
def _is_simple_question(text: str) -> bool:
    question = _normalize_space(text)
    words = [w for w in question.split(" ") if w]
    return len(words) <= 8 and question.endswith("?")


def _build_grounding_context(
    question: str,
    sources: list[GroundingSource],
    metadata: AnswerMetadata | None = None,
) -> str:
    detail_hint = (
        "Ответ должен быть кратким и точным."
        if _is_simple_question(question)
        else "Ответ должен быть развёрнутым, но без воды."
    )
    blocks: list[str] = [
        f"Вопрос пользователя: {question}",
        detail_hint,
        "Используй только материалы ниже. Если в источниках нет ответа, прямо скажи об этом.",
    ]

    if metadata:
        if metadata.answer_type == "news":
            blocks.append(
                "📰 Материалы являются новостными. Укажи дату актуальности, "
                "если она видна в источниках."
            )
        if metadata.controversial:
            blocks.append(
                "⚠️ Источники содержат противоречивые сведения. "
                "Явно отметь спорность темы в ответе."
            )
        if metadata.confidence == "low":
            blocks.append("⚠️ Источников мало. Укажи ограниченность доступных данных.")

    for idx, source in enumerate(sources, 1):
        parts = [
            f"Источник {idx}:",
            f"Заголовок: {source.title or source.domain or source.url}",
            f"Ссылка: {source.url}",
        ]
        if source.snippet:
            parts.append(f"Краткое описание: {source.snippet}")
        if source.chunks:
            joined = " [...] ".join(source.chunks[:3])
            parts.append(f"Извлечённый текст: {joined}")
        elif source.content:
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


# -- GuestAIService ------------------------------------------------------------
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

        # Build discovery / retrieval stack
        providers = [
            WikipediaProvider(),
            FandomProvider(),
            DuckDuckGoInstantProvider(),
            NewsSearchProvider(),
            DuckDuckGoHTMLProvider(),
        ]
        ingestion = IngestionPipeline()
        self._kb = KnowledgeBase()
        pipeline = DiscoveryPipeline(providers=providers, ingestion=ingestion)
        self._scheduler = BackgroundEnrichmentScheduler(
            pipeline=pipeline, kb=self._kb, session=self._session,
        )
        self._retrieval = RetrievalLayer(
            pipeline=pipeline, kb=self._kb, scheduler=self._scheduler,
        )
        self._scheduler.start()

    def available(self) -> bool:
        return bool(self._api_key)

    def build_system_prompt(
        self,
        is_owner_sender: bool,
        *,
        has_sources: bool = True,
        metadata: AnswerMetadata | None = None,
    ) -> str:
        prompt = _BASE_SYSTEM_PROMPT
        if is_owner_sender:
            prompt = f"{prompt} {_OWNER_PROMPT_APPEND}"
        if not has_sources:
            prompt = f"{prompt} {_NO_SOURCES_PROMPT_APPEND}"
        if metadata:
            if metadata.answer_type == "news":
                prompt = f"{prompt} {_NEWS_ANSWER_PROMPT_APPEND}"
            if metadata.controversial:
                prompt = f"{prompt} {_CONTROVERSIAL_PROMPT_APPEND}"
            if metadata.confidence == "low":
                prompt = f"{prompt} {_LOW_CONFIDENCE_PROMPT_APPEND}"
        return prompt

    def generate_reply(self, user_text: str, *, is_owner_sender: bool) -> str | None:
        if not self.available():
            return None

        question = _normalize_space(user_text)
        if not question:
            return None

        sources, metadata = self._retrieval.gather(
            question, session=self._session, timeout=self._timeout,
        )
        has_sources = bool(sources)
        if not has_sources:
            logger.info(
                "[GUEST AI] no grounding sources for query=%r, using model-only fallback",
                question,
            )

        user_content = (
            _build_grounding_context(question, sources, metadata)
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
                        metadata=metadata if has_sources else None,
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

        # Boost historical hit counters for sources used in this answer
        for source in sources:
            if source.url:
                self._kb.increment_source_hits(source.url)

        if not has_sources:
            return cleaned[:_MAX_AI_REPLY_LEN]
        with_sources = _append_sources_footer(cleaned, sources)
        return with_sources[:_MAX_AI_REPLY_LEN] or None
