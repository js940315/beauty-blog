"""소스 수집. 경로는 두 개다.

1) 네이버 검색 API (권장) — developers.naver.com/apps 에서 "검색" 선택. 무료 25,000회/일.
   description에 본문 요약이 실려 있어 팩트시트에 쓸 근거가 두껍다.
   sort=sim 은 정확도순이라 옛날 인기 기사가 섞인다. 최신 위주면 --sort date.

2) Google News RSS (폴백) — 키가 필요 없다.
   ⚠️ description이 제목 복사본이라 사실상 헤드라인만 얻는다(2026-08-04 실측).
      근거가 얇으니 richness가 thin으로 떨어지는 게 정상이고, 그러면 글도 짧게 나간다.
      "못 구하면 뺀다"가 이 파이프라인의 기본 태도다.

   취급 주의(실측):
   - <title> 끝에 " - 매체명"이 붙는다 → 잘라내고 매체명은 <source>에서 따로 얻는다
   - 날짜순이 아니라 관련도순이다 → pubDate로 정렬한 뒤 기간 필터를 반드시 건다
   - UA 없이 요청하면 막힌다
"""

import email.utils
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import config as C
import store

ENDPOINT = "https://openapi.naver.com/v1/search/{target}.json"
GNEWS = "https://news.google.com/rss/search"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
_TAG = re.compile(r"<[^>]+>")
_GN_SUFFIX = re.compile(r"\s+-\s+[^-]+$")


class NaverKeyMissing(RuntimeError):
    pass


def _clean(s: str) -> str:
    return html.unescape(_TAG.sub("", s or "")).strip()


def naver_ready() -> bool:
    return bool(C.NAVER_CLIENT_ID and C.NAVER_CLIENT_SECRET)


def _request(target: str, query: str, sort: str, display: int):
    if not C.NAVER_CLIENT_ID or not C.NAVER_CLIENT_SECRET:
        raise NaverKeyMissing(
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 없습니다. "
            "developers.naver.com/apps 에서 '검색' API 키를 발급받으세요."
        )
    params = urllib.parse.urlencode(
        {"query": query, "display": display, "sort": sort}
    )
    url = ENDPOINT.format(target=target) + "?" + params
    req = urllib.request.Request(
        url,
        headers={
            "X-Naver-Client-Id": C.NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": C.NAVER_CLIENT_SECRET,
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def _collect_naver(celeb: str, sort: str, display: int, seen_links: set):
    items = []
    for suffix in C.QUERY_SUFFIXES:
        query = f"{celeb} {suffix}"
        for target in C.SEARCH_TARGETS:
            try:
                data = _request(target, query, sort, display)
            except NaverKeyMissing:
                raise
            except Exception as e:
                print(f"  [경고] {target}/{query} 수집 실패: {e}")
                continue

            for it in data.get("items", []):
                link = it.get("link", "")
                if not link or link in seen_links:
                    continue
                if store.article_seen(link):
                    continue
                seen_links.add(link)
                items.append({
                    "source": target,
                    "query": query,
                    "title": _clean(it.get("title", "")),
                    "desc": _clean(it.get("description", "")),
                    "link": link,
                    "date": it.get("pubDate") or it.get("postdate") or "",
                })
    return items


# ── Google News RSS (키 불필요 폴백) ────────────────────────────────────

def _gnews(query: str):
    url = GNEWS + "?" + urllib.parse.urlencode(
        {"q": query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "ignore")

    rows = []
    for blob in re.findall(r"<item>(.*?)</item>", raw, re.S):
        t = re.search(r"<title>(.*?)</title>", blob, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", blob, re.S)
        if not (t and d):
            continue
        s = re.search(r"<source[^>]*>(.*?)</source>", blob, re.S)
        lk = re.search(r"<link>(.*?)</link>", blob, re.S)
        try:
            when = email.utils.parsedate_to_datetime(d.group(1).strip())
        except Exception:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        rows.append({
            "title": _GN_SUFFIX.sub("", _clean(t.group(1))).strip(),
            "media": _clean(s.group(1)) if s else "",
            "link": (lk.group(1).strip() if lk else ""),
            "when": when,
        })
    rows.sort(key=lambda r: -r["when"].timestamp())   # 관련도순으로 오므로 반드시 재정렬
    return rows


def _collect_google(celeb: str, seen_links: set):
    cutoff = datetime.now(timezone.utc) - timedelta(days=C.GOOGLE_NEWS_DAYS)
    queries = [f"{celeb} {s}" for s in C.QUERY_SUFFIXES] + list(C.GOOGLE_TOPIC_QUERIES)

    items = []
    for query in queries:
        try:
            rows = _gnews(query)
        except Exception as e:
            print(f"  [경고] google/{query} 수집 실패: {e}")
            continue

        taken = 0
        for r in rows:
            if taken >= C.GOOGLE_NEWS_PER_QUERY:
                break
            if r["when"] < cutoff:
                continue
            link = r["link"]
            if not link or link in seen_links or store.article_seen(link):
                continue
            seen_links.add(link)
            taken += 1
            items.append({
                "source": "google",
                "query": query,
                "title": r["title"],
                # 구글 뉴스는 본문 요약을 주지 않는다. 없는 걸 지어내지 않고 비워둔다.
                "desc": "",
                "media": r["media"],
                "link": link,
                "date": r["when"].isoformat(),
            })
    return items


def collect(celeb: str, sort: str = None, display: int = None, mode: str = None):
    """한 인물에 대한 소스를 모은다. 이미 쓴 링크는 걸러낸다."""
    sort = sort or C.DEFAULT_SORT
    display = display or C.NAVER_DISPLAY
    mode = mode or C.SOURCE_MODE

    seen_links = set()

    if mode == "naver":
        return _collect_naver(celeb, sort, display, seen_links)
    if mode == "google":
        return _collect_google(celeb, seen_links)
    if mode != "auto":
        raise ValueError(f"알 수 없는 수집 경로: {mode}")

    # auto — 네이버를 먼저 시도하되, 한 건도 못 받으면 구글로 넘어간다.
    # 키가 있다고 되는 게 아니다. 검색 API는 스코프가 따로 있어서 앱에 그 권한이
    # 없으면 401 "Scope Status Invalid"가 난다(실측). 키 만료·한도 초과도 마찬가지다.
    # 여기서 안 넘어가면 그날 글이 통째로 안 나온다.
    if naver_ready():
        items = _collect_naver(celeb, sort, display, seen_links)
        if items:
            return items
        print("  [알림] 네이버에서 한 건도 못 받았습니다 (키·스코프·한도를 확인하세요). "
              "Google News RSS로 전환합니다.")
        seen_links.clear()
    else:
        print("  [알림] 네이버 키가 없어 Google News RSS로 수집합니다. "
              "헤드라인만 얻으므로 근거가 얇습니다(글이 짧게 나갑니다).")
    return _collect_google(celeb, seen_links)


def assess_richness(items) -> str:
    """소스가 얇으면 'thin'. 분량 하한을 완화해 창작으로 새는 걸 막는다.

    짧게 내는 게 지어내는 것보다 낫다.
    """
    if len(items) < 6:
        return "thin"

    # 구글 뉴스는 본문 요약을 주지 않는다. 제목만 100개 모여도 근거는 얇다.
    # 제목 수로 richness를 부풀리면 모델이 그 빈칸을 창작으로 메운다.
    if not any((i.get("desc") or "").strip() for i in items):
        return "thin"

    body = " ".join(i["title"] + " " + i.get("desc", "") for i in items)
    # 구체적 근거(숫자·아이템·루틴명)가 얼마나 있는지
    hits = len(re.findall(r"\d+\s*(?:년|개월|주|일|만원|억|세|분)", body))
    hits += sum(body.count(w) for w in ("피부", "메이크업", "패션", "코디", "루틴", "관리"))
    return "thin" if hits < 12 else "normal"


def summarize(items, limit=25):
    """프롬프트에 넣을 소스 묶음. 너무 길면 자른다."""
    out = []
    for i, it in enumerate(items[:limit], 1):
        head = it.get("media") or it["source"]
        line = f"[{i}] ({head}) {it['title']}"
        desc = (it.get("desc") or "").strip()
        if desc:
            line += f"\n    {desc}"
        line += f"\n    {it['link']}"
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    store.init()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    mode = next((a[2:] for a in sys.argv[1:] if a.startswith("--")), None)
    name = args[0] if args else C.CELEB_POOL[0]
    try:
        got = collect(name, mode=mode)
    except NaverKeyMissing as e:
        print("[중단]", e)
        sys.exit(2)
    print(f"{name}: {len(got)}건 / richness={assess_richness(got)}")
    for it in got[:5]:
        print(" -", (it.get("media") or it["source"]), "|", it["title"][:56])
