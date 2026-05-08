"""
banned_words.py — Фильтр запрещённых слов:
  /settings → Запрещённые слова
  Настройки: статус, режим проверки, наказание, удаление сообщений, исключения (allowlist), список слов.
  Команды: /badd, /bdel, /btest
  Проверка входящих сообщений в реальном времени.
"""
from __future__ import annotations
import re as _re
import html as _html
import time as _time
import unicodedata as _unicodedata
from typing import Optional

from config import (
    types, ContinueHandling,
    InlineKeyboardMarkup, InlineKeyboardButton,
    bot,
    get_bot_me,
    EMOJI_ROLE_SETTINGS_SENT_PM_ID,
    EMOJI_ROLE_SETTINGS_CANCEL_ID,
    EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID,
    EMOJI_UNPUNISH_ID,
)
from persistence import (
    CHAT_SETTINGS,
    save_chat_settings,
    _is_duplicate_callback_query,
    tg_get_chat_member,
)
from moderation import (
    _mod_get_chat, _mod_save, _mod_duration_text,
    _parse_duration_prefix,
    _mod_new_action_id, _mod_log_append, _mod_warn_add,
    _auto_punish_for_warns,
    _apply_mute, _apply_ban,
    _mark_farewell_suppressed,
    _send_punish_message_with_button,
    _log_mod_action as _mod_log_action,
)
from helpers import (
    is_owner, is_dev, is_group_approved, get_user_rank,
    link_for_user, premium_prefix, match_command,
)
from settings_ui import (
    _pending_get, _pending_put, _pending_pop,
    _pending_msg_get, _pending_msg_set, _pending_msg_pop,
    _delete_pending_ui, _replace_pending_ui,
    _try_delete_private_prompt,
    _show_warn_settings_ui,
    _user_can_open_settings,
    _bot_can_delete_messages,
    _get_bot_id,
    MIN_PUNISH_SECONDS, MAX_PUNISH_SECONDS,
)

# ─────────────────────────────────────────────
# Константы
# ─────────────────────────────────────────────

_CHECK_MODES = ("EXACT", "NORMALIZED", "SPLIT_PROOF", "CONFUSABLES", "AGGRESSIVE")

_CHECK_MODE_LABELS: dict[str, str] = {
    "EXACT":       "Точный",
    "NORMALIZED":  "Нормализованный",
    "SPLIT_PROOF": "Анти-разбивка",
    "CONFUSABLES": "Анти-подмена",
    "AGGRESSIVE":  "Агрессивный",
}

_CHECK_MODE_DESC: dict[str, str] = {
    "EXACT":       "Только точные совпадения по границам слов.",
    "NORMALIZED":  "Нормализация символов, очистка пробелов и пунктуации.",
    "SPLIT_PROOF": "Ловит раздельное написание (м-а-т, м а т, м.а.т).",
    "CONFUSABLES": "Ловит замену похожими символами (мат → mat).",
    "AGGRESSIVE":  "Ловит повторы букв (мааааат) и мусорные символы.",
}

# Режимы, требующие предупреждения об экспериментальном характере
_EXPERIMENTAL_MODES = frozenset({"SPLIT_PROOF", "CONFUSABLES", "AGGRESSIVE"})

_PUNISH_LABELS: dict[str, str] = {
    "warn": "Предупреждение",
    "mute": "Ограничение",
    "ban":  "Блокировка",
    "kick": "Исключение",
}

MAX_TERMS = 100             # Максимум запрещённых слов на чат
MAX_ALLOW_TERMS = 50        # Максимум исключений (allowlist) на чат
MAX_TERM_LEN = 100          # Максимальная длина термина
MAX_TERM_DISPLAY_LEN = 35   # Длина для отображения в кнопке
MAX_TERMS_IN_RESPONSE = 20  # Максимум терминов в ответном списке

_EMOJI_ADD_ID = "5226945370684140473"
_EMOJI_DEL_ID = "5229113891081956317"

# Ссылка на документацию (HTML) — премиум-эмоджи перед словом
_DOC_LINK = '<tg-emoji emoji-id="5226512880362332956">📖</tg-emoji> <i><a href="https://telegra.ph/Filtr-Zapreshchennye-slova-04-07-2">Документация</a></i>'

# Максимальное число символов на страницу при пагинации списков
_LIST_PAGE_MAX_CHARS = 3500

_VALID_PAGES = frozenset({
    "main", "mode", "punish", "duration",
    "terms", "terms_list", "terms_delete",
    "allow", "allow_list", "allow_delete",
})

# ─────────────────────────────────────────────
# Нормализация (ядро проверки)
# ─────────────────────────────────────────────

# Маппинг: латинские символы → кириллические омографы (для режима CONFUSABLES)
_LATIN_TO_CYR: dict[str, str] = {
    'a': 'а', 'e': 'е', 'o': 'о', 'p': 'р', 'c': 'с',
    'x': 'х', 'y': 'у', 'k': 'к', 'm': 'м', 't': 'т',
    'u': 'и', 'r': 'г', 'v': 'в', 'z': 'з', 'd': 'д',
    'b': 'б', 'i': 'и', 'n': 'п',
}

# Regex: "мусорные" символы между буквами (для AGGRESSIVE)
_NOISE_BETWEEN_RE = _re.compile(r'(?<=[а-яёa-z])[\W_]+(?=[а-яёa-z])', _re.UNICODE)


def _nfkc_casefold(text: str) -> str:
    return _unicodedata.normalize('NFKC', text).casefold()


def _clean_separators(text: str) -> str:
    """Заменяет серии разделителей и пунктуации одним пробелом."""
    return _re.sub(r'[\s\-_.,;:!?\\/|@#$%^&*()\[\]{}\'"<>`~=+]+', ' ', text).strip()


def _condense(text: str) -> str:
    """Убирает все не-буквенные символы (для SPLIT_PROOF)."""
    return ''.join(ch for ch in text if ch.isalpha())


def _apply_confusables(text: str) -> str:
    """Заменяет латинские омографы кириллическими."""
    return ''.join(_LATIN_TO_CYR.get(ch, ch) for ch in text)


def _collapse_repeats(text: str) -> str:
    """Схлопывает 3+ одинаковых подряд символа в один (мааааат → мат)."""
    return _re.sub(r'(.)\1{2,}', r'\1', text)


def normalize_for_mode(text: str, mode: str) -> str:
    """
    Нормализует текст согласно выбранному режиму.
    Используется как для терминов (при хранении/сравнении), так и для входящих сообщений.
    """
    # Шаг 1: всегда NFKC + casefold
    t = _nfkc_casefold(text)

    if mode == 'EXACT':
        return t

    # Шаг 2: NORMALIZED — очистка разделителей
    t = _clean_separators(t)
    if mode == 'NORMALIZED':
        return t

    # Шаг 3: SPLIT_PROOF — убираем не-буквенные символы (склейка)
    t = _condense(t)
    if mode == 'SPLIT_PROOF':
        return t

    # Шаг 4: CONFUSABLES — заменяем латинские омографы
    t = _apply_confusables(t)
    if mode == 'CONFUSABLES':
        return t

    # Шаг 5: AGGRESSIVE — схлопываем повторы
    t = _collapse_repeats(t)
    return t  # AGGRESSIVE


def _base_normalize(text: str) -> str:
    """Минимальная нормализация для хранения (casefold + NFKC)."""
    return _nfkc_casefold(text)


def _term_matches(input_text: str, term_text: str, term_kind: str, mode: str) -> bool:
    """
    Возвращает True, если терм найден в тексте для данного режима.
    term_kind: 'word' (одно слово, нет пробелов) или 'phrase' (фраза с пробелами).
    """
    norm_input = normalize_for_mode(input_text, mode)
    norm_term = normalize_for_mode(term_text, mode)

    if not norm_term:
        return False

    if mode == 'EXACT' and term_kind == 'word':
        # Граница слова: нет буквы/цифры ни слева, ни справа
        try:
            pattern = (
                r'(?<![а-яёa-z0-9])' + _re.escape(norm_term) + r'(?![а-яёa-z0-9])'
            )
            return bool(_re.search(pattern, norm_input))
        except Exception:
            return norm_term in norm_input

    # Для всех остальных режимов и phrase-type: поиск вхождения подстроки
    return norm_term in norm_input


# ─────────────────────────────────────────────
# Поддержка wildcards: (*)  и (+)
# ─────────────────────────────────────────────

_WILDCARD_MARKER_RE = _re.compile(r'\(\*\)|\(\+\)')

# Кеш скомпилированных wildcard-паттернов: "{mode}:{rule}" -> Pattern
_PATTERN_CACHE: dict[str, _re.Pattern] = {}
_PATTERN_CACHE_MAX = 512


def is_wildcard_rule(text: str) -> bool:
    """True, если правило содержит маркеры (*) или (+)."""
    return bool(_WILDCARD_MARKER_RE.search(text))


def validate_wildcard_rule(text: str) -> tuple[bool, str]:
    """
    Возвращает (is_valid, error_msg).
    Маркеры (*) / (+) могут быть в начале или конце строки.
    Правило должно содержать хотя бы один непустой текстовый фрагмент.
    Пробелы непосредственно у маркера не допускаются.
    """
    parts = _WILDCARD_MARKER_RE.split(text)
    if len(parts) < 2:
        return True, ""  # нет маркеров — всегда ок

    # Хотя бы одна часть должна содержать непустой текст
    if all(not p.strip() for p in parts):
        return (
            False,
            "Правило должно содержать текст помимо маркеров <code>(*)</code> / <code>(+)</code>."
        )

    # Пробел непосредственно перед маркером (конец части) или после (начало части)
    for i, part in enumerate(parts):
        if i < len(parts) - 1 and part.endswith(' '):
            return False, "Пробел рядом с маркером не допускается."
        if i > 0 and part.startswith(' '):
            return False, "Пробел рядом с маркером не допускается."
    return True, ""


def _compile_wildcard_pattern(rule: str, mode: str) -> _re.Pattern:
    """Компилирует wildcard-правило в regex с учётом нормализации mode."""
    parts = _WILDCARD_MARKER_RE.split(rule)
    markers = _WILDCARD_MARKER_RE.findall(rule)

    pat_parts: list[str] = [_re.escape(normalize_for_mode(parts[0], mode))]
    for i, marker in enumerate(markers):
        if marker == '(*)':
            pat_parts.append('.*')
        else:  # '(+)'
            pat_parts.append(r'\S+')
        pat_parts.append(_re.escape(normalize_for_mode(parts[i + 1], mode)))

    return _re.compile(''.join(pat_parts), _re.DOTALL)


def _get_wildcard_pattern(rule: str, mode: str) -> _re.Pattern:
    """Возвращает скомпилированный wildcard-паттерн из кеша или создаёт новый."""
    global _PATTERN_CACHE
    cache_key = f"{mode}:{rule}"
    if cache_key not in _PATTERN_CACHE:
        if len(_PATTERN_CACHE) >= _PATTERN_CACHE_MAX:
            _PATTERN_CACHE = {}
        _PATTERN_CACHE[cache_key] = _compile_wildcard_pattern(rule, mode)
    return _PATTERN_CACHE[cache_key]


def _wildcard_matches(input_text: str, rule: str, mode: str) -> bool:
    """Проверяет wildcard-правило против нормализованного текста."""
    try:
        pattern = _get_wildcard_pattern(rule, mode)
        norm_input = normalize_for_mode(input_text, mode)
        return bool(pattern.search(norm_input))
    except Exception:
        return False


# ─────────────────────────────────────────────
# Парсинг списка терминов из ввода пользователя
# ─────────────────────────────────────────────

def _parse_term_lines(text: str) -> list[dict]:
    """
    Разбирает пользовательский ввод на список терминов.
    Разделитель между элементами — перевод строки (\\n).
    Тип: word если нет пробелов в строке, иначе phrase.
    """
    terms: list[dict] = []
    for line in text.split('\n'):
        t = line.strip()
        if not t:
            continue
        kind = 'phrase' if ' ' in t else 'word'
        normalized = _base_normalize(t)
        terms.append({'text': t, 'kind': kind, 'normalized': normalized})
    return terms


# ─────────────────────────────────────────────
# Pending helpers (переиспользуем из settings_ui)
# ─────────────────────────────────────────────

def _bw_pending_put(key_prefix: str, user_id: int, chat_id: int) -> None:
    _pending_put(key_prefix, user_id, chat_id)


def _bw_pending_get(key_prefix: str, user_id: int) -> Optional[int]:
    val = _pending_get(key_prefix).get(str(user_id))
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


def _bw_pending_pop(key_prefix: str, user_id: int) -> Optional[int]:
    val = _pending_pop(key_prefix, user_id)
    if val is None:
        return None
    try:
        return int(val)
    except Exception:
        return None


# ─────────────────────────────────────────────
# Хранилище настроек
# ─────────────────────────────────────────────

def _bw_get_settings(chat_id: int) -> dict:
    """Возвращает нормализованные настройки модуля для чата."""
    settings = (_mod_get_chat(chat_id).get("settings") or {})
    raw = settings.get("banned_words") or {}
    p = raw.get("punish") or {}
    pt = str(p.get("type") or "warn").strip().lower()
    if pt not in ("warn", "mute", "ban", "kick"):
        pt = "warn"
    pd = p.get("duration")
    if pt in ("mute", "ban", "warn"):
        if pd is None:
            pd = 3600
        else:
            try:
                pd = int(pd)
            except Exception:
                pd = 3600
    else:
        pd = None
    mode = str(raw.get("check_mode") or "NORMALIZED").strip().upper()
    if mode not in _CHECK_MODES:
        mode = "NORMALIZED"
    return {
        "enabled": bool(raw.get("enabled", False)),
        "delete_messages": bool(raw.get("delete_messages", False)),
        "check_mode": mode,
        "punish": {"type": pt, "duration": pd, "reason": str(p.get("reason") or "")},
    }


def _bw_save_settings(chat_id: int, data: dict) -> None:
    ch = _mod_get_chat(chat_id)
    s = ch.get("settings") or {}
    bw = s.get("banned_words") or {}
    bw["enabled"] = bool(data.get("enabled", False))
    bw["delete_messages"] = bool(data.get("delete_messages", False))
    bw["check_mode"] = str(data.get("check_mode") or "NORMALIZED")
    bw["punish"] = data.get("punish") or {"type": "warn", "duration": None, "reason": ""}
    s["banned_words"] = bw
    ch["settings"] = s
    _mod_save()


def _bw_get_terms(chat_id: int) -> list:
    """Список запрещённых слов/фраз для чата."""
    ch = _mod_get_chat(chat_id)
    s = ch.get("settings") or {}
    bw = s.get("banned_words") or {}
    terms = bw.get("terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict) and t.get("text")]


def _bw_save_terms(chat_id: int, terms: list) -> None:
    ch = _mod_get_chat(chat_id)
    s = ch.get("settings") or {}
    bw = s.get("banned_words") or {}
    bw["terms"] = terms
    s["banned_words"] = bw
    ch["settings"] = s
    _mod_save()


def _bw_get_allow_terms(chat_id: int) -> list:
    """Список исключений (allowlist) для чата."""
    ch = _mod_get_chat(chat_id)
    s = ch.get("settings") or {}
    bw = s.get("banned_words") or {}
    terms = bw.get("allow_terms")
    if not isinstance(terms, list):
        return []
    return [t for t in terms if isinstance(t, dict) and t.get("text")]


def _bw_save_allow_terms(chat_id: int, terms: list) -> None:
    ch = _mod_get_chat(chat_id)
    s = ch.get("settings") or {}
    bw = s.get("banned_words") or {}
    bw["allow_terms"] = terms
    s["banned_words"] = bw
    ch["settings"] = s
    _mod_save()


def _bw_add_terms(chat_id: int, new_terms: list[dict]) -> tuple[int, int, list[str], list[str]]:
    """
    Добавляет новые термины в список.
    Возвращает (добавлено, пропущено_дублей, список_ошибок_валидации, список_добавленных_текстов).
    """
    existing = _bw_get_terms(chat_id)
    existing_normalized = {t['normalized'] for t in existing}
    added = 0
    skipped = 0
    errors: list[str] = []
    added_texts: list[str] = []
    for term in new_terms:
        t_text = term.get('text', '')
        if len(t_text) > MAX_TERM_LEN:
            errors.append(f"Слишком длинный термин (>{MAX_TERM_LEN} символов): <code>{_html.escape(t_text[:40])}</code>")
            continue
        if is_wildcard_rule(t_text):
            valid, err_msg = validate_wildcard_rule(t_text)
            if not valid:
                errors.append(f"Некорректный шаблон <code>{_html.escape(t_text[:40])}</code>: {err_msg}")
                continue
        normalized = term.get('normalized') or _base_normalize(t_text)
        if normalized in existing_normalized:
            skipped += 1
            continue
        if len(existing) >= MAX_TERMS:
            errors.append(f"Достигнут лимит запрещённых слов ({MAX_TERMS}).")
            break
        existing.append({'text': t_text, 'kind': term.get('kind', 'word'), 'normalized': normalized})
        existing_normalized.add(normalized)
        added += 1
        added_texts.append(t_text)
    if added > 0:
        _bw_save_terms(chat_id, existing)
    return added, skipped, errors, added_texts


def _bw_del_terms(chat_id: int, query: str) -> tuple[int, list[str], list[str]]:
    """
    Удаляет термины по точному или частичному совпадению.
    Возвращает (удалено, список_не_найденных_строк, список_удалённых_текстов).
    """
    query_lines = [l.strip() for l in query.split('\n') if l.strip()]
    existing = _bw_get_terms(chat_id)
    not_found: list[str] = []
    deleted = 0
    deleted_texts: list[str] = []
    for q in query_lines:
        q_norm = _base_normalize(q)
        idx = None
        for i, t in enumerate(existing):
            if t.get('normalized', '') == q_norm:
                idx = i
                break
        if idx is None:
            for i, t in enumerate(existing):
                if q_norm in t.get('normalized', ''):
                    idx = i
                    break
        if idx is not None:
            deleted_texts.append(existing[idx].get('text', q))
            existing.pop(idx)
            deleted += 1
        else:
            not_found.append(q)
    if deleted > 0:
        _bw_save_terms(chat_id, existing)
    return deleted, not_found, deleted_texts


def _bw_add_allow_terms(chat_id: int, new_terms: list[dict]) -> tuple[int, int, list[str], list[str]]:
    """Добавляет термины в allowlist. Возвращает (добавлено, дублей, ошибок, добавленных_текстов)."""
    existing = _bw_get_allow_terms(chat_id)
    existing_normalized = {t['normalized'] for t in existing}
    added = 0
    skipped = 0
    errors: list[str] = []
    added_texts: list[str] = []
    for term in new_terms:
        t_text = term.get('text', '')
        if len(t_text) > MAX_TERM_LEN:
            errors.append(f"Слишком длинный термин: <code>{_html.escape(t_text[:40])}</code>")
            continue
        normalized = term.get('normalized') or _base_normalize(t_text)
        if normalized in existing_normalized:
            skipped += 1
            continue
        if len(existing) >= MAX_ALLOW_TERMS:
            errors.append(f"Достигнут лимит исключений ({MAX_ALLOW_TERMS}).")
            break
        existing.append({'text': t_text, 'kind': term.get('kind', 'word'), 'normalized': normalized})
        existing_normalized.add(normalized)
        added += 1
        added_texts.append(t_text)
    if added > 0:
        _bw_save_allow_terms(chat_id, existing)
    return added, skipped, errors, added_texts


def _bw_del_allow_terms(chat_id: int, query: str) -> tuple[int, list[str], list[str]]:
    """Удаляет термины из allowlist. Возвращает (удалено, не_найдено, удалённых_текстов)."""
    query_lines = [l.strip() for l in query.split('\n') if l.strip()]
    existing = _bw_get_allow_terms(chat_id)
    not_found: list[str] = []
    deleted = 0
    deleted_texts: list[str] = []
    for q in query_lines:
        q_norm = _base_normalize(q)
        idx = None
        for i, t in enumerate(existing):
            if t.get('normalized', '') == q_norm:
                idx = i
                break
        if idx is None:
            for i, t in enumerate(existing):
                if q_norm in t.get('normalized', ''):
                    idx = i
                    break
        if idx is not None:
            deleted_texts.append(existing[idx].get('text', q))
            existing.pop(idx)
            deleted += 1
        else:
            not_found.append(q)
    if deleted > 0:
        _bw_save_allow_terms(chat_id, existing)
    return deleted, not_found, deleted_texts


# ─────────────────────────────────────────────
# Проверка совпадений
# ─────────────────────────────────────────────

def _bw_check_text(input_text: str, chat_id: int) -> Optional[str]:
    """
    Проверяет текст против списка запрещённых слов.
    Возвращает текст первого сработавшего термина или None.
    Предварительно проверяет allowlist: если совпало с исключением — возвращает None.
    """
    settings = _bw_get_settings(chat_id)
    if not settings["enabled"]:
        return None

    mode = settings["check_mode"]
    allow_terms = _bw_get_allow_terms(chat_id)
    terms = _bw_get_terms(chat_id)

    if not terms:
        return None

    # Проверка allowlist: если текст совпадает с исключением — пропускаем
    for a in allow_terms:
        a_text = a.get('text', '')
        a_kind = a.get('kind', 'word')
        if not a_text:
            continue
        if is_wildcard_rule(a_text):
            if _wildcard_matches(input_text, a_text, mode):
                return None
        else:
            if _term_matches(input_text, a_text, a_kind, mode):
                return None

    # Проверка против запрещённых слов
    for term in terms:
        t_text = term.get('text', '')
        t_kind = term.get('kind', 'word')
        if not t_text:
            continue
        if is_wildcard_rule(t_text):
            if _wildcard_matches(input_text, t_text, mode):
                return t_text
        else:
            if _term_matches(input_text, t_text, t_kind, mode):
                return t_text

    return None


# ─────────────────────────────────────────────
# Rendering (text)
# ─────────────────────────────────────────────

def _render_bw_main(chat_id: int, page: str = "main") -> str:
    settings = _bw_get_settings(chat_id)
    terms = _bw_get_terms(chat_id)
    allow_terms = _bw_get_allow_terms(chat_id)

    emoji_s = f'<tg-emoji emoji-id="{EMOJI_ROLE_SETTINGS_SENT_PM_ID}">⚙️</tg-emoji>'

    status_txt = "<code>включён</code>" if settings["enabled"] else "<code>выключен</code>"
    del_txt = "<code>включено</code>" if settings["delete_messages"] else "<code>выключено</code>"
    mode = settings["check_mode"]
    mode_label = _CHECK_MODE_LABELS.get(mode, mode)
    ptype = settings["punish"]["type"]
    dur = settings["punish"]["duration"]
    punish_label = _PUNISH_LABELS.get(ptype, ptype)
    dur_label = "нет" if ptype == "kick" else _mod_duration_text(int(dur or 0))
    terms_count = len(terms)
    allow_count = len(allow_terms)

    text = (
        f"{emoji_s} <b>Запрещённые слова</b>\n\n"
        f"Автоматически обнаруживает и удаляет сообщения с запрещёнными словами или фразами.\n\n"
        f"<b>Статус:</b> {status_txt}\n"
        f"<b>Режим проверки:</b> <code>{_html.escape(mode_label)}</code>\n"
        f"<b>Удаление сообщений:</b> {del_txt}\n"
        f"<b>Наказание:</b> <code>{_html.escape(punish_label)}</code>\n"
        f"<b>Длительность:</b> <code>{_html.escape(dur_label)}</code>\n"
        f"<b>Запрещённых слов/фраз:</b> <code>{terms_count}</code>\n"
        f"<b>Исключений:</b> <code>{allow_count}</code>"
    )

    hints: dict[str, str] = {
        "punish":       "\n\n<i>Выберите тип наказания за нарушение.</i>",
        "duration":     (
            "\n\nДля выбранного типа наказания длительность не используется."
            if ptype == "kick" else
            "\n\n<i>Установите длительность наказания.</i>"
        ),
        "terms":        "\n\n<i>Управление списком запрещённых слов/фраз.</i>",
        "terms_list":   "\n\n<i>Список запрещённых слов/фраз.</i>",
        "terms_delete": "\n\n<i>Выберите термин для удаления.</i>",
        "allow":        "\n\n<i>Управление исключениями (allowlist).</i>",
        "allow_list":   "\n\n<i>Список исключений.</i>",
        "allow_delete": "\n\n<i>Выберите исключение для удаления.</i>",
    }
    text += hints.get(page, "")
    text += f"\n\n{_DOC_LINK}"
    return text


def _render_bw_mode_screen(chat_id: int) -> str:
    """Текст для отдельного экрана «Режим проверки»."""
    settings = _bw_get_settings(chat_id)
    current_mode = settings["check_mode"]
    current_label = _CHECK_MODE_LABELS.get(current_mode, current_mode)

    text = (
        f"<b>Режим проверки</b>\n\n"
        f"Режим проверки определяет, как бот проверяет сообщение на наличие "
        f"запрещённых слов.\n\n"
        f"<b>Выбранный режим:</b> <code>{_html.escape(current_label)}</code>\n\n"
        f"{_DOC_LINK}"
    )

    return text


def _render_bw_mode_confirm(mode_key: str) -> str:
    """Текст подтверждения активации экспериментального режима."""
    return premium_prefix(
        "Внимание: этот режим является экспериментальным и вероятность ложных срабатываний "
        "повышается. Вы действительно хотите его активировать?"
    )
# Keyboards
# ─────────────────────────────────────────────

def _back_btn(cb: str) -> InlineKeyboardButton:
    b = InlineKeyboardButton("Назад", callback_data=cb)
    try:
        b.icon_custom_emoji_id = str(EMOJI_ROLE_SETTINGS_BACK_PREMIUM_ID)
        b.style = "primary"
    except Exception:
        pass
    return b


def _build_bw_mode_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    """Клавиатура для экрана «Режим проверки» — клик по режиму сразу выбирает его."""
    settings = _bw_get_settings(chat_id)
    current_mode = settings["check_mode"]

    kb = InlineKeyboardMarkup(row_width=2)

    # Кнопки режимов: [Точный][Нормализованный] / [Анти-разбивка][Анти-подмена] / [Агрессивный]
    mode_rows = [
        ["EXACT", "NORMALIZED"],
        ["SPLIT_PROOF", "CONFUSABLES"],
        ["AGGRESSIVE"],
    ]

    for row in mode_rows:
        btns = []
        for m_key in row:
            m_label = _CHECK_MODE_LABELS[m_key]
            is_current = (current_mode == m_key)
            title = f"»{m_label}«" if is_current else m_label
            b = InlineKeyboardButton(title, callback_data=f"stbw:mode_select:{chat_id}:{m_key}")
            try:
                if is_current:
                    b.style = "primary"
            except Exception:
                pass
            btns.append(b)
        kb.row(*btns)

    kb.add(_back_btn(f"stbw:mode_back:{chat_id}"))
    return kb


def _build_bw_mode_confirm_keyboard(chat_id: int, mode_key: str) -> InlineKeyboardMarkup:
    """Клавиатура подтверждения активации экспериментального режима."""
    kb = InlineKeyboardMarkup(row_width=2)
    b_yes = InlineKeyboardButton("Да", callback_data=f"stbw:mode_confirm_yes:{chat_id}:{mode_key}")
    b_no = InlineKeyboardButton("Нет", callback_data=f"stbw:mode_confirm_no:{chat_id}")
    kb.row(b_yes, b_no)
    return kb


def _build_bw_keyboard(chat_id: int, page: str = "main") -> InlineKeyboardMarkup:
    settings = _bw_get_settings(chat_id)
    enabled = settings["enabled"]
    delete_messages = settings["delete_messages"]
    mode = settings["check_mode"]
    ptype = settings["punish"]["type"]

    kb = InlineKeyboardMarkup(row_width=2)

    # ── 1) Статус ──
    b_status = InlineKeyboardButton(
        "Статус",
        callback_data=f"stbw:statusset:{chat_id}:{0 if enabled else 1}",
    )
    try:
        b_status.style = "success" if enabled else "danger"
    except Exception:
        pass
    kb.add(b_status)

    # ── 2) Удаление сообщений ──
    b_del = InlineKeyboardButton(
        "Удаление сообщений",
        callback_data=f"stbw:delset:{chat_id}:{0 if delete_messages else 1}",
    )
    try:
        b_del.style = "success" if delete_messages else "danger"
    except Exception:
        pass
    kb.add(b_del)

    # ── 3) Режим проверки → открывает отдельный экран ──
    b_mode = InlineKeyboardButton("Режим проверки", callback_data=f"stbw:page:{chat_id}:mode")
    kb.add(b_mode)

    # ── 4) Наказание и Длительность в одном ряду (expandable) ──
    p_title = "»Наказание«" if page == "punish" else "Наказание"
    d_title = "»Длительность«" if page == "duration" else "Длительность"
    b_punish = InlineKeyboardButton(p_title, callback_data=f"stbw:page:{chat_id}:punish")
    b_dur = InlineKeyboardButton(d_title, callback_data=f"stbw:page:{chat_id}:duration")
    try:
        if page == "punish":
            b_punish.style = "primary"
        if page == "duration":
            b_dur.style = "primary"
    except Exception:
        pass
    kb.row(b_punish, b_dur)

    if page == "punish":
        punish_pairs = [
            ("warn", "Предупреждение"),
            ("mute", "Ограничение"),
            ("ban", "Блокировка"),
            ("kick", "Исключение"),
        ]
        p_btns = []
        for pt_key, pt_label in punish_pairs:
            b = InlineKeyboardButton(pt_label, callback_data=f"stbw:ptype:{chat_id}:{pt_key}")
            try:
                if ptype == pt_key:
                    b.style = "primary"
            except Exception:
                pass
            p_btns.append(b)
        kb.row(p_btns[0], p_btns[1])
        kb.row(p_btns[2], p_btns[3])

    if page == "duration":
        b_set = InlineKeyboardButton("Установить длительность", callback_data=f"stbw:dur_prompt:{chat_id}")
        try:
            b_set.style = "primary"
        except Exception:
            pass
        kb.add(b_set)

    # ── 5) Список запрещённых (expandable) ──
    terms_count = len(_bw_get_terms(chat_id))
    terms_title = "»Список запрещённых«" if page in ("terms", "terms_list", "terms_delete") else "Список запрещённых"
    b_terms = InlineKeyboardButton(terms_title, callback_data=f"stbw:page:{chat_id}:terms")
    try:
        if page in ("terms", "terms_list", "terms_delete"):
            b_terms.style = "primary"
    except Exception:
        pass
    kb.add(b_terms)

    if page == "terms":
        b_list = InlineKeyboardButton("Список", callback_data=f"stbw:page:{chat_id}:terms_list")
        b_add = InlineKeyboardButton("Добавить", callback_data=f"stbw:term_add:{chat_id}")
        b_del_p = InlineKeyboardButton("Удалить", callback_data=f"stbw:term_del_prompt:{chat_id}")
        try:
            b_add.icon_custom_emoji_id = str(_EMOJI_ADD_ID)
            b_add.style = "primary"
            b_del_p.icon_custom_emoji_id = str(_EMOJI_DEL_ID)
            b_del_p.style = "primary"
        except Exception:
            pass
        kb.add(b_list)
        if terms_count < MAX_TERMS:
            kb.add(b_add)
        kb.add(b_del_p)

    if page == "terms_delete":
        terms = _bw_get_terms(chat_id)
        if terms:
            for idx, t in enumerate(terms[:30]):  # Показываем до 30 кнопок
                disp = t.get('text', '')
                disp_short = disp[:MAX_TERM_DISPLAY_LEN] + ('…' if len(disp) > MAX_TERM_DISPLAY_LEN else '')
                b_del = InlineKeyboardButton(
                    f"🗑 {disp_short}",
                    callback_data=f"stbw:term_del:{chat_id}:{idx}",
                )
                kb.add(b_del)
        else:
            kb.add(InlineKeyboardButton("Список пуст", callback_data=f"stbw:noop:{chat_id}"))
        kb.add(_back_btn(f"stbw:page:{chat_id}:terms"))
        return kb

    # ── 6) Управление исключениями (allowlist, expandable) ──
    allow_count = len(_bw_get_allow_terms(chat_id))
    allow_title = "»Управление исключениями«" if page in ("allow", "allow_list", "allow_delete") else "Управление исключениями"
    b_allow = InlineKeyboardButton(allow_title, callback_data=f"stbw:page:{chat_id}:allow")
    try:
        if page in ("allow", "allow_list", "allow_delete"):
            b_allow.style = "primary"
    except Exception:
        pass
    kb.add(b_allow)

    if page == "allow":
        b_list_a = InlineKeyboardButton("Список", callback_data=f"stbw:page:{chat_id}:allow_list")
        b_add_a = InlineKeyboardButton("Добавить", callback_data=f"stbw:allow_add:{chat_id}")
        b_del_a = InlineKeyboardButton("Удалить", callback_data=f"stbw:allow_del_prompt:{chat_id}")
        try:
            b_add_a.icon_custom_emoji_id = str(_EMOJI_ADD_ID)
            b_add_a.style = "primary"
            b_del_a.icon_custom_emoji_id = str(_EMOJI_DEL_ID)
            b_del_a.style = "primary"
        except Exception:
            pass
        kb.add(b_list_a)
        if allow_count < MAX_ALLOW_TERMS:
            kb.add(b_add_a)
        kb.add(b_del_a)

    if page == "allow_delete":
        allow_terms = _bw_get_allow_terms(chat_id)
        if allow_terms:
            for idx, t in enumerate(allow_terms[:30]):
                disp = t.get('text', '')
                disp_short = disp[:MAX_TERM_DISPLAY_LEN] + ('…' if len(disp) > MAX_TERM_DISPLAY_LEN else '')
                b_del = InlineKeyboardButton(
                    f"🗑 {disp_short}",
                    callback_data=f"stbw:allow_del:{chat_id}:{idx}",
                )
                kb.add(b_del)
        else:
            kb.add(InlineKeyboardButton("Список пуст", callback_data=f"stbw:noop:{chat_id}"))
        kb.add(_back_btn(f"stbw:page:{chat_id}:allow"))
        return kb

    # ── Назад ──
    kb.add(_back_btn(f"st_main:{chat_id}:filters"))
    return kb


def _build_bw_list_keyboard(
    chat_id: int, list_type: str, page_num: int = 0, total_pages: int = 1
) -> InlineKeyboardMarkup:
    """Клавиатура для страниц terms_list и allow_list (Назад + пагинация при необходимости)."""
    kb = InlineKeyboardMarkup(row_width=3)
    back_page = "terms" if list_type == "terms" else "allow"

    if total_pages > 1:
        b_prev = (
            InlineKeyboardButton("←", callback_data=f"stbw:list_nav:{chat_id}:{list_type}_{page_num - 1}")
            if page_num > 0
            else InlineKeyboardButton("\u2063", callback_data=f"stbw:noop:{chat_id}")
        )
        b_back = _back_btn(f"stbw:page:{chat_id}:{back_page}")
        b_next = (
            InlineKeyboardButton("→", callback_data=f"stbw:list_nav:{chat_id}:{list_type}_{page_num + 1}")
            if page_num < total_pages - 1
            else InlineKeyboardButton("\u2063", callback_data=f"stbw:noop:{chat_id}")
        )
        kb.row(b_prev, b_back, b_next)
    else:
        kb.add(_back_btn(f"stbw:page:{chat_id}:{back_page}"))

    return kb


def _bw_fmt_term_default(i: int, t: dict) -> str:
    return f"{i + 1}. <code>{_html.escape(t.get('text', ''))}</code>"


def _bw_render_list_page(
    items: list,
    header: str,
    page_num: int = 0,
    item_formatter=None,
) -> tuple[str, int]:
    """
    Формирует текст для страницы списка с учётом лимита Telegram.
    Возвращает (text, total_pages).
    """
    if item_formatter is None:
        item_formatter = _bw_fmt_term_default

    header_prefix = f"<b>{_html.escape(header)}</b>\n\n"

    if not items:
        return f"{header_prefix}Список пуст.", 1

    all_lines = [item_formatter(i, t) for i, t in enumerate(items)]

    # Разбиваем на страницы
    pages: list[list[str]] = [[]]
    cur_len = len(header_prefix)
    for line in all_lines:
        entry_len = len(line) + 1  # +1 for \n separator
        if cur_len + entry_len > _LIST_PAGE_MAX_CHARS and pages[-1]:
            pages.append([])
            cur_len = len(header_prefix)
        pages[-1].append(line)
        cur_len += entry_len

    total_pages = max(1, len(pages))
    page_num = max(0, min(page_num, total_pages - 1))

    body = "\n".join(pages[page_num])
    text = f"{header_prefix}{body}"
    if total_pages > 1:
        text += f"\n\n<i>Страница {page_num + 1} из {total_pages}</i>"

    return text, total_pages


# ─────────────────────────────────────────────
# Callbacks
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda c: bool(c.data) and c.data.startswith("stbw:"))
def cb_bw_settings(c: types.CallbackQuery) -> None:
    if _is_duplicate_callback_query(c):
        return

    data = c.data or ""
    user = c.from_user
    msg_chat = c.message.chat

    if msg_chat.type != "private":
        bot.answer_callback_query(c.id)
        return

    # stbw:<action>:<chat_id>[:<extra>]
    parts = data.split(":", 4)
    if len(parts) < 3:
        bot.answer_callback_query(c.id)
        return

    _, action = parts[0], parts[1]
    chat_id_s = parts[2] if len(parts) > 2 else ""
    extra = parts[3] if len(parts) > 3 else ""

    try:
        chat_id = int(chat_id_s)
    except ValueError:
        bot.answer_callback_query(c.id)
        return

    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        bot.answer_callback_query(c.id, err or "Недостаточно прав.", show_alert=True)
        return

    # Сброс pending-состояний при навигации (кроме prompt-действий)
    _prompt_actions = {"dur_prompt", "term_add", "term_del_prompt", "allow_add", "allow_del_prompt"}
    if action not in _prompt_actions:
        _bw_pending_pop("pending_bw_duration", user.id)
        _pending_msg_pop("pending_bw_duration_msg", user.id)
        _bw_pending_pop("pending_bw_add_term", user.id)
        _pending_msg_pop("pending_bw_add_term_msg", user.id)
        _bw_pending_pop("pending_bw_del_term", user.id)
        _pending_msg_pop("pending_bw_del_term_msg", user.id)
        _bw_pending_pop("pending_bw_add_allow", user.id)
        _pending_msg_pop("pending_bw_add_allow_msg", user.id)
        _bw_pending_pop("pending_bw_del_allow", user.id)
        _pending_msg_pop("pending_bw_del_allow_msg", user.id)

    # ── open ──
    if action == "open":
        text = _render_bw_main(chat_id)
        kb = _build_bw_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть раздел.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── noop ──
    if action == "noop":
        bot.answer_callback_query(c.id)
        return

    # ── page switch ──
    if action == "page":
        page = (extra or "main").strip()
        if page not in _VALID_PAGES:
            page = "main"

        # Страницы-списки: удаляем текущее сообщение и отправляем текстовый список
        if page == "terms_list":
            terms = _bw_get_terms(chat_id)
            def _fmt_term(i: int, t: dict) -> str:
                kind_text = f" <i>({_html.escape(t.get('kind', ''))})</i>" if t.get('kind') else ""
                return f"{i + 1}. <code>{_html.escape(t.get('text', ''))}</code>{kind_text}"
            list_text, total_pages = _bw_render_list_page(
                terms, "Запрещённые слова/фразы", 0, _fmt_term
            )
            try:
                bot.delete_message(msg_chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_message(
                msg_chat.id, list_text, parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_build_bw_list_keyboard(chat_id, "terms", 0, total_pages),
            )
            bot.answer_callback_query(c.id)
            return

        if page == "allow_list":
            allow_terms = _bw_get_allow_terms(chat_id)
            def _fmt_allow(i: int, t: dict) -> str:
                return f"{i + 1}. <code>{_html.escape(t.get('text', ''))}</code>"
            list_text, total_pages = _bw_render_list_page(
                allow_terms, "Исключения (allowlist)", 0, _fmt_allow
            )
            try:
                bot.delete_message(msg_chat.id, c.message.message_id)
            except Exception:
                pass
            bot.send_message(
                msg_chat.id, list_text, parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=_build_bw_list_keyboard(chat_id, "allow", 0, total_pages),
            )
            bot.answer_callback_query(c.id)
            return

        # «Режим проверки» → удаляем текущее сообщение, открываем отдельный экран
        if page == "mode":
            try:
                bot.delete_message(msg_chat.id, c.message.message_id)
            except Exception:
                pass
            mode_text = _render_bw_mode_screen(chat_id)
            mode_kb = _build_bw_mode_keyboard(chat_id)
            bot.send_message(
                msg_chat.id, mode_text, parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=mode_kb,
            )
            bot.answer_callback_query(c.id)
            return

        text = _render_bw_main(chat_id, page)
        kb = _build_bw_keyboard(chat_id, page)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось открыть страницу.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── mode screen: select mode (replaces mode_view + mode_enable/disable) ──
    if action == "mode_select":
        mode_key = (extra or "").strip().upper()
        if mode_key not in _CHECK_MODES:
            bot.answer_callback_query(c.id)
            return
        if mode_key in _EXPERIMENTAL_MODES:
            # Показываем подтверждение (редактируем текущее сообщение)
            confirm_text = _render_bw_mode_confirm(mode_key)
            confirm_kb = _build_bw_mode_confirm_keyboard(chat_id, mode_key)
            if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, confirm_text, confirm_kb):
                bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
                return
        else:
            # Выбираем режим напрямую
            s = _bw_get_settings(chat_id)
            s["check_mode"] = mode_key
            _bw_save_settings(chat_id, s)
            _PATTERN_CACHE.clear()
            mode_text = _render_bw_mode_screen(chat_id)
            mode_kb = _build_bw_mode_keyboard(chat_id)
            if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, mode_text, mode_kb):
                bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
                return
        bot.answer_callback_query(c.id)
        return

    # ── mode confirm: yes — активировать экспериментальный режим ──
    if action == "mode_confirm_yes":
        mode_key = (extra or "").strip().upper()
        if mode_key in _CHECK_MODES:
            s = _bw_get_settings(chat_id)
            s["check_mode"] = mode_key
            _bw_save_settings(chat_id, s)
            _PATTERN_CACHE.clear()
        mode_text = _render_bw_mode_screen(chat_id)
        mode_kb = _build_bw_mode_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, mode_text, mode_kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── mode confirm: no — вернуться без изменений ──
    if action == "mode_confirm_no":
        mode_text = _render_bw_mode_screen(chat_id)
        mode_kb = _build_bw_mode_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, mode_text, mode_kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── legacy: mode_view / mode_enable / mode_disable — перенаправляем на новый экран ──
    if action in ("mode_view", "mode_enable", "mode_disable"):
        if action == "mode_enable":
            mode_key = (extra or "").strip().upper()
            if mode_key in _CHECK_MODES:
                s = _bw_get_settings(chat_id)
                s["check_mode"] = mode_key
                _bw_save_settings(chat_id, s)
                _PATTERN_CACHE.clear()
        mode_text = _render_bw_mode_screen(chat_id)
        mode_kb = _build_bw_mode_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, mode_text, mode_kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── mode screen: back to main ──
    if action == "mode_back":
        text = _render_bw_main(chat_id)
        kb = _build_bw_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось вернуться.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── list pagination navigation ──
    if action == "list_nav":
        nav_parts = (extra or "").rsplit("_", 1)
        if len(nav_parts) != 2:
            bot.answer_callback_query(c.id)
            return
        list_type, page_num_s = nav_parts[0], nav_parts[1]
        try:
            page_num = int(page_num_s)
        except ValueError:
            bot.answer_callback_query(c.id)
            return
        if list_type == "terms":
            items = _bw_get_terms(chat_id)
            def _fmt_term_nav(i: int, t: dict) -> str:
                kind_text = f" <i>({_html.escape(t.get('kind', ''))})</i>" if t.get('kind') else ""
                return f"{i + 1}. <code>{_html.escape(t.get('text', ''))}</code>{kind_text}"
            list_text, total_pages = _bw_render_list_page(items, "Запрещённые слова/фразы", page_num, _fmt_term_nav)
        elif list_type == "allow":
            items = _bw_get_allow_terms(chat_id)
            def _fmt_allow_nav(i: int, t: dict) -> str:
                return f"{i + 1}. <code>{_html.escape(t.get('text', ''))}</code>"
            list_text, total_pages = _bw_render_list_page(items, "Исключения (allowlist)", page_num, _fmt_allow_nav)
        else:
            bot.answer_callback_query(c.id)
            return
        if not _show_warn_settings_ui(
            msg_chat.id, c.message.message_id, list_text,
            _build_bw_list_keyboard(chat_id, list_type, page_num, total_pages),
        ):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── status set ──
    if action == "statusset":
        s = _bw_get_settings(chat_id)
        s["enabled"] = (extra == "1")
        _bw_save_settings(chat_id, s)

    # ── delete messages set ──
    elif action == "delset":
        s = _bw_get_settings(chat_id)
        s["delete_messages"] = (extra == "1")
        _bw_save_settings(chat_id, s)

    # ── select check mode (legacy, kept for backwards compatibility) ──
    elif action == "modesel":
        mode = extra.strip().upper()
        if mode in _CHECK_MODES:
            s = _bw_get_settings(chat_id)
            s["check_mode"] = mode
            _bw_save_settings(chat_id, s)
            _PATTERN_CACHE.clear()
        text = _render_bw_main(chat_id)
        kb = _build_bw_keyboard(chat_id)
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── set punishment type ──
    elif action == "ptype":
        pt = (extra or "").strip().lower()
        if pt in ("warn", "mute", "ban", "kick"):
            s = _bw_get_settings(chat_id)
            s["punish"]["type"] = pt
            if pt == "kick":
                s["punish"]["duration"] = None
            elif s["punish"].get("duration") is None:
                s["punish"]["duration"] = 3600
            _bw_save_settings(chat_id, s)
        text = _render_bw_main(chat_id, "punish")
        kb = _build_bw_keyboard(chat_id, "punish")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── duration prompt ──
    elif action == "dur_prompt":
        s = _bw_get_settings(chat_id)
        if s["punish"]["type"] == "kick":
            bot.answer_callback_query(c.id, "Для исключения длительность не используется.", show_alert=True)
            return
        _bw_pending_put("pending_bw_duration", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_bw_duration_msg", user.id, also_msg_id=c.message.message_id)

        kb_p = InlineKeyboardMarkup(row_width=1)
        kb_p.add(_back_btn(f"stbw:page:{chat_id}:duration"))
        prompt_text = (
            "<b>Установите длительность наказания для «Запрещённых слов»</b>\n\n"
            "<b>Подсказка по интервалам:</b>\n"
            "<code>m</code> — минуты, <code>h</code> — часы, <code>d</code> — дни, <code>w</code> — недели\n"
            "<code>м</code> — минуты, <code>ч</code> — часы, <code>д</code> — дни, <code>н</code> — недели\n"
            "Можно комбинировать до <b>3</b> интервалов.\n\n"
            "<b>Примеры:</b> <code>10m</code>, <code>1h 30m</code>, <code>2д</code>, <code>навсегда</code>."
        )
        sent = bot.send_message(msg_chat.id, prompt_text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_p)
        _pending_msg_set("pending_bw_duration_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── add term prompt ──
    elif action == "term_add":
        terms = _bw_get_terms(chat_id)
        if len(terms) >= MAX_TERMS:
            bot.answer_callback_query(c.id, f"Достигнут лимит слов ({MAX_TERMS}).", show_alert=True)
            return
        _bw_pending_put("pending_bw_add_term", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_bw_add_term_msg", user.id, also_msg_id=c.message.message_id)

        kb_p = InlineKeyboardMarkup(row_width=1)
        kb_p.add(_back_btn(f"stbw:page:{chat_id}:terms"))
        prompt_text = (
            "<b>Добавить запрещённые слова/фразы</b>\n\n"
            "Отправьте одно или несколько слов — <b>каждое с новой строки</b>.\n"
            "Строка без пробелов → <i>слово</i>.\n"
            "Строка с пробелами → <i>фраза</i>.\n\n"
            "<b>Поддерживаются шаблоны:</b>\n"
            "<code>сло(*)во</code> — 0 или более любых символов.\n"
            "<code>бля(*)</code> — слово с любым продолжением (0+).\n"
            "<code>бля(+)</code> — слово с обязательным продолжением (1+, без пробела).\n"
            "<code>(*)бля(*)</code> — подстрока с любым окружением.\n\n"
            f"<b>Пример:</b>\n<code>плохоеслово\nплохая фраза\nсло(*)во\nбля(*)</code>\n\n"
            f"{_DOC_LINK}"
        )
        sent = bot.send_message(msg_chat.id, prompt_text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_p)
        _pending_msg_set("pending_bw_add_term_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── term delete prompt ──
    elif action == "term_del_prompt":
        terms = _bw_get_terms(chat_id)
        if not terms:
            bot.answer_callback_query(c.id, "Список запрещённых слов пуст.", show_alert=True)
            return
        _bw_pending_put("pending_bw_del_term", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_bw_del_term_msg", user.id, also_msg_id=c.message.message_id)

        kb_p = InlineKeyboardMarkup(row_width=1)
        kb_p.add(_back_btn(f"stbw:page:{chat_id}:terms"))
        prompt_text = (
            "<b>Удалить запрещённые слова/фразы</b>\n\n"
            "Отправьте слово или фразу для удаления.\n"
            "Можно несколько — каждое с новой строки.\n\n"
            f"{_DOC_LINK}"
        )
        sent = bot.send_message(msg_chat.id, prompt_text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_p)
        _pending_msg_set("pending_bw_del_term_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── term delete by index ──
    elif action == "term_del":
        try:
            idx = int(extra)
        except Exception:
            bot.answer_callback_query(c.id)
            return
        terms = list(_bw_get_terms(chat_id))
        if 0 <= idx < len(terms):
            terms.pop(idx)
            _bw_save_terms(chat_id, terms)
        text = _render_bw_main(chat_id, "terms_delete")
        kb = _build_bw_keyboard(chat_id, "terms_delete")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    # ── add allow term prompt ──
    elif action == "allow_add":
        allow_terms = _bw_get_allow_terms(chat_id)
        if len(allow_terms) >= MAX_ALLOW_TERMS:
            bot.answer_callback_query(c.id, f"Достигнут лимит исключений ({MAX_ALLOW_TERMS}).", show_alert=True)
            return
        _bw_pending_put("pending_bw_add_allow", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_bw_add_allow_msg", user.id, also_msg_id=c.message.message_id)

        kb_p = InlineKeyboardMarkup(row_width=1)
        kb_p.add(_back_btn(f"stbw:page:{chat_id}:allow"))
        prompt_text = (
            "<b>Добавить исключения (allowlist)</b>\n\n"
            "Отправьте одно или несколько слов/фраз — <b>каждое с новой строки</b>.\n"
            "Совпадение с исключением отменяет наказание.\n\n"
            f"<b>Пример:</b>\n<code>разрешённоеслово\nразрешённая фраза</code>\n\n"
            f"{_DOC_LINK}"
        )
        sent = bot.send_message(msg_chat.id, prompt_text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_p)
        _pending_msg_set("pending_bw_add_allow_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── allow delete prompt ──
    elif action == "allow_del_prompt":
        allow_terms = _bw_get_allow_terms(chat_id)
        if not allow_terms:
            bot.answer_callback_query(c.id, "Список исключений пуст.", show_alert=True)
            return
        _bw_pending_put("pending_bw_del_allow", user.id, chat_id)
        _delete_pending_ui(msg_chat.id, "pending_bw_del_allow_msg", user.id, also_msg_id=c.message.message_id)

        kb_p = InlineKeyboardMarkup(row_width=1)
        kb_p.add(_back_btn(f"stbw:page:{chat_id}:allow"))
        prompt_text = (
            "<b>Удалить исключения</b>\n\n"
            "Отправьте слово или фразу для удаления из allowlist.\n"
            "Можно несколько — каждое с новой строки.\n\n"
            f"{_DOC_LINK}"
        )
        sent = bot.send_message(msg_chat.id, prompt_text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup=kb_p)
        _pending_msg_set("pending_bw_del_allow_msg", user.id, sent.message_id)
        bot.answer_callback_query(c.id)
        return

    # ── allow delete by index ──
    elif action == "allow_del":
        try:
            idx = int(extra)
        except Exception:
            bot.answer_callback_query(c.id)
            return
        allow_terms = list(_bw_get_allow_terms(chat_id))
        if 0 <= idx < len(allow_terms):
            allow_terms.pop(idx)
            _bw_save_allow_terms(chat_id, allow_terms)
        text = _render_bw_main(chat_id, "allow_delete")
        kb = _build_bw_keyboard(chat_id, "allow_delete")
        if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
            bot.answer_callback_query(c.id, "Не удалось обновить.", show_alert=True)
            return
        bot.answer_callback_query(c.id)
        return

    else:
        bot.answer_callback_query(c.id)
        return

    # Обновляем главную страницу после изменения статуса/удаления
    text = _render_bw_main(chat_id)
    kb = _build_bw_keyboard(chat_id)
    if not _show_warn_settings_ui(msg_chat.id, c.message.message_id, text, kb):
        bot.answer_callback_query(c.id, "Не удалось обновить раздел.", show_alert=True)
        return
    bot.answer_callback_query(c.id)


# ─────────────────────────────────────────────
# Команды: /badd, /bdel, /btest
# ─────────────────────────────────────────────

@bot.message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and match_command(m, 'badd'),
)
def cmd_badd(m: types.Message) -> None:
    user = m.from_user
    chat_id = m.chat.id
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        return

    text = (m.text or "").strip()
    # Убираем команду: первый токен
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            m,
            "<b>/badd</b> — добавить запрещённые слова.\n"
            "<b>Использование:</b> <code>/badd слово</code> или <code>/badd плохая фраза</code>\n"
            "Несколько через новую строку.",
            parse_mode="HTML",
        )
        return

    raw_input = parts[1]
    new_terms = _parse_term_lines(raw_input)
    if not new_terms:
        bot.reply_to(m, premium_prefix("Не удалось распознать слова."), parse_mode="HTML")
        return

    added, skipped, errors, added_texts = _bw_add_terms(chat_id, new_terms)
    add_emoji = f'<tg-emoji emoji-id="{_EMOJI_ADD_ID}">✅</tg-emoji>'
    lines: list[str] = []
    if added:
        term_list = "\n".join(
            f"{i + 1}. <code>{_html.escape(t)}</code>"
            for i, t in enumerate(added_texts[:MAX_TERMS_IN_RESPONSE])
        )
        lines.append(f"{add_emoji} Добавлено: <b>{added}</b>\n{term_list}")
    if skipped:
        lines.append(f"ℹ️ Уже в списке: <b>{skipped}</b>")
    for e in errors[:3]:
        lines.append(premium_prefix(e))
    bot.reply_to(m, "\n".join(lines) if lines else premium_prefix("Нет изменений."), parse_mode="HTML")


@bot.message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and match_command(m, 'bdel'),
)
def cmd_bdel(m: types.Message) -> None:
    user = m.from_user
    chat_id = m.chat.id
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        return

    text = (m.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            m,
            "<b>/bdel</b> — удалить запрещённые слова.\n"
            "<b>Использование:</b> <code>/bdel слово</code>\n"
            "Несколько через новую строку.",
            parse_mode="HTML",
        )
        return

    raw_input = parts[1]
    deleted, not_found, deleted_texts = _bw_del_terms(chat_id, raw_input)
    del_emoji = f'<tg-emoji emoji-id="{_EMOJI_DEL_ID}">❌</tg-emoji>'
    lines: list[str] = []
    if deleted:
        term_list = "\n".join(
            f"{i + 1}. <code>{_html.escape(t)}</code>"
            for i, t in enumerate(deleted_texts[:MAX_TERMS_IN_RESPONSE])
        )
        lines.append(f"{del_emoji} Удалено: <b>{deleted}</b>\n{term_list}")
    for nf in not_found[:5]:
        lines.append(premium_prefix(f"Не найдено: <code>{_html.escape(nf)}</code>"))
    bot.reply_to(m, "\n".join(lines) if lines else premium_prefix("Нет изменений."), parse_mode="HTML")


@bot.message_handler(
    func=lambda m: m.chat.type in ('group', 'supergroup') and match_command(m, 'btest'),
)
def cmd_btest(m: types.Message) -> None:
    user = m.from_user
    chat_id = m.chat.id
    allowed, err = _user_can_open_settings(chat_id, user)
    if not allowed:
        return

    text = (m.text or "").strip()
    parts = text.split(None, 1)
    if len(parts) < 2 or not parts[1].strip():
        bot.reply_to(
            m,
            "<b>/btest</b> — проверить текст.\n"
            "<b>Использование:</b> <code>/btest текст для проверки</code>",
            parse_mode="HTML",
        )
        return

    test_text = parts[1]
    settings = _bw_get_settings(chat_id)
    mode = settings["check_mode"]
    mode_label = _CHECK_MODE_LABELS.get(mode, mode)

    matched = _bw_check_text(test_text, chat_id)
    if matched:
        bot.reply_to(
            m,
            f"⚠️ <b>Нарушение!</b>\n"
            f"Сработал термин: <code>{_html.escape(matched)}</code>\n"
            f"Режим: <code>{_html.escape(mode_label)}</code>",
            parse_mode="HTML",
        )
    else:
        enabled = settings["enabled"]
        if not enabled:
            note = " <i>(фильтр выключен)</i>"
        elif not _bw_get_terms(chat_id):
            note = " <i>(список запрещённых слов пуст)</i>"
        else:
            note = ""
        bot.reply_to(
            m,
            f"✅ <b>Нарушений нет</b>.{note}\n"
            f"Режим: <code>{_html.escape(mode_label)}</code>",
            parse_mode="HTML",
        )


# ─────────────────────────────────────────────
# Pending text input handler
# ─────────────────────────────────────────────

def handle_banwords_private_pending(m: types.Message) -> bool:
    """
    Обрабатывает pending-ввод для «Запрещённых слов» (вызывается из settings_ui.on_settings_private_input).
    Возвращает True, если сообщение обработано.
    """
    user_id = int(m.from_user.id)
    ct = getattr(m, "content_type", "text") or "text"

    # ── duration ──
    dur_cid = _bw_pending_get("pending_bw_duration", user_id)
    if dur_cid is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{dur_cid}:duration"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_duration_msg", user_id,
                premium_prefix("Пришлите длительность текстом: 30m, 2h, 3д, 1н или 'навсегда'."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(dur_cid, m.from_user)
        if not allowed:
            _bw_pending_pop("pending_bw_duration", user_id)
            _pending_msg_pop("pending_bw_duration_msg", user_id)
            return True

        raw = (m.text or "").strip()
        parsed_duration, consumed_tokens, invalid = _parse_duration_prefix(
            raw, allow_russian_duration=True, max_parts=3,
        )
        total_tokens = len(raw.split()) if raw else 0
        if invalid or parsed_duration is None or consumed_tokens == 0 or consumed_tokens != total_tokens:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{dur_cid}:duration"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_duration_msg", user_id,
                premium_prefix("Неверный формат. Используйте до 3 интервалов: 30m, 1h 2m, 2д, навсегда."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        duration = int(parsed_duration)
        if duration != 0 and (duration < MIN_PUNISH_SECONDS or duration > MAX_PUNISH_SECONDS):
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{dur_cid}:duration"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_duration_msg", user_id,
                premium_prefix("Длительность должна быть от 1 минуты до 365 дней, либо 'навсегда'."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        s = _bw_get_settings(dur_cid)
        ptype = s["punish"]["type"]
        if ptype == "kick":
            _bw_pending_pop("pending_bw_duration", user_id)
            _pending_msg_pop("pending_bw_duration_msg", user_id)
            bot.send_message(
                m.chat.id,
                premium_prefix("Для исключения длительность не используется."),
                parse_mode="HTML",
            )
            return True

        s["punish"]["duration"] = int(duration)
        _bw_save_settings(dur_cid, s)
        _bw_pending_pop("pending_bw_duration", user_id)
        prompt_id = _pending_msg_pop("pending_bw_duration_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        _try_delete_private_prompt(m.chat.id, m.message_id)

        kb_ok = InlineKeyboardMarkup()
        kb_ok.add(_back_btn(f"stbw:page:{dur_cid}:duration"))
        bot.send_message(
            m.chat.id, premium_prefix("✅ Длительность наказания установлена."),
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=kb_ok,
        )
        return True

    # ── add term ──
    add_cid = _bw_pending_get("pending_bw_add_term", user_id)
    if add_cid is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{add_cid}:terms"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_add_term_msg", user_id,
                premium_prefix("Пришлите список слов/фраз текстом (каждое с новой строки)."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(add_cid, m.from_user)
        if not allowed:
            _bw_pending_pop("pending_bw_add_term", user_id)
            _pending_msg_pop("pending_bw_add_term_msg", user_id)
            return True

        raw = (m.text or "").strip()
        if not raw:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{add_cid}:terms"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_add_term_msg", user_id,
                premium_prefix("Сообщение пустое. Пришлите слова/фразы."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        new_terms = _parse_term_lines(raw)
        added, skipped, errors, added_texts = _bw_add_terms(add_cid, new_terms)

        _bw_pending_pop("pending_bw_add_term", user_id)
        prompt_id = _pending_msg_pop("pending_bw_add_term_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        # Не удаляем сообщение пользователя с добавленными словами

        add_emoji = f'<tg-emoji emoji-id="{_EMOJI_ADD_ID}">✅</tg-emoji>'
        lines: list[str] = []
        if added:
            term_list = "\n".join(
                f"{i + 1}. <code>{_html.escape(t)}</code>"
                for i, t in enumerate(added_texts[:MAX_TERMS_IN_RESPONSE])
            )
            lines.append(f"{add_emoji} Добавлено: <b>{added}</b>\n{term_list}")
        if skipped:
            lines.append(f"ℹ️ Уже в списке: <b>{skipped}</b>")
        for e in errors[:3]:
            lines.append(premium_prefix(e))
        ok_text = "\n".join(lines) if lines else premium_prefix("Нет изменений.")
        kb_ok = InlineKeyboardMarkup()
        kb_ok.add(_back_btn(f"stbw:page:{add_cid}:terms"))
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    # ── delete term ──
    del_cid = _bw_pending_get("pending_bw_del_term", user_id)
    if del_cid is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{del_cid}:terms"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_del_term_msg", user_id,
                premium_prefix("Пришлите слово/фразу для удаления текстом."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(del_cid, m.from_user)
        if not allowed:
            _bw_pending_pop("pending_bw_del_term", user_id)
            _pending_msg_pop("pending_bw_del_term_msg", user_id)
            return True

        raw = (m.text or "").strip()
        if not raw:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{del_cid}:terms"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_del_term_msg", user_id,
                premium_prefix("Сообщение пустое."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        deleted, not_found, deleted_texts = _bw_del_terms(del_cid, raw)
        _bw_pending_pop("pending_bw_del_term", user_id)
        prompt_id = _pending_msg_pop("pending_bw_del_term_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        # Не удаляем сообщение пользователя с указанными словами

        del_emoji = f'<tg-emoji emoji-id="{_EMOJI_DEL_ID}">❌</tg-emoji>'
        lines_r: list[str] = []
        if deleted:
            term_list = "\n".join(
                f"{i + 1}. <code>{_html.escape(t)}</code>"
                for i, t in enumerate(deleted_texts[:MAX_TERMS_IN_RESPONSE])
            )
            lines_r.append(f"{del_emoji} Удалено: <b>{deleted}</b>\n{term_list}")
        for nf in not_found[:5]:
            lines_r.append(premium_prefix(f"Не найдено: <code>{_html.escape(nf)}</code>"))
        ok_text = "\n".join(lines_r) if lines_r else premium_prefix("Нет изменений.")
        kb_ok = InlineKeyboardMarkup()
        kb_ok.add(_back_btn(f"stbw:page:{del_cid}:terms"))
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    # ── add allow term ──
    add_allow_cid = _bw_pending_get("pending_bw_add_allow", user_id)
    if add_allow_cid is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{add_allow_cid}:allow"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_add_allow_msg", user_id,
                premium_prefix("Пришлите исключения текстом (каждое с новой строки)."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(add_allow_cid, m.from_user)
        if not allowed:
            _bw_pending_pop("pending_bw_add_allow", user_id)
            _pending_msg_pop("pending_bw_add_allow_msg", user_id)
            return True

        raw = (m.text or "").strip()
        if not raw:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{add_allow_cid}:allow"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_add_allow_msg", user_id,
                premium_prefix("Сообщение пустое."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        new_terms = _parse_term_lines(raw)
        added, skipped, errors, added_texts = _bw_add_allow_terms(add_allow_cid, new_terms)

        _bw_pending_pop("pending_bw_add_allow", user_id)
        prompt_id = _pending_msg_pop("pending_bw_add_allow_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        # Не удаляем сообщение пользователя с добавленными исключениями

        add_emoji = f'<tg-emoji emoji-id="{_EMOJI_ADD_ID}">✅</tg-emoji>'
        lines_a: list[str] = []
        if added:
            term_list = "\n".join(
                f"{i + 1}. <code>{_html.escape(t)}</code>"
                for i, t in enumerate(added_texts[:MAX_TERMS_IN_RESPONSE])
            )
            lines_a.append(f"{add_emoji} Добавлено: <b>{added}</b>\n{term_list}")
        if skipped:
            lines_a.append(f"ℹ️ Уже в списке: <b>{skipped}</b>")
        for e in errors[:3]:
            lines_a.append(premium_prefix(e))
        ok_text = "\n".join(lines_a) if lines_a else premium_prefix("Нет изменений.")
        kb_ok = InlineKeyboardMarkup()
        kb_ok.add(_back_btn(f"stbw:page:{add_allow_cid}:allow"))
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    # ── delete allow term ──
    del_allow_cid = _bw_pending_get("pending_bw_del_allow", user_id)
    if del_allow_cid is not None:
        if ct != "text":
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{del_allow_cid}:allow"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_del_allow_msg", user_id,
                premium_prefix("Пришлите исключение для удаления текстом."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        allowed, _ = _user_can_open_settings(del_allow_cid, m.from_user)
        if not allowed:
            _bw_pending_pop("pending_bw_del_allow", user_id)
            _pending_msg_pop("pending_bw_del_allow_msg", user_id)
            return True

        raw = (m.text or "").strip()
        if not raw:
            kb_err = InlineKeyboardMarkup(row_width=1)
            kb_err.add(_back_btn(f"stbw:page:{del_allow_cid}:allow"))
            _replace_pending_ui(
                m.chat.id, "pending_bw_del_allow_msg", user_id,
                premium_prefix("Сообщение пустое."),
                reply_markup=kb_err, parse_mode="HTML",
            )
            return True

        deleted, not_found, deleted_texts = _bw_del_allow_terms(del_allow_cid, raw)
        _bw_pending_pop("pending_bw_del_allow", user_id)
        prompt_id = _pending_msg_pop("pending_bw_del_allow_msg", user_id)
        _try_delete_private_prompt(m.chat.id, prompt_id)
        # Не удаляем сообщение пользователя с указанными исключениями

        del_emoji = f'<tg-emoji emoji-id="{_EMOJI_DEL_ID}">❌</tg-emoji>'
        lines_da: list[str] = []
        if deleted:
            term_list = "\n".join(
                f"{i + 1}. <code>{_html.escape(t)}</code>"
                for i, t in enumerate(deleted_texts[:MAX_TERMS_IN_RESPONSE])
            )
            lines_da.append(f"{del_emoji} Удалено: <b>{deleted}</b>\n{term_list}")
        for nf in not_found[:5]:
            lines_da.append(premium_prefix(f"Не найдено: <code>{_html.escape(nf)}</code>"))
        ok_text = "\n".join(lines_da) if lines_da else premium_prefix("Нет изменений.")
        kb_ok = InlineKeyboardMarkup()
        kb_ok.add(_back_btn(f"stbw:page:{del_allow_cid}:allow"))
        bot.send_message(m.chat.id, ok_text, parse_mode="HTML",
                         disable_web_page_preview=True, reply_markup=kb_ok)
        return True

    return False


# ─────────────────────────────────────────────
# Runtime: применение наказания
# ─────────────────────────────────────────────

def _bw_user_allowed(chat_id: int, user: types.User) -> bool:
    """Возвращает True если пользователь подлежит проверке (не является исключением)."""
    if not user:
        return False
    uid = int(getattr(user, "id", 0) or 0)
    if uid <= 0:
        return False
    if is_owner(user) or is_dev(user):
        return False
    try:
        if int(get_user_rank(chat_id, uid) or 0) > 0:
            return False
    except Exception:
        pass
    if bool(getattr(user, "is_bot", False)):
        return False
    bot_id = _get_bot_id()
    if bot_id and uid == bot_id:
        return False
    try:
        member = tg_get_chat_member(chat_id, uid)
        if getattr(member, "status", "") in ("administrator", "creator"):
            return False
    except Exception:
        pass
    return True


def _bw_apply_punishment(
    chat_id: int,
    user: types.User,
    settings: dict,
    message_id: int,
    matched_term: str,
) -> None:
    target_id = int(getattr(user, "id", 0) or 0)
    if target_id <= 0:
        return

    punish = settings.get("punish") or {}
    ptype = str(punish.get("type") or "warn").lower()
    duration_raw = punish.get("duration")
    reason = "Запрещённое слово"

    actor_id = _get_bot_id() or target_id

    if settings.get("delete_messages") and message_id:
        if _bot_can_delete_messages(chat_id):
            try:
                bot.delete_message(chat_id, message_id)
            except Exception:
                pass

    if ptype == "warn":
        action_id, count_after, _ = _mod_warn_add(chat_id, actor_id, target_id, reason)
        warn_limit = int((_mod_get_chat(chat_id).get("settings") or {}).get("warn_limit", 3))
        if count_after >= warn_limit:
            try:
                _auto_punish_for_warns(chat_id, get_bot_me(), target_id,
                                       source_tag="#ЗАПРЕЩЁННЫЕ_СЛОВА")
            except Exception:
                pass
        else:
            _send_punish_message_with_button(
                chat_id, "warn", action_id, target_id, actor_id,
                None, reason,
                warn_count=count_after, warn_limit=warn_limit,
            )
        _mod_log_action(chat_id, "warn", actor_id, target_id, reason=reason,
                        source_tag="#ЗАПРЕЩЁННЫЕ_СЛОВА")
        return

    if ptype == "kick":
        try:
            bot.ban_chat_member(chat_id, target_id)
            bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
            _mark_farewell_suppressed(chat_id, target_id)
        except AttributeError:
            try:
                bot.kick_chat_member(chat_id, target_id)
                bot.unban_chat_member(chat_id, target_id, only_if_banned=True)
                _mark_farewell_suppressed(chat_id, target_id)
            except Exception:
                return
        except Exception:
            return
        kick_action_id = _mod_new_action_id()
        kick_row = {
            "id": kick_action_id,
            "target_id": target_id,
            "actor_id": actor_id,
            "created_at": _time.time(),
            "duration": 0, "until": 0,
            "reason": reason, "active": True,
            "auto": True, "source": "banned_words",
        }
        _mod_log_append(chat_id, "kick", kick_row)
        try:
            emoji_p = f'<tg-emoji emoji-id="{EMOJI_UNPUNISH_ID}">⚠️</tg-emoji>'
            tname = link_for_user(chat_id, target_id)
            aname = link_for_user(chat_id, actor_id)
            txt = (
                f"{emoji_p} <b>Пользователь</b> {tname} <b>наказан.</b>\n"
                f"<b>Наказание:</b> Исключение\n"
                f"<b>Причина:</b> {_html.escape(reason)}\n\n"
                f"<b>Администратор:</b> {aname}"
            )
            bot.send_message(chat_id, txt, parse_mode="HTML", disable_web_page_preview=True)
        except Exception:
            pass
        _mod_log_action(chat_id, "kick", actor_id, target_id, reason=reason,
                        source_tag="#ЗАПРЕЩЁННЫЕ_СЛОВА")
        return

    # mute / ban
    try:
        duration = int(duration_raw) if duration_raw is not None else 3600
    except Exception:
        duration = 3600
    if duration != 0:
        duration = max(MIN_PUNISH_SECONDS, min(MAX_PUNISH_SECONDS, duration))

    until_ts = None
    ok = False
    if ptype == "ban":
        ok, _, until_ts = _apply_ban(chat_id, target_id, duration)
    else:
        ok, _, until_ts = _apply_mute(chat_id, target_id, duration)
        ptype = "mute"
    if not ok:
        return

    action_id = _mod_new_action_id()
    row = {
        "id": action_id,
        "target_id": target_id,
        "actor_id": actor_id,
        "created_at": _time.time(),
        "duration": int(duration or 0),
        "until": int(until_ts or 0),
        "reason": reason,
        "active": True,
        "auto": True,
        "source": "banned_words",
    }
    _mod_log_append(chat_id, ptype, row)
    ch = _mod_get_chat(chat_id)
    ch.setdefault("active", {}).setdefault(ptype, {})[str(target_id)] = {
        "id": action_id,
        "actor_id": actor_id,
        "created_at": row["created_at"],
        "duration": row["duration"],
        "until": row["until"],
        "reason": row["reason"],
    }
    _mod_save()
    _send_punish_message_with_button(
        chat_id, ptype, action_id, target_id, actor_id,
        int(duration or 0), reason,
        until_ts=int(until_ts or 0),
        created_at=row["created_at"],
    )
    _mod_log_action(chat_id, ptype, actor_id, target_id,
                    duration=int(duration or 0), reason=reason,
                    until_ts=int(until_ts or 0) if until_ts else None,
                    source_tag="#ЗАПРЕЩЁННЫЕ_СЛОВА")


def _bw_runtime_check(m: types.Message) -> None:
    chat_id = int(m.chat.id)
    if not is_group_approved(chat_id):
        return

    # Пропускаем сообщения от привязанного канала (sender_chat — канал)
    sender_chat = getattr(m, "sender_chat", None)
    if sender_chat and getattr(sender_chat, "type", None) == "channel":
        return

    user = getattr(m, "from_user", None)
    if not _bw_user_allowed(chat_id, user):
        return

    text = (getattr(m, "text", None) or getattr(m, "caption", None) or "")
    if not text:
        return

    msg_id = int(getattr(m, "message_id", 0) or 0)

    matched = _bw_check_text(text, chat_id)
    if matched:
        settings = _bw_get_settings(chat_id)
        _bw_apply_punishment(chat_id, user, settings, msg_id, matched)


_BW_CONTENT_TYPES = [
    "text", "photo", "video", "document", "audio", "animation",
    "sticker", "voice", "video_note",
]


@bot.message_handler(
    content_types=_BW_CONTENT_TYPES,
    func=lambda m: m.chat.type in ("group", "supergroup"),
)
def bw_runtime_handler(m: types.Message) -> None:
    try:
        _bw_runtime_check(m)
    except Exception:
        pass
    return ContinueHandling()


__all__ = [name for name in globals() if not name.startswith("__")]
