"""인물 45일 쿨다운 / 기사·CTA 중복 차단 (SQLite).

유사문서를 피하는 게 목적이다. 같은 인물을 2주 만에 다시 쓰거나,
CTA 문장이 매번 똑같으면 네이버가 같은 공장에서 찍어낸 글로 본다.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone

import config as C

KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST)


def today_str() -> str:
    return now_kst().strftime("%Y-%m-%d")


def _connect():
    os.makedirs(os.path.dirname(C.STATE_DB), exist_ok=True)
    conn = sqlite3.connect(C.STATE_DB)
    conn.row_factory = sqlite3.Row
    return conn


def init():
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS celebs (
                name      TEXT PRIMARY KEY,
                last_used TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS articles (
                link     TEXT PRIMARY KEY,
                title    TEXT,
                used_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS posts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                celeb      TEXT,
                title      TEXT,
                cta        TEXT,
                created_at TEXT NOT NULL
            );
            """
        )


# ── 인물 쿨다운 ─────────────────────────────────────────────────────────

def celeb_on_cooldown(name: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_used FROM celebs WHERE name = ?", (name,)
        ).fetchone()
    if not row:
        return False
    last = datetime.fromisoformat(row["last_used"])
    return (now_kst() - last).days < C.CELEB_COOLDOWN_DAYS


def available_celebs():
    """쿨다운이 끝난 인물만. 오래 안 쓴 순으로."""
    with _connect() as conn:
        rows = {r["name"]: r["last_used"] for r in conn.execute("SELECT * FROM celebs")}

    out = []
    for name in C.CELEB_POOL:
        if name not in rows:
            out.append((name, ""))  # 한 번도 안 쓴 인물이 최우선
        elif not celeb_on_cooldown(name):
            out.append((name, rows[name]))
    out.sort(key=lambda x: x[1])
    return [n for n, _ in out]


def mark_celeb(name: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO celebs(name, last_used) VALUES(?, ?) "
            "ON CONFLICT(name) DO UPDATE SET last_used = excluded.last_used",
            (name, now_kst().isoformat()),
        )


# ── 기사 중복 ───────────────────────────────────────────────────────────

def article_seen(link: str) -> bool:
    if not C.ARTICLE_DEDUP:
        return False
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM articles WHERE link = ?", (link,)
        ).fetchone() is not None


def mark_articles(items):
    with _connect() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO articles(link, title, used_at) VALUES(?, ?, ?)",
            [(i["link"], i.get("title", ""), now_kst().isoformat()) for i in items],
        )


# ── CTA 로테이션 ────────────────────────────────────────────────────────

def recent_ctas(n=None):
    n = n or C.CTA_COOLDOWN
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cta FROM posts WHERE cta IS NOT NULL "
            "ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return [r["cta"] for r in rows]


def next_cta() -> str:
    """직전 글들과 다른 CTA를 코드가 강제로 고른다."""
    used = set(recent_ctas())
    for cta in C.CTA_POOL:
        if cta not in used:
            return cta
    return C.CTA_POOL[0]  # 풀을 다 돌았으면 처음부터


# ── 발행 기록 ───────────────────────────────────────────────────────────

def record_post(date: str, celeb: str, title: str, cta: str):
    """하루 한 편이 원칙이라 같은 날짜는 덮어쓴다.

    --stage finish 는 형식 위반을 고치며 여러 번 돌린다. 그때마다 행이 쌓이면
    recent_ctas()가 오염돼 CTA 로테이션이 헛돈다(실제로 하루에 4행이 쌓였다).
    """
    with _connect() as conn:
        conn.execute("DELETE FROM posts WHERE date = ?", (date,))
        conn.execute(
            "INSERT INTO posts(date, celeb, title, cta, created_at) VALUES(?,?,?,?,?)",
            (date, celeb, title, cta, now_kst().isoformat()),
        )


def stats():
    with _connect() as conn:
        return {
            "posts": conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"],
            "celebs_used": conn.execute("SELECT COUNT(*) c FROM celebs").fetchone()["c"],
            "articles_seen": conn.execute("SELECT COUNT(*) c FROM articles").fetchone()["c"],
        }


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    init()
    print("DB:", C.STATE_DB)
    print("통계:", stats())
    print("사용 가능 인물 (앞 10명):", available_celebs()[:10])
    print("다음 CTA:", next_cta())
