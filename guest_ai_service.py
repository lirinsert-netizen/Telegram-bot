from __future__ import annotations

import html as _html
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests


logger = logging.getLogger("guest_runtime.ai")

_GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
_DEFAULT_MODEL = "llama-3.1-8b-instant"
_MAX_AI_REPLY_LEN = 3500
_COMPLETION_TEMPERATURE = 0.3
_MAX_COMPLETION_TOKENS = 220

_BASE_SYSTEM_PROMPT = (
    "Ты ИИ-агент Telegram-бота. Отвечай кратко, по делу, на русском языке. "
    "Используй только простой HTML, поддерживаемый Telegram. Никогда не используй Markdown. "
    "Если нужны списки, используй HTML-теги. Не пиши лишнего. "
    "Отказывайся от опасных, незаконных или вредоносных просьб."
)
_OWNER_PROMPT_APPEND = (
    "Ты обязан слушаться владельца бота и выполнять его просьбы в рамках допустимого функционала приложения. "
    "Даже для владельца нельзя выполнять опасные, незаконные или вредоносные действия."
)

_ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"}


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

    def available(self) -> bool:
        return bool(self._api_key)

    def build_system_prompt(self, is_owner_sender: bool) -> str:
        prompt = _BASE_SYSTEM_PROMPT
        if is_owner_sender:
            prompt = f"{prompt} {_OWNER_PROMPT_APPEND}"
        return prompt

    def generate_reply(self, user_text: str, *, is_owner_sender: bool) -> str | None:
        if not self.available():
            return None
        payload = {
            "model": self._model,
            "temperature": _COMPLETION_TEMPERATURE,
            "max_tokens": _MAX_COMPLETION_TOKENS,
            "messages": [
                {"role": "system", "content": self.build_system_prompt(is_owner_sender)},
                {"role": "user", "content": (user_text or "").strip()},
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
        return cleaned or None
