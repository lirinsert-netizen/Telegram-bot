"""
persistence.py — SQLite/JSON, throttled saves, TG кеш, callback-дедупликация,
загрузка всех state-словарей, STATS.
Импортирует только из config. Бизнес-логика — в helpers.py.
"""
from __future__ import annotations

import time
import threading
import atexit
import queue
from collections import Counter

from config import (
    # stdlib re-exports нужны для typing внутри этого модуля
    os, json, sqlite3, Any,
    # настройки
    DATA_DIR, SQLITE_DB_FILE, SQLITE_JSON_FALLBACK_WRITE,
    DB_FLUSH_INTERVAL_SECONDS, GLOBAL_LAST_SEEN_UPDATE_SECONDS,
    TG_CACHE_MEMBER_TTL, TG_CACHE_CHAT_TTL,
    # bot для кеша
    bot,
    # file paths
    GROUP_STATS_FILE, GROUP_SETTINGS_FILE, PROFILES_FILE, USERS_FILE,
    VERIFY_ADMINS_FILE, VERIFY_DEV_FILE, GLOBAL_USERS_FILE,
    CHAT_SETTINGS_FILE, MODERATION_FILE, DEV_CONTACT_INBOX_FILE,
    DEV_CONTACT_META_FILE, PENDING_GROUPS_FILE,
    CLOSE_CHAT_FILE, CHAT_ROLES_FILE, ROLE_PERMS_FILE,
    CLONES_FILE,
    # types
    types,
)

# ─────────────────────────── STATS (Перенесён наверх!) ───────────────────────────────────

STATS: dict[str, Any] = {
    'users': set(),
    'chats': set(),
    'messages': 0,
    'commands_used': {},
    'start_time': time.time(),
    'sqlite_errors': 0,
    'tg_cache_chat_misses': 0,
    'tg_cache_member_misses': 0,
    'tg_user_fetch_hits': 0,
    'tg_user_fetch_misses': 0,
}


def _stats_increment(key: str, delta: int = 1) -> None:
    STATS[key] = int(STATS.get(key) or 0) + delta


# ─────────────────────────── SQLite / JSON ───────────────────────────────────

_DB_LOCK = threading.RLock()
_DB_CONN: sqlite3.Connection | None = None
_SQLITE_RO_TLS = threading.local()

# ── Message-event buffer (per-message stats for time periods) ────────────────
_MSG_EVENTS_BUFFER: list[tuple[int, int, int, Any]] = []
_MSG_EVENTS_BUFFER_LOCK = threading.Lock()
_STATS_CLEANUP_LAST_TS: float = 0.0
_STATS_CLEANUP_INTERVAL: float = 86400.0  # run cleanup at most once per day
MSG_EVENTS_RETENTION_DAYS: int = 31       # keep events for this many days


def _db_connect() -> sqlite3.Connection:
    global _DB_CONN
    with _DB_LOCK:
        if _DB_CONN is not None:
            return _DB_CONN
        conn = sqlite3.connect(SQLITE_DB_FILE, check_same_thread=False, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA temp_store=MEMORY;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS kv_store (
                store_key TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """
        )
        # ── Per-message stats tables (time-period queries) ──────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS msg_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ts      INTEGER NOT NULL,
                msg_id  INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_events_chat_ts "
            "ON msg_events(chat_id, ts)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_msg_events_user "
            "ON msg_events(chat_id, user_id, ts)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS msg_alltime (
                chat_id     INTEGER NOT NULL,
                user_id     INTEGER NOT NULL,
                count       INTEGER NOT NULL DEFAULT 0,
                last_msg_id INTEGER,
                PRIMARY KEY (chat_id, user_id)
            )
            """
        )
        # ── Привязка бота к группе (один бот на группу) ─────────────────────
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_bot_assignment (
                chat_id      INTEGER PRIMARY KEY,
                bot_id       INTEGER NOT NULL,
                bot_username TEXT    NOT NULL,
                assigned_at  INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_bots (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id      INTEGER NOT NULL,
                bot_id             INTEGER NOT NULL UNIQUE,
                bot_username       TEXT    NOT NULL UNIQUE,
                bot_token          TEXT    NOT NULL UNIQUE,
                display_name       TEXT    NOT NULL DEFAULT '',
                status             TEXT    NOT NULL DEFAULT 'active',
                enabled            INTEGER NOT NULL DEFAULT 1,
                linked_modules_json TEXT   NOT NULL DEFAULT '[]',
                ai_access_mode     TEXT    NOT NULL DEFAULT 'all',
                ai_provider        TEXT    NOT NULL DEFAULT 'groq',
                runtime_pid        INTEGER,
                created_at         INTEGER NOT NULL,
                updated_at         INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guest_commands (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_bot_id   INTEGER NOT NULL,
                user_id        INTEGER NOT NULL DEFAULT 0,
                name           TEXT    NOT NULL,
                response_text  TEXT    NOT NULL DEFAULT '',
                media_items    TEXT    NOT NULL DEFAULT '[]',
                buttons_json   TEXT    NOT NULL DEFAULT '{"rows":[],"popups":[]}',
                enabled        INTEGER NOT NULL DEFAULT 1,
                owner_only     INTEGER NOT NULL DEFAULT 0,
                created_at     INTEGER NOT NULL,
                updated_at     INTEGER NOT NULL,
                UNIQUE(guest_bot_id, user_id, name),
                FOREIGN KEY(guest_bot_id) REFERENCES guest_bots(id) ON DELETE CASCADE
            )
            """
        )
        # Migration: add owner_only column to existing tables
        try:
            conn.execute("ALTER TABLE guest_commands ADD COLUMN owner_only INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE guest_commands ADD COLUMN media_items TEXT NOT NULL DEFAULT '[]'")
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("""ALTER TABLE guest_commands ADD COLUMN buttons_json TEXT NOT NULL DEFAULT '{"rows":[],"popups":[]}'""")
            conn.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add ai_access_mode for guest AI access policy
        try:
            conn.execute("ALTER TABLE guest_bots ADD COLUMN ai_access_mode TEXT NOT NULL DEFAULT 'all'")
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            conn.execute("ALTER TABLE guest_bots ADD COLUMN ai_provider TEXT NOT NULL DEFAULT 'groq'")
            conn.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add user_id to guest_commands and rebuild UNIQUE constraint
        try:
            cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(guest_commands)").fetchall()}
            if "user_id" not in cols:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS _guest_commands_v2 (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        guest_bot_id   INTEGER NOT NULL,
                        user_id        INTEGER NOT NULL DEFAULT 0,
                        name           TEXT    NOT NULL,
                        response_text  TEXT    NOT NULL DEFAULT '',
                        media_items    TEXT    NOT NULL DEFAULT '[]',
                        buttons_json   TEXT    NOT NULL DEFAULT '{"rows":[],"popups":[]}',
                        enabled        INTEGER NOT NULL DEFAULT 1,
                        owner_only     INTEGER NOT NULL DEFAULT 0,
                        created_at     INTEGER NOT NULL,
                        updated_at     INTEGER NOT NULL,
                        UNIQUE(guest_bot_id, user_id, name),
                        FOREIGN KEY(guest_bot_id) REFERENCES guest_bots(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO _guest_commands_v2
                    SELECT id, guest_bot_id, 0, name, response_text, media_items, buttons_json,
                           enabled, owner_only, created_at, updated_at
                    FROM guest_commands
                    """
                )
                conn.execute("DROP TABLE guest_commands")
                conn.execute("ALTER TABLE _guest_commands_v2 RENAME TO guest_commands")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.commit()
        except Exception as _e:
            print(f"[MIGRATION user_id] Error: {_e}")
        # Migration: move legacy user_id=0 commands to the bot owner so old commands keep working.
        try:
            ts = int(time.time())
            conn.execute(
                """
                UPDATE guest_commands AS gc
                SET user_id = (
                        SELECT gb.owner_user_id
                        FROM guest_bots gb
                        WHERE gb.id = gc.guest_bot_id
                    ),
                    updated_at = ?
                WHERE gc.user_id = 0
                  AND EXISTS (
                        SELECT 1
                        FROM guest_bots gb
                        WHERE gb.id = gc.guest_bot_id
                          AND gb.owner_user_id > 0
                    )
                  AND NOT EXISTS (
                        SELECT 1
                        FROM guest_commands dup
                        JOIN guest_bots gb2 ON gb2.id = gc.guest_bot_id
                        WHERE dup.guest_bot_id = gc.guest_bot_id
                          AND dup.user_id = gb2.owner_user_id
                          AND dup.name = gc.name
                          AND dup.id != gc.id
                    )
                """,
                (ts,),
            )
            conn.commit()
        except Exception as _e:
            print(f"[MIGRATION legacy owner scope] Error: {_e}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guest_commands_bot_enabled "
            "ON guest_commands(guest_bot_id, enabled)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_guest_commands_user "
            "ON guest_commands(guest_bot_id, user_id)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS log_channels (
                chat_id       INTEGER PRIMARY KEY,
                channel_id    INTEGER NOT NULL,
                channel_title TEXT,
                events        TEXT    NOT NULL DEFAULT '{}',
                created_at    INTEGER NOT NULL
            )
            """
        )
        conn.commit()
        _DB_CONN = conn
        return conn


def _db_key(path: str) -> str:
    return os.path.abspath(path)


def _reset_db_connection():
    global _DB_CONN
    with _DB_LOCK:
        if _DB_CONN is None:
            return
        try:
            _DB_CONN.close()
        except Exception:
            pass
        _DB_CONN = None


def _legacy_json_load(path: str, default: Any):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def _legacy_json_save(path: str, data: Any):
    """Атомарный fallback-save в legacy JSON."""
    try:
        tmp = path + ".tmp"
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        print(f"Ошибка fallback-сохранения JSON {path}: {e}")


def _known_json_store_paths() -> list[str]:
    import config as _cfg
    out: list[str] = []
    for name in dir(_cfg):
        if not name.endswith("_FILE"):
            continue
        value = getattr(_cfg, name, None)
        if not isinstance(value, str) or not value.endswith(".json"):
            continue
        out.append(value)
    return list(dict.fromkeys(out))


def migrate_legacy_json_to_sqlite() -> dict[str, int]:
    migrated = skipped = failed = 0
    dynamic_paths: list[str] = []
    try:
        for name in os.listdir(DATA_DIR):
            if name.endswith(".json"):
                dynamic_paths.append(os.path.join(DATA_DIR, name))
    except Exception:
        pass

    paths = list(dict.fromkeys(_known_json_store_paths() + dynamic_paths))
    for path in paths:
        if not os.path.exists(path):
            skipped += 1
            continue
        try:
            payload = _legacy_json_load(path, None)
            if payload is None:
                failed += 1
                continue
            if not save_json_file(path, payload):
                failed += 1
                continue
            key = _db_key(path)
            with _DB_LOCK:
                conn = _db_connect()
                row = conn.execute(
                    "SELECT payload_json FROM kv_store WHERE store_key = ?", (key,)
                ).fetchone()
            if not row:
                failed += 1
                continue
            migrated += 1
        except Exception:
            failed += 1
    return {"migrated": migrated, "skipped": skipped, "failed": failed, "total": len(paths)}


def get_sqlite_status() -> dict[str, Any]:
    status: dict[str, Any] = {
        "db_path": SQLITE_DB_FILE,
        "exists": os.path.exists(SQLITE_DB_FILE),
        "size_bytes": os.path.getsize(SQLITE_DB_FILE) if os.path.exists(SQLITE_DB_FILE) else 0,
        "rows": 0,
        "latest_updated_at": 0,
        "keys": [],
    }
    try:
        with _DB_LOCK:
            conn = _db_connect()
            status["rows"] = int(conn.execute("SELECT COUNT(*) FROM kv_store").fetchone()[0])
            latest = conn.execute("SELECT COALESCE(MAX(updated_at), 0) FROM kv_store").fetchone()[0]
            status["latest_updated_at"] = int(latest or 0)
            status["keys"] = [
                r[0] for r in conn.execute(
                    "SELECT store_key FROM kv_store ORDER BY updated_at DESC LIMIT 10"
                ).fetchall()
            ]
    except Exception as e:
        _stats_increment("sqlite_errors")
        status["error"] = str(e)
    return status


def _get_ro_db_conn() -> sqlite3.Connection:
    conn = getattr(_SQLITE_RO_TLS, "conn", None)
    if conn is None:
        _db_connect()  # <-- ГАРАНТИЯ СОЗДАНИЯ ТАБЛИЦ ПЕРЕД ЧТЕНИЕМ
        conn = sqlite3.connect(SQLITE_DB_FILE, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        _SQLITE_RO_TLS.conn = conn
    return conn


def load_json_file(path, default):
    key = _db_key(path)
    try:
        conn = _get_ro_db_conn()
        row = conn.execute(
            "SELECT payload_json FROM kv_store WHERE store_key = ?", (key,)
        ).fetchone()
        if row:
            return json.loads(row[0])
    except Exception:
        _stats_increment("sqlite_errors")

    legacy = _legacy_json_load(path, default)
    try:
        save_json_file(path, legacy)
    except Exception:
        pass
    return legacy


def save_json_file(path, data):
    key = _db_key(path)
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    ts = int(time.time())
    for attempt in range(2):
        try:
            with _DB_LOCK:
                conn = _db_connect()
                conn.execute(
                    """
                    INSERT INTO kv_store (store_key, payload_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(store_key) DO UPDATE SET
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (key, payload, ts),
                )
                conn.commit()
            return True
        except Exception as e:
            _stats_increment("sqlite_errors")
            print(f"Ошибка сохранения SQLite {path}: {e}")
            if attempt == 0:
                _reset_db_connection()
                continue

    if SQLITE_JSON_FALLBACK_WRITE:
        _legacy_json_save(path, data)
    return False


# ─────────────────────────── Throttled saves ─────────────────────────────────

_SAVE_LOCK = threading.Lock()
_SAVE_LAST_TS: dict[str, float] = {}
_SAVE_REGISTRY: dict[str, tuple[str, Any]] = {}
_SAVE_DIRTY_KEYS: set[str] = set()


def throttled_save_json_file(path: str, data: Any, key: str, force: bool = False):
    with _SAVE_LOCK:
        _SAVE_REGISTRY[key] = (path, data)
        _SAVE_DIRTY_KEYS.add(key)
        if force:
            _SAVE_LAST_TS[key] = 0.0


def _flush_pending_saves(force: bool = False):
    now = time.monotonic()
    to_write: list[tuple[str, Any]] = []
    with _SAVE_LOCK:
        for key in list(_SAVE_DIRTY_KEYS):
            item = _SAVE_REGISTRY.get(key)
            if not item:
                _SAVE_DIRTY_KEYS.discard(key)
                continue
            path, data = item
            last_ts = _SAVE_LAST_TS.get(key, 0.0)
            if force or (now - last_ts) >= DB_FLUSH_INTERVAL_SECONDS:
                _SAVE_LAST_TS[key] = now
                _SAVE_DIRTY_KEYS.discard(key)
                to_write.append((path, data))
    for path, data in to_write:
        save_json_file(path, data)


# ─────────────────────── Message-event stats helpers ─────────────────────────

def buffer_msg_event(
    chat_id: int, user_id: int, ts: int, msg_id: int | None = None
) -> None:
    with _MSG_EVENTS_BUFFER_LOCK:
        _MSG_EVENTS_BUFFER.append((int(chat_id), int(user_id), int(ts), msg_id))


def _flush_msg_events() -> None:
    with _MSG_EVENTS_BUFFER_LOCK:
        if not _MSG_EVENTS_BUFFER:
            return
        batch = list(_MSG_EVENTS_BUFFER)
        del _MSG_EVENTS_BUFFER[:]
    try:
        counts: dict[tuple[int, int], int] = Counter(
            (r[0], r[1]) for r in batch
        )
        last_msgs: dict[tuple[int, int], Any] = {}
        for r in batch:
            if r[3] is not None:
                last_msgs[(r[0], r[1])] = r[3]
        with _DB_LOCK:
            conn = _db_connect()
            conn.executemany(
                "INSERT INTO msg_events(chat_id, user_id, ts, msg_id) VALUES (?, ?, ?, ?)",
                [(r[0], r[1], r[2], r[3]) for r in batch],
            )
            for (cid, uid), delta in counts.items():
                last_mid = last_msgs.get((cid, uid))
                conn.execute(
                    """
                    INSERT INTO msg_alltime(chat_id, user_id, count, last_msg_id)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(chat_id, user_id) DO UPDATE SET
                        count = count + excluded.count,
                        last_msg_id = COALESCE(excluded.last_msg_id, last_msg_id)
                    """,
                    (cid, uid, delta, last_mid),
                )
            conn.commit()
    except Exception as e:
        print(f"[MSG EVENTS FLUSH] Error: {e}")


def _cleanup_old_msg_events() -> None:
    global _STATS_CLEANUP_LAST_TS
    now = time.monotonic()
    if now - _STATS_CLEANUP_LAST_TS < _STATS_CLEANUP_INTERVAL:
        return
    _STATS_CLEANUP_LAST_TS = now
    try:
        cutoff = int(time.time()) - MSG_EVENTS_RETENTION_DAYS * 86400
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute("DELETE FROM msg_events WHERE ts < ?", (cutoff,))
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            conn.commit()
    except Exception as e:
        print(f"[MSG EVENTS CLEANUP] Error: {e}")


def get_stats_for_period(
    chat_id: int, since_ts: int
) -> list[tuple[int, int, Any]]:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            if since_ts == 0:
                rows = conn.execute(
                    """
                    SELECT user_id, count, last_msg_id
                    FROM msg_alltime
                    WHERE chat_id = ?
                    ORDER BY count DESC
                    """,
                    (int(chat_id),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT user_id, COUNT(*) AS cnt, MAX(msg_id) AS last_msg_id
                    FROM msg_events
                    WHERE chat_id = ? AND ts >= ?
                    GROUP BY user_id
                    ORDER BY cnt DESC
                    """,
                    (int(chat_id), int(since_ts)),
                ).fetchall()
        return [(int(r[0]), int(r[1]), r[2]) for r in rows]
    except Exception as e:
        print(f"[GET STATS PERIOD] Error: {e}")
        return []


def get_user_msg_count_for_period(chat_id: int, user_id: int, since_ts: int) -> int:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM msg_events
                WHERE chat_id = ? AND user_id = ? AND ts >= ?
                """,
                (int(chat_id), int(user_id), int(since_ts)),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[GET USER MSG COUNT PERIOD] Error: {e}")
        return 0


def get_stats_by_day(
    chat_id: int, since_ts: int, max_days: int = 100
) -> list[tuple[int, int]]:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            rows = conn.execute(
                """
                SELECT (ts / 86400) * 86400 AS day_ts, COUNT(*) AS cnt
                FROM msg_events
                WHERE chat_id = ? AND ts >= ?
                GROUP BY day_ts
                ORDER BY day_ts DESC
                LIMIT ?
                """,
                (int(chat_id), int(since_ts), int(max_days)),
            ).fetchall()
        return [(int(r[0]), int(r[1])) for r in reversed(rows)]
    except Exception as e:
        print(f"[GET STATS BY DAY] Error: {e}")
        return []


def _periodic_flush_worker():
    sleep_seconds = max(1, DB_FLUSH_INTERVAL_SECONDS)
    while True:
        time.sleep(sleep_seconds)
        _flush_pending_saves(force=False)
        _flush_msg_events()
        _cleanup_old_msg_events()


def force_flush_all_saves():
    _flush_pending_saves(force=True)


def close_sqlite_connection():
    _reset_db_connection()


def _shutdown_persistence():
    force_flush_all_saves()
    _flush_msg_events()
    close_sqlite_connection()


_FLUSH_THREAD = threading.Thread(target=_periodic_flush_worker, daemon=True)
_FLUSH_THREAD.start()
atexit.register(_shutdown_persistence)

# ─────────────────────────── TG кеш / дедупликация ──────────────────────────

_TG_CACHE_LOCK = threading.Lock()
_TG_MEMBER_CACHE: dict[tuple[int, int], tuple[float, Any]] = {}
_TG_CHAT_CACHE: dict[str, tuple[float, Any]] = {}
_TG_FETCH_PROMISES: dict[Any, threading.Event] = {}
_CALLBACK_DEDUPE_LOCK = threading.Lock()
_CALLBACK_DEDUPE: dict[tuple[int, str, int], float] = {}

CALLBACK_DEDUPE_BUCKET_SECONDS = 5
CALLBACK_DEDUPE_KEEP_BUCKETS = 2

_CALLBACK_DEDUPE_CLEANUP_LOCK = threading.Lock()
_CALLBACK_DEDUPE_CLEANUP_STATE: dict[str, float] = {"last": 0.0}
_CALLBACK_DEDUPE_CLEANUP_INTERVAL: float = 30.0


def _callback_dedupe_cleanup_stale() -> None:
    current_bucket = int(time.time() // CALLBACK_DEDUPE_BUCKET_SECONDS)
    min_bucket = current_bucket - CALLBACK_DEDUPE_KEEP_BUCKETS
    with _CALLBACK_DEDUPE_LOCK:
        keys_snapshot = list(_CALLBACK_DEDUPE.keys())
    stale_keys = [k for k in keys_snapshot if k[2] < min_bucket]
    if stale_keys:
        with _CALLBACK_DEDUPE_LOCK:
            for sk in stale_keys:
                _CALLBACK_DEDUPE.pop(sk, None)


def _tg_chat_cache_key(chat_ref: Any) -> str:
    if isinstance(chat_ref, str):
        return f"s:{chat_ref.strip().lower()}"
    try:
        return f"i:{int(chat_ref)}"
    except Exception:
        return f"o:{str(chat_ref)}"


def tg_get_chat(chat_ref: Any):
    key = _tg_chat_cache_key(chat_ref)
    now = time.monotonic()
    
    with _TG_CACHE_LOCK:
        cached = _TG_CHAT_CACHE.get(key)
        if cached and (now - cached[0]) < TG_CACHE_CHAT_TTL:
            return cached[1]
            
        event = _TG_FETCH_PROMISES.get(key)
        if event is None:
            event = threading.Event()
            _TG_FETCH_PROMISES[key] = event
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        event.wait()
        with _TG_CACHE_LOCK:
            return _TG_CHAT_CACHE.get(key, (0, None))[1]

    _stats_increment("tg_cache_chat_misses")
    try:
        chat = bot.get_chat(chat_ref)
    finally:
        with _TG_CACHE_LOCK:
            if 'chat' in locals():
                _TG_CHAT_CACHE[key] = (time.monotonic(), chat)
            _TG_FETCH_PROMISES.pop(key, None)
            event.set()

    return chat


def tg_get_chat_member(chat_id: int, user_id: int):
    key = (int(chat_id), int(user_id))
    now = time.monotonic()
    
    with _TG_CACHE_LOCK:
        cached = _TG_MEMBER_CACHE.get(key)
        if cached and (now - cached[0]) < TG_CACHE_MEMBER_TTL:
            return cached[1]
            
        promise_key = f"m:{chat_id}:{user_id}"
        event = _TG_FETCH_PROMISES.get(promise_key)
        if event is None:
            event = threading.Event()
            _TG_FETCH_PROMISES[promise_key] = event
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        event.wait()
        with _TG_CACHE_LOCK:
            return _TG_MEMBER_CACHE.get(key, (0, None))[1]

    _stats_increment("tg_cache_member_misses")
    try:
        member = bot.get_chat_member(chat_id, user_id)
    finally:
        with _TG_CACHE_LOCK:
            if 'member' in locals():
                _TG_MEMBER_CACHE[key] = (time.monotonic(), member)
            _TG_FETCH_PROMISES.pop(promise_key, None)
            event.set()

    return member


def tg_invalidate_member_cache(chat_id: int, user_id: int) -> None:
    key = (int(chat_id), int(user_id))
    with _TG_CACHE_LOCK:
        _TG_MEMBER_CACHE.pop(key, None)


def tg_invalidate_chat_cache(chat_ref: Any) -> None:
    key = _tg_chat_cache_key(chat_ref)
    with _TG_CACHE_LOCK:
        _TG_CHAT_CACHE.pop(key, None)


def tg_invalidate_chat_member_caches(chat_id: int, user_id: int) -> None:
    tg_invalidate_member_cache(chat_id, user_id)
    tg_invalidate_chat_cache(chat_id)


_USER_FETCH_TLS = threading.local()


def tg_user_fetch_scope_reset() -> None:
    d = getattr(_USER_FETCH_TLS, "by_id", None)
    if d is not None:
        d.clear()


def tg_get_user_by_id_cached(user_id: int) -> Any:
    uid = int(user_id)
    d = getattr(_USER_FETCH_TLS, "by_id", None)
    if d is None:
        d = {}
        _USER_FETCH_TLS.by_id = d
    hit = uid in d
    if hit:
        _stats_increment("tg_user_fetch_hits")
        return d[uid]
    _stats_increment("tg_user_fetch_misses")
    try:
        obj = bot.get_chat(uid)
    except Exception:
        obj = types.User(uid, False, first_name="", last_name=None, username=None)
    d[uid] = obj
    return obj


def install_telebot_user_fetch_cache_hooks() -> None:
    from telebot import TeleBot

    if getattr(TeleBot, "_user_fetch_cache_patched", False):
        return

    def _exec_task(self, task, *args, **kwargs):
        if self.threaded:
            def wrapped(*a, **kw):
                tg_user_fetch_scope_reset()
                return task(*a, **kw)
            self.worker_pool.put(wrapped, *args, **kwargs)
        else:
            try:
                tg_user_fetch_scope_reset()
                task(*args, **kwargs)
            except Exception as e:
                handled = self._handle_exception(e)
                if not handled:
                    raise e

    TeleBot._exec_task = _exec_task
    TeleBot._user_fetch_cache_patched = True


def get_tg_cache_stats() -> dict[str, int]:
    with _TG_CACHE_LOCK:
        member_size = len(_TG_MEMBER_CACHE)
        chat_size = len(_TG_CHAT_CACHE)
    return {
        'member_size': member_size,
        'chat_size': chat_size,
        'total_size': member_size + chat_size,
        'member_misses': int(STATS.get('tg_cache_member_misses') or 0),
        'chat_misses': int(STATS.get('tg_cache_chat_misses') or 0),
        'total_misses': (
            int(STATS.get('tg_cache_member_misses') or 0)
            + int(STATS.get('tg_cache_chat_misses') or 0)
        ),
        'user_fetch_hits': int(STATS.get('tg_user_fetch_hits') or 0),
        'user_fetch_misses': int(STATS.get('tg_user_fetch_misses') or 0),
    }


def _is_duplicate_callback_query(call: types.CallbackQuery) -> bool:
    data = (call.data or "").strip()
    if not data:
        return False
    user_id = int(getattr(call.from_user, "id", 0) or 0)
    bucket = int(time.time() // CALLBACK_DEDUPE_BUCKET_SECONDS)
    key = (user_id, data, bucket)
    now_mono = time.monotonic()
    should_cleanup = False
    with _CALLBACK_DEDUPE_CLEANUP_LOCK:
        if now_mono - _CALLBACK_DEDUPE_CLEANUP_STATE["last"] >= _CALLBACK_DEDUPE_CLEANUP_INTERVAL:
            _CALLBACK_DEDUPE_CLEANUP_STATE["last"] = now_mono
            should_cleanup = True
    if should_cleanup:
        threading.Thread(target=_callback_dedupe_cleanup_stale, daemon=True).start()
    with _CALLBACK_DEDUPE_LOCK:
        if key in _CALLBACK_DEDUPE:
            try:
                bot.answer_callback_query(call.id)
            except Exception:
                pass
            return True
        _CALLBACK_DEDUPE[key] = time.monotonic()
    return False

# ─────────────────────────── Загрузка state-словарей ─────────────────────────

VERIFY_ADMINS: dict = load_json_file(VERIFY_ADMINS_FILE, {})
VERIFY_DEV: set = set(load_json_file(VERIFY_DEV_FILE, []))

DEV_CONTACT_INBOX: dict = load_json_file(DEV_CONTACT_INBOX_FILE, {"last_id": 0, "items": []})
if not isinstance(DEV_CONTACT_INBOX, dict):
    DEV_CONTACT_INBOX = {"last_id": 0, "items": []}
DEV_CONTACT_INBOX.setdefault("last_id", 0)
DEV_CONTACT_INBOX.setdefault("items", [])

DEV_CONTACT_META: dict = load_json_file(DEV_CONTACT_META_FILE, {})
if not isinstance(DEV_CONTACT_META, dict):
    DEV_CONTACT_META = {}

CLOSE_CHAT_STATE: dict = load_json_file(CLOSE_CHAT_FILE, {})

GROUP_STATS: dict = load_json_file(GROUP_STATS_FILE, {})
GROUP_SETTINGS: dict = load_json_file(GROUP_SETTINGS_FILE, {})

CHAT_SETTINGS: dict = load_json_file(CHAT_SETTINGS_FILE, {})
MODERATION: dict = load_json_file(MODERATION_FILE, {})
PENDING_GROUPS: dict = load_json_file(PENDING_GROUPS_FILE, {})

USERS: dict = load_json_file(USERS_FILE, {})
GLOBAL_USERS: dict = load_json_file(GLOBAL_USERS_FILE, {})
PROFILES: dict = load_json_file(PROFILES_FILE, {})

CHAT_ROLES: dict = load_json_file(CHAT_ROLES_FILE, {})
ROLE_PERMS: dict = load_json_file(ROLE_PERMS_FILE, {})

PENDING_DEV_CONTACT_FROM_USER: dict[int, dict] = {}
PENDING_DEV_REPLY_FROM_OWNER: dict[int, dict] = {}
BROADCAST_DRAFTS: dict[int, dict] = {}
BROADCAST_PENDING_INPUT: dict[int, dict] = {}
SENDPM_DRAFTS: dict[int, dict] = {}
SENDPM_PENDING_INPUT: dict[int, dict] = {}
PENDING_LOG_CHANNEL_SETUP: dict[int, dict] = {}

_OPERATION_QUEUE: queue.Queue[dict[str, Any]] = queue.Queue()
_OPERATION_QUEUE_LOCK = threading.Lock()
_OPERATION_QUEUE_ACTIVE: dict[int, dict[str, Any]] = {}
_OPERATION_QUEUE_NEXT_ID = 0

OPERATION_QUEUE_MAX_RETRIES = 4
OPERATION_QUEUE_MAX_BACKOFF_SECONDS = 30


# ─────────────────────────── Чистые save-функции ─────────────────────────────

def save_verify_admins():
    throttled_save_json_file(VERIFY_ADMINS_FILE, VERIFY_ADMINS, "verify_admins")

def save_verify_dev():
    save_json_file(VERIFY_DEV_FILE, list(VERIFY_DEV))

def save_dev_contact_inbox():
    save_json_file(DEV_CONTACT_INBOX_FILE, DEV_CONTACT_INBOX)

def save_dev_contact_meta():
    save_json_file(DEV_CONTACT_META_FILE, DEV_CONTACT_META)

def save_close_chat_state():
    save_json_file(CLOSE_CHAT_FILE, CLOSE_CHAT_STATE)

def save_group_stats():
    throttled_save_json_file(GROUP_STATS_FILE, GROUP_STATS, "group_stats")

def save_group_settings():
    throttled_save_json_file(GROUP_SETTINGS_FILE, GROUP_SETTINGS, "group_settings")

def save_chat_settings():
    throttled_save_json_file(CHAT_SETTINGS_FILE, CHAT_SETTINGS, "chat_settings")

def save_moderation():
    throttled_save_json_file(MODERATION_FILE, MODERATION, "moderation")

def save_pending_groups():
    save_json_file(PENDING_GROUPS_FILE, PENDING_GROUPS)

def save_users():
    throttled_save_json_file(USERS_FILE, USERS, "users")

def save_global_users():
    throttled_save_json_file(GLOBAL_USERS_FILE, GLOBAL_USERS, "global_users")

def save_profiles():
    throttled_save_json_file(PROFILES_FILE, PROFILES, "profiles")

def save_chat_roles():
    throttled_save_json_file(CHAT_ROLES_FILE, CHAT_ROLES, "chat_roles")

def save_role_perms():
    throttled_save_json_file(ROLE_PERMS_FILE, ROLE_PERMS, "role_perms")

def save_clones():
    save_json_file(CLONES_FILE, CLONES)


# ──────────────────── chat_bot_assignment helpers ────────────────────────────

def assign_bot_to_chat(chat_id: int, bot_id: int, bot_username: str) -> bool:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            existing = conn.execute(
                "SELECT bot_id FROM chat_bot_assignment WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
            if existing:
                return int(existing[0]) == int(bot_id)
            conn.execute(
                "INSERT INTO chat_bot_assignment(chat_id, bot_id, bot_username, assigned_at) "
                "VALUES (?, ?, ?, ?)",
                (int(chat_id), int(bot_id), str(bot_username), ts),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[ASSIGN BOT] Error: {e}")
        return True


def unassign_bot_from_chat(chat_id: int) -> None:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                "DELETE FROM chat_bot_assignment WHERE chat_id = ?",
                (int(chat_id),),
            )
            conn.commit()
    except Exception as e:
        print(f"[UNASSIGN BOT] Error: {e}")


def get_chat_assignment(chat_id: int) -> dict | None:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute(
                "SELECT bot_id, bot_username, assigned_at FROM chat_bot_assignment WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
        if row:
            return {"bot_id": int(row[0]), "bot_username": str(row[1]), "assigned_at": int(row[2])}
        return None
    except Exception as e:
        print(f"[GET ASSIGNMENT] Error: {e}")
        return None


def get_all_bot_chat_ids() -> list[int]:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            rows = conn.execute("SELECT chat_id FROM chat_bot_assignment").fetchall()
        return [int(r[0]) for r in rows]
    except Exception as e:
        print(f"[GET ALL BOT CHATS] Error: {e}")
        return []


def reassign_bot_in_chat(chat_id: int, bot_id: int, bot_username: str) -> None:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                """
                INSERT INTO chat_bot_assignment(chat_id, bot_id, bot_username, assigned_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    bot_username = excluded.bot_username,
                    assigned_at = excluded.assigned_at
                """,
                (int(chat_id), int(bot_id), str(bot_username), ts),
            )
            conn.commit()
    except Exception as e:
        print(f"[REASSIGN BOT] Error: {e}")


# ──────────────────── guest_bots helpers ──────────────────────────────────────

def _guest_modules_to_json(modules: list[str] | None) -> str:
    norm: list[str] = []
    for item in (modules or []):
        value = str(item).strip().lower()
        if not value or value in norm:
            continue
        if not value.replace("_", "").replace("-", "").isalnum():
            continue
        norm.append(value)
    if not norm:
        norm = ["commands"]
    return json.dumps(norm, ensure_ascii=False)


def _normalize_ai_access_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    return "owner" if mode == "owner" else "all"


def _normalize_ai_provider(value: str | None) -> str:
    provider = str(value or "").strip().lower()
    if provider in {"gemini", "gemini google ai studio", "gemini_google_ai_studio"}:
        return "gemini_google_ai_studio"
    return "groq"


def _guest_bot_row_to_dict(row: sqlite3.Row | tuple | None) -> dict | None:
    if not row:
        return None
    (
        gid, owner_uid, bot_id, bot_username, bot_token, display_name,
        status, enabled, runtime_pid, ai_access_mode, ai_provider,
        created_at, updated_at, linked_modules_json,
    ) = row
    data = {
        "id": int(gid),
        "owner_user_id": int(owner_uid),
        "bot_id": int(bot_id),
        "bot_username": str(bot_username or ""),
        "bot_token": str(bot_token or ""),
        "display_name": str(display_name or ""),
        "status": str(status or "active"),
        "enabled": bool(int(enabled or 0)),
        "runtime_pid": int(runtime_pid or 0),
        "ai_access_mode": _normalize_ai_access_mode(ai_access_mode),
        "ai_provider": _normalize_ai_provider(ai_provider),
        "created_at": int(created_at or 0),
        "updated_at": int(updated_at or 0),
    }
    try:
        data["linked_modules"] = json.loads(linked_modules_json or "[]")
    except Exception:
        data["linked_modules"] = ["commands"]
    if not isinstance(data["linked_modules"], list):
        data["linked_modules"] = ["commands"]
    return data


def list_guest_bots(owner_user_id: int | None = None) -> list[dict]:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            if owner_user_id is None:
                rows = conn.execute(
                    """
                    SELECT id, owner_user_id, bot_id, bot_username, bot_token, display_name,
                           status, enabled, runtime_pid, ai_access_mode, ai_provider, created_at, updated_at, linked_modules_json
                    FROM guest_bots
                    ORDER BY created_at DESC
                    """
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, owner_user_id, bot_id, bot_username, bot_token, display_name,
                           status, enabled, runtime_pid, ai_access_mode, ai_provider, created_at, updated_at, linked_modules_json
                    FROM guest_bots
                    WHERE owner_user_id = ?
                    ORDER BY created_at DESC
                    """,
                    (int(owner_user_id),),
                ).fetchall()
        return [item for item in (_guest_bot_row_to_dict(r) for r in rows) if item]
    except Exception as e:
        print(f"[LIST GUEST BOTS] Error: {e}")
        return []


def get_guest_bot_by_id(guest_bot_id: int) -> dict | None:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute(
                """
                SELECT id, owner_user_id, bot_id, bot_username, bot_token, display_name,
                       status, enabled, runtime_pid, ai_access_mode, ai_provider, created_at, updated_at, linked_modules_json
                FROM guest_bots
                WHERE id = ?
                """,
                (int(guest_bot_id),),
            ).fetchone()
        return _guest_bot_row_to_dict(row)
    except Exception as e:
        print(f"[GET GUEST BOT] Error: {e}")
        return None


def get_guest_bot_by_username(bot_username: str) -> dict | None:
    uname = str(bot_username or "").strip().lstrip("@").lower()
    if not uname:
        return None
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute(
                """
                SELECT id, owner_user_id, bot_id, bot_username, bot_token, display_name,
                       status, enabled, runtime_pid, ai_access_mode, ai_provider, created_at, updated_at, linked_modules_json
                FROM guest_bots
                WHERE lower(bot_username) = ?
                """,
                (uname,),
            ).fetchone()
        return _guest_bot_row_to_dict(row)
    except Exception as e:
        print(f"[GET GUEST BOT BY USERNAME] Error: {e}")
        return None


def create_guest_bot(
    owner_user_id: int, bot_id: int, bot_username: str, bot_token: str,
    display_name: str, linked_modules: list[str] | None = None,
) -> tuple[bool, str, dict | None]:
    uname = str(bot_username or "").strip().lstrip("@").lower()
    token = str(bot_token or "").strip()
    if not owner_user_id or not bot_id or not uname or not token:
        return False, "Некорректные данные guest-бота.", None

    ts = int(time.time())
    modules_json = _guest_modules_to_json(linked_modules)
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conflict = conn.execute(
                """
                SELECT id, owner_user_id, bot_id, bot_username, bot_token, display_name,
                       status, enabled, runtime_pid, ai_access_mode, ai_provider, created_at, updated_at, linked_modules_json
                FROM guest_bots
                WHERE bot_id = ? OR lower(bot_username) = ? OR bot_token = ?
                LIMIT 1
                """,
                (int(bot_id), uname, token),
            ).fetchone()
            if conflict:
                existing = _guest_bot_row_to_dict(conflict)
                return False, "Этот guest-бот уже зарегистрирован.", existing

            conn.execute(
                """
                INSERT INTO guest_bots(
                    owner_user_id, bot_id, bot_username, bot_token, display_name,
                    status, enabled, linked_modules_json, ai_access_mode, ai_provider, runtime_pid, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'active', 1, ?, 'all', 'groq', 0, ?, ?)
                """,
                (int(owner_user_id), int(bot_id), uname, token, str(display_name or uname), modules_json, ts, ts),
            )
            guest_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()
        return True, "", get_guest_bot_by_id(guest_id)
    except Exception as e:
        print(f"[CREATE GUEST BOT] Error: {e}")
        return False, "Не удалось сохранить guest-бота.", None


def set_guest_bot_enabled(guest_bot_id: int, enabled: bool) -> bool:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                "UPDATE guest_bots SET enabled = ?, status = ?, updated_at = ? WHERE id = ?",
                (1 if enabled else 0, "active" if enabled else "disabled", ts, int(guest_bot_id)),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[SET GUEST BOT ENABLED] Error: {e}")
        return False


def update_guest_bot_modules(guest_bot_id: int, linked_modules: list[str]) -> bool:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                "UPDATE guest_bots SET linked_modules_json = ?, updated_at = ? WHERE id = ?",
                (_guest_modules_to_json(linked_modules), ts, int(guest_bot_id)),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[UPDATE GUEST BOT MODULES] Error: {e}")
        return False


def set_guest_bot_runtime_pid(guest_bot_id: int, pid: int) -> bool:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                "UPDATE guest_bots SET runtime_pid = ?, updated_at = ? WHERE id = ?",
                (int(pid or 0), ts, int(guest_bot_id)),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[SET GUEST BOT PID] Error: {e}")
        return False


def delete_guest_bot(guest_bot_id: int) -> bool:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute("DELETE FROM guest_bots WHERE id = ?", (int(guest_bot_id),))
            conn.commit()
        return True
    except Exception as e:
        print(f"[DELETE GUEST BOT] Error: {e}")
        return False


def list_guest_commands(guest_bot_id: int, enabled_only: bool = False, user_id: int | None = None) -> list[dict]:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            where = "guest_bot_id = ?"
            params: list = [int(guest_bot_id)]
            if enabled_only:
                where += " AND enabled = 1"
            if user_id is not None:
                where += " AND user_id = ?"
                params.append(int(user_id))
            rows = conn.execute(
                f"""
                SELECT id, guest_bot_id, user_id, name, response_text, media_items, buttons_json, enabled, owner_only, created_at, updated_at
                FROM guest_commands
                WHERE {where}
                ORDER BY name ASC
                """,
                params,
            ).fetchall()
        out: list[dict] = []
        for row in rows:
            out.append({
                "id": int(row[0]), "guest_bot_id": int(row[1]), "user_id": int(row[2] or 0),
                "name": str(row[3] or ""), "response_text": str(row[4] or ""),
                "media_items": str(row[5] or "[]"), "buttons_json": str(row[6] or '{"rows":[],"popups":[]}'),
                "enabled": bool(int(row[7] or 0)), "owner_only": bool(int(row[8] or 0)),
                "created_at": int(row[9] or 0), "updated_at": int(row[10] or 0),
            })
        return out
    except Exception as e:
        print(f"[LIST GUEST COMMANDS] Error: {e}")
        return []


def upsert_guest_command(
    guest_bot_id: int, name: str, response_text: str, enabled: bool = True,
    owner_only: bool = False, media_items: str = "[]",
    buttons_json: str = '{"rows":[],"popups":[]}', user_id: int = 0,
) -> bool:
    cmd_name = str(name or "").strip().lower()
    if not cmd_name:
        return False
    ts = int(time.time())
    uid = int(user_id)
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                """
                INSERT INTO guest_commands(guest_bot_id, user_id, name, response_text, media_items, buttons_json, enabled, owner_only, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guest_bot_id, user_id, name) DO UPDATE SET
                    response_text = excluded.response_text,
                    media_items = excluded.media_items,
                    buttons_json = excluded.buttons_json,
                    enabled = excluded.enabled,
                    owner_only = excluded.owner_only,
                    updated_at = excluded.updated_at
                """,
                (int(guest_bot_id), uid, cmd_name, str(response_text or ""), str(media_items or "[]"),
                 str(buttons_json or '{"rows":[],"popups":[]}'), 1 if enabled else 0, 1 if owner_only else 0, ts, ts),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[UPSERT GUEST COMMAND] Error: {e}")
        return False


def delete_guest_command(guest_bot_id: int, name: str, user_id: int = 0) -> bool:
    cmd_name = str(name or "").strip().lower()
    if not cmd_name:
        return False
    uid = int(user_id)
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute("DELETE FROM guest_commands WHERE guest_bot_id = ? AND user_id = ? AND name = ?", (int(guest_bot_id), uid, cmd_name))
            conn.commit()
        return True
    except Exception as e:
        print(f"[DELETE GUEST COMMAND] Error: {e}")
        return False


def set_guest_command_enabled(guest_bot_id: int, name: str, enabled: bool, user_id: int = 0) -> bool:
    cmd_name = str(name or "").strip().lower()
    if not cmd_name:
        return False
    uid = int(user_id)
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute(
                "UPDATE guest_commands SET enabled = ?, updated_at = ? WHERE guest_bot_id = ? AND user_id = ? AND name = ?",
                (1 if enabled else 0, ts, int(guest_bot_id), uid, cmd_name),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[SET GUEST COMMAND ENABLED] Error: {e}")
        return False


# ──────────────────── log_channels helpers ───────────────────────────────────

LOG_CHANNEL_ALL_EVENTS: frozenset[str] = frozenset([
    "ban", "unban", "mute", "unmute", "kick", "warn", "unwarn",
    "chat_closed", "chat_opened", "join", "leave",
    "antiflood", "antispam", "antiraid", "settings",
    "verify", "role", "role_change", "manual_punish",
])


def _lc_default_events() -> dict[str, bool]:
    return {ev: True for ev in LOG_CHANNEL_ALL_EVENTS}


def set_log_channel(chat_id: int, channel_id: int, channel_title: str = "") -> bool:
    ts = int(time.time())
    try:
        with _DB_LOCK:
            conn = _db_connect()
            existing = conn.execute("SELECT events FROM log_channels WHERE chat_id = ?", (int(chat_id),)).fetchone()
            events_json = existing[0] if existing else json.dumps(_lc_default_events(), ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO log_channels(chat_id, channel_id, channel_title, events, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    channel_id    = excluded.channel_id,
                    channel_title = excluded.channel_title,
                    events        = ?,
                    created_at    = excluded.created_at
                """,
                (int(chat_id), int(channel_id), str(channel_title), events_json, ts, events_json),
            )
            conn.commit()
        return True
    except Exception as e:
        print(f"[SET LOG CHANNEL] Error: {e}")
        return False


def remove_log_channel(chat_id: int) -> bool:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            conn.execute("DELETE FROM log_channels WHERE chat_id = ?", (int(chat_id),))
            conn.commit()
        return True
    except Exception as e:
        print(f"[REMOVE LOG CHANNEL] Error: {e}")
        return False


def get_log_channel(chat_id: int) -> dict | None:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute("SELECT channel_id, channel_title, events, created_at FROM log_channels WHERE chat_id = ?", (int(chat_id),)).fetchone()
        if row:
            try:
                events = json.loads(row[2] or "{}")
            except Exception:
                events = {}
            for ev in LOG_CHANNEL_ALL_EVENTS:
                events.setdefault(ev, True)
            return {"channel_id": int(row[0]), "channel_title": str(row[1] or ""), "events": events, "created_at": int(row[3])}
        return None
    except Exception as e:
        print(f"[GET LOG CHANNEL] Error: {e}")
        return None


def set_log_channel_event(chat_id: int, event: str, enabled: bool) -> bool:
    try:
        with _DB_LOCK:
            conn = _db_connect()
            row = conn.execute("SELECT events FROM log_channels WHERE chat_id = ?", (int(chat_id),)).fetchone()
            if not row:
                return False
            events = json.loads(row[0] or "{}") if row else {}
            for ev in LOG_CHANNEL_ALL_EVENTS:
                events.setdefault(ev, True)
            events[event] = bool(enabled)
            conn.execute("UPDATE log_channels SET events = ? WHERE chat_id = ?", (json.dumps(events, ensure_ascii=False), int(chat_id)))
            conn.commit()
        return True
    except Exception as e:
        print(f"[SET LOG CHANNEL EVENT] Error: {e}")
        return False


def send_log_event(chat_id: int, event: str, text: str, forward_from_chat_id: int | None = None, forward_message_id: int | None = None) -> bool:
    lc = get_log_channel(chat_id)
    if not lc or not lc.get("events", {}).get(event, True):
        return False
    channel_id = lc["channel_id"]
    try:
        bot.send_message(channel_id, text, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        print(f"[SEND LOG EVENT] chat={chat_id} event={event}: {e}")
        return False
    if forward_from_chat_id and forward_message_id:
        try:
            bot.forward_message(channel_id, forward_from_chat_id, forward_message_id)
        except Exception as e:
            print(f"[SEND LOG EVENT FORWARD] chat={chat_id} event={event}: {e}")
    return True


_clones_default: dict = {"clones": []}
CLONES: dict = load_json_file(CLONES_FILE, _clones_default)
if not isinstance(CLONES, dict):
    CLONES = _clones_default
CLONES.setdefault("clones", [])

install_telebot_user_fetch_cache_hooks()
