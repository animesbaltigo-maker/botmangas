import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from config import DATA_DIR

DB_PATH = DATA_DIR / "manga_metrics.sqlite3"


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_str() -> str:
    return _utc_now_dt().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def _get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=30000;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_metrics_db():
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metric_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                user_id TEXT,
                username TEXT,
                title_id TEXT,
                title_name TEXT,
                chapter_id TEXT,
                chapter_number TEXT,
                query_text TEXT,
                result_count INTEGER,
                extra TEXT,
                created_at TEXT NOT NULL
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_events_type
            ON metric_events(event_type)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_events_user
            ON metric_events(user_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_events_created_at
            ON metric_events(created_at)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_events_title
            ON metric_events(title_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_metric_events_title_name
            ON metric_events(title_name)
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reading_history (
                user_id TEXT NOT NULL,
                title_id TEXT NOT NULL,
                title_name TEXT,
                chapter_id TEXT NOT NULL,
                chapter_number TEXT,
                chapter_url TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, title_id, chapter_id)
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reading_history_user
            ON reading_history(user_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reading_history_title
            ON reading_history(title_id)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reading_history_title_name
            ON reading_history(title_name)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_reading_history_updated_at
            ON reading_history(updated_at)
            """
        )


def log_event(
    event_type: str,
    user_id: int | str | None = None,
    username: str | None = None,
    title_id: str | None = None,
    title_name: str | None = None,
    chapter_id: str | None = None,
    chapter_number: str | int | None = None,
    query_text: str | None = None,
    result_count: int | None = None,
    extra: str | None = None,
    anime_id: str | None = None,
    anime_title: str | None = None,
    episode: str | int | None = None,
):
    title_id = (title_id or anime_id or "").strip()
    title_name = (title_name or anime_title or "").strip()
    chapter_number_text = (
        str(chapter_number).strip()
        if chapter_number is not None
        else (str(episode).strip() if episode is not None else "")
    )

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO metric_events (
                event_type,
                user_id,
                username,
                title_id,
                title_name,
                chapter_id,
                chapter_number,
                query_text,
                result_count,
                extra,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(event_type or "").strip(),
                str(user_id) if user_id is not None else None,
                (username or "").strip(),
                title_id,
                title_name,
                (chapter_id or "").strip(),
                chapter_number_text,
                (query_text or "").strip(),
                result_count,
                (extra or "").strip(),
                _utc_now_str(),
            ),
        )


def mark_user_seen(user_id: int | str, username: str | None = None):
    user_id = str(user_id).strip()
    username = (username or "").strip()

    with _get_conn() as conn:
        exists = conn.execute(
            """
            SELECT 1
            FROM metric_events
            WHERE event_type = 'new_user' AND user_id = ?
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if not exists:
            conn.execute(
                """
                INSERT INTO metric_events (
                    event_type, user_id, username, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                ("new_user", user_id, username, _utc_now_str()),
            )

        conn.execute(
            """
            INSERT INTO metric_events (
                event_type, user_id, username, created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            ("active_user", user_id, username, _utc_now_str()),
        )


def mark_chapter_read(
    user_id: int | str,
    title_id: str,
    chapter_id: str,
    chapter_number: str | int,
    title_name: str | None = None,
    chapter_url: str | None = None,
    username: str | None = None,
):
    user_id = str(user_id).strip()
    title_id = str(title_id).strip()
    chapter_id = str(chapter_id).strip()
    chapter_number_text = str(chapter_number).strip()
    title_name = (title_name or "").strip()
    chapter_url = (chapter_url or "").strip()
    username = (username or "").strip()

    if not title_id or not chapter_id:
        raise ValueError("title_id e chapter_id sao obrigatorios para leitura.")

    now = _utc_now_str()

    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO reading_history (
                user_id,
                title_id,
                title_name,
                chapter_id,
                chapter_number,
                chapter_url,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, title_id, chapter_id)
            DO UPDATE SET
                title_name = excluded.title_name,
                chapter_number = excluded.chapter_number,
                chapter_url = excluded.chapter_url,
                updated_at = excluded.updated_at
            """,
            (
                user_id,
                title_id,
                title_name,
                chapter_id,
                chapter_number_text,
                chapter_url,
                now,
                now,
            ),
        )

    log_event(
        event_type="chapter_read",
        user_id=user_id,
        username=username,
        title_id=title_id,
        title_name=title_name,
        chapter_id=chapter_id,
        chapter_number=chapter_number_text,
    )


def unmark_chapter_read(
    user_id: int | str,
    title_id: str,
    chapter_id: str,
    title_name: str | None = None,
    chapter_number: str | int | None = None,
    username: str | None = None,
):
    user_id = str(user_id).strip()
    title_id = str(title_id).strip()
    chapter_id = str(chapter_id).strip()

    with _get_conn() as conn:
        conn.execute(
            """
            DELETE FROM reading_history
            WHERE user_id = ? AND title_id = ? AND chapter_id = ?
            """,
            (user_id, title_id, chapter_id),
        )

    log_event(
        event_type="chapter_unread",
        user_id=user_id,
        username=username or "",
        title_id=title_id,
        title_name=title_name or "",
        chapter_id=chapter_id,
        chapter_number=chapter_number,
    )


def is_chapter_read(
    user_id: int | str,
    title_id: str,
    chapter_id: str | None = None,
    chapter_number: str | int | None = None,
) -> bool:
    user_id = str(user_id).strip()
    title_id = str(title_id).strip()

    sql = """
        SELECT 1
        FROM reading_history
        WHERE user_id = ? AND title_id = ?
    """
    params: list[Any] = [user_id, title_id]

    if chapter_id:
        sql += " AND chapter_id = ?"
        params.append(str(chapter_id).strip())
    elif chapter_number is not None:
        sql += " AND chapter_number = ?"
        params.append(str(chapter_number).strip())
    else:
        return False

    sql += " LIMIT 1"

    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return row is not None


def get_read_chapter_ids(user_id: int | str, title_id: str) -> list[str]:
    user_id = str(user_id).strip()
    title_id = str(title_id).strip()

    try:
        with _get_conn() as conn:
            rows = conn.execute(
                """
                SELECT chapter_id
                FROM reading_history
                WHERE user_id = ? AND title_id = ?
                ORDER BY updated_at DESC
                """,
                (user_id, title_id),
            ).fetchall()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        init_metrics_db()
        return []

    return [str(row["chapter_id"]) for row in rows if row["chapter_id"]]


def get_last_read_entry(user_id: int | str, title_id: str) -> dict[str, Any] | None:
    user_id = str(user_id).strip()
    title_id = str(title_id).strip()

    try:
        with _get_conn() as conn:
            row = conn.execute(
                """
                SELECT title_id, title_name, chapter_id, chapter_number, chapter_url, updated_at
                FROM reading_history
                WHERE user_id = ? AND title_id = ?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, title_id),
            ).fetchone()
    except sqlite3.OperationalError as error:
        if "no such table" not in str(error).lower():
            raise
        init_metrics_db()
        return None

    if not row:
        return None

    return {
        "title_id": row["title_id"],
        "title_name": row["title_name"] or "",
        "chapter_id": row["chapter_id"],
        "chapter_number": row["chapter_number"] or "",
        "chapter_url": row["chapter_url"] or "",
        "updated_at": row["updated_at"],
    }


def get_recently_read(user_id: int | str, limit: int = 10) -> list[dict[str, Any]]:
    user_id = str(user_id).strip()
    limit = max(1, int(limit))

    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT title_id, title_name, chapter_id, chapter_number, chapter_url, updated_at
            FROM reading_history
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    return [
        {
            "title_id": row["title_id"],
            "title_name": row["title_name"] or "",
            "chapter_id": row["chapter_id"],
            "chapter_number": row["chapter_number"] or "",
            "chapter_url": row["chapter_url"] or "",
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def get_reading_summary(user_id: int | str, recent_limit: int = 5) -> dict[str, Any]:
    user_id = str(user_id).strip()
    recent_limit = max(1, int(recent_limit))

    with _get_conn() as conn:
        stats = conn.execute(
            """
            SELECT
                COUNT(*) AS chapter_count,
                COUNT(DISTINCT title_id) AS title_count
            FROM reading_history
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        rows = conn.execute(
            """
            SELECT title_id, title_name, chapter_id, chapter_number, chapter_url, updated_at
            FROM reading_history
            WHERE user_id = ?
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (user_id, recent_limit),
        ).fetchall()

    return {
        "title_count": int(stats["title_count"] if stats else 0),
        "chapter_count": int(stats["chapter_count"] if stats else 0),
        "recent_reads": [
            {
                "title_id": row["title_id"],
                "title_name": row["title_name"] or "",
                "chapter_id": row["chapter_id"],
                "chapter_number": row["chapter_number"] or "",
                "chapter_url": row["chapter_url"] or "",
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
    }


def get_search_seed_titles(limit: int = 300) -> list[dict[str, Any]]:
    limit = max(1, int(limit))
    catalog: dict[str, dict[str, Any]] = {}

    def _merge_row(title_id: str, title_name: str, weight: int, last_seen: str) -> None:
        title_id = str(title_id or "").strip()
        title_name = str(title_name or "").strip()
        if not title_id or not title_name:
            return

        current = catalog.get(title_id)
        if current is None:
            catalog[title_id] = {
                "title_id": title_id,
                "title": title_name,
                "weight": int(weight),
                "last_seen": last_seen or "",
            }
            return

        current["weight"] = int(current.get("weight") or 0) + int(weight)
        if len(title_name) > len(str(current.get("title") or "")):
            current["title"] = title_name
        if (last_seen or "") > str(current.get("last_seen") or ""):
            current["last_seen"] = last_seen or ""

    with _get_conn() as conn:
        history_rows = conn.execute(
            """
            SELECT title_id, title_name, COUNT(*) AS hits, MAX(updated_at) AS last_seen
            FROM reading_history
            WHERE COALESCE(title_id, '') <> '' AND COALESCE(title_name, '') <> ''
            GROUP BY title_id, title_name
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit * 2,),
        ).fetchall()

        event_rows = conn.execute(
            """
            SELECT title_id, title_name, COUNT(*) AS hits, MAX(created_at) AS last_seen
            FROM metric_events
            WHERE COALESCE(title_id, '') <> '' AND COALESCE(title_name, '') <> ''
            GROUP BY title_id, title_name
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit * 3,),
        ).fetchall()

    for row in history_rows:
        _merge_row(row["title_id"], row["title_name"], int(row["hits"] or 0) + 4, row["last_seen"] or "")

    for row in event_rows:
        _merge_row(row["title_id"], row["title_name"], int(row["hits"] or 0), row["last_seen"] or "")

    ranked = sorted(
        catalog.values(),
        key=lambda item: (int(item.get("weight") or 0), str(item.get("last_seen") or "")),
        reverse=True,
    )

    return [
        {
            "title_id": item["title_id"],
            "title": item["title"],
        }
        for item in ranked[:limit]
    ]


def _range_start(period: str | None) -> str | None:
    period = (period or "total").lower().strip()
    now = _utc_now_dt()

    if period == "total":
        return None
    if period == "30d":
        return (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    if period == "7d":
        return (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    if period == "hoje":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.strftime("%Y-%m-%d %H:%M:%S")
    return None


def _top_rows(event_type: str, field_expr: str, limit: int = 10, period: str = "total"):
    since = _range_start(period)

    sql = f"""
        SELECT
            {field_expr} AS label,
            COUNT(*) AS total
        FROM metric_events
        WHERE event_type = ?
          AND COALESCE({field_expr}, '') <> ''
    """
    params: list[Any] = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    sql += f"""
        GROUP BY {field_expr}
        ORDER BY total DESC, {field_expr} ASC
        LIMIT ?
    """
    params.append(limit)

    with _get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def _count(event_type: str, period: str = "total") -> int:
    since = _range_start(period)
    sql = """
        SELECT COUNT(*) AS total
        FROM metric_events
        WHERE event_type = ?
    """
    params: list[Any] = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["total"] if row else 0)


def _count_distinct_users(event_type: str, period: str = "total") -> int:
    since = _range_start(period)
    sql = """
        SELECT COUNT(DISTINCT user_id) AS total
        FROM metric_events
        WHERE event_type = ?
          AND COALESCE(user_id, '') <> ''
    """
    params: list[Any] = [event_type]

    if since:
        sql += " AND created_at >= ?"
        params.append(since)

    with _get_conn() as conn:
        row = conn.execute(sql, params).fetchone()
    return int(row["total"] if row else 0)


def _top_read_titles(limit: int = 10):
    with _get_conn() as conn:
        return conn.execute(
            """
            SELECT
                COALESCE(NULLIF(title_name, ''), title_id) AS label,
                COUNT(*) AS total
            FROM reading_history
            GROUP BY title_id, title_name
            ORDER BY total DESC, label ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def clear_metrics():
    with _get_conn() as conn:
        conn.execute("DELETE FROM metric_events")


def clear_reading_history():
    with _get_conn() as conn:
        conn.execute("DELETE FROM reading_history")


def clear_all_metrics_data():
    with _get_conn() as conn:
        conn.execute("DELETE FROM metric_events")
        conn.execute("DELETE FROM reading_history")


def get_metrics_report(limit: int = 7, period: str = "total") -> dict:
    return {
        "period": period,
        "top_searches": _top_rows("search", "query_text", limit, period),
        "top_opened_titles": _top_rows("title_open", "title_name", limit, period),
        "top_opened_chapters": _top_rows(
            "chapter_open",
            "title_name || ' - CAP ' || chapter_number",
            limit,
            period,
        ),
        "top_read_titles": _top_read_titles(limit),
        "searches_without_result": _count("search_no_result", period),
        "new_users": _count_distinct_users("new_user", period),
        "active_users": _count_distinct_users("active_user", period),
        "read_marks_total": _count("chapter_read", period),
    }


def mark_episode_watched(
    user_id: int | str,
    anime_id: str,
    episode: int | str,
    anime_title: str | None = None,
    username: str | None = None,
):
    chapter_id = f"{anime_id}:{episode}"
    mark_chapter_read(
        user_id=user_id,
        title_id=anime_id,
        chapter_id=chapter_id,
        chapter_number=episode,
        title_name=anime_title,
        username=username,
    )


def unmark_episode_watched(
    user_id: int | str,
    anime_id: str,
    episode: int | str,
    anime_title: str | None = None,
    username: str | None = None,
):
    chapter_id = f"{anime_id}:{episode}"
    unmark_chapter_read(
        user_id=user_id,
        title_id=anime_id,
        chapter_id=chapter_id,
        title_name=anime_title,
        chapter_number=episode,
        username=username,
    )


def is_episode_watched(user_id: int | str, anime_id: str, episode: int | str) -> bool:
    return is_chapter_read(
        user_id=user_id,
        title_id=anime_id,
        chapter_id=f"{anime_id}:{episode}",
    )


def get_recently_watched(user_id: int | str, limit: int = 10) -> list[dict[str, Any]]:
    return get_recently_read(user_id, limit=limit)
