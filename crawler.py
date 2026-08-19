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


def _collect_naver(celeb: str, sort: str, display: int, seen_links: set,
                   extra_queries=()):
    items = []
    for query in [f"{celeb} {s}" for s in C.QUERY_SUFFIXES] + list(extra_queries):
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


def _collect_google(celeb: str, seen_links: set, extra_queries=()):
    cutoff = datetime.now(timezone.utc) - timedelta(days=C.GOOGLE_NEWS_DAYS)
    queries = ([f"{celeb} {s}" for s in C.QUERY_SUFFIXES]
               + list(extra_queries)
               + list(C.GOOGLE_TOPIC_QUERIES))

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


def drop_namesakes(celeb: str, items):
    """동명이인 기사를 걷어낸다 (2026-08-13 사용자 확정).

    검색은 이름으로만 하니 같은 이름의 다른 사람 기사가 그대로 섞여 들어온다.
    2026-08-13 실측 사고: '김고은' 소스에 배우 김고은, '솔로지옥5' 출연자
    김고은, 인플루언서 김고은이 함께 담겼고, 본문 한 편이 두 사람 얘기를
    한 사람인 것처럼 썼다.

    여기서 안 자르면 팩트시트가 오염되고, 오염된 팩트시트는 제목·본문까지
    그대로 간다. 소스 단계가 유일하게 싼 방어선이다.
    """
    markers = C.NAMESAKE_DROP.get(celeb)
    if not markers:
        return items
    kept, dropped = [], []
    for it in items:
        blob = (it.get("title", "") + " " + (it.get("desc") or ""))
        hit = next((m for m in markers if m in blob), None)
        if hit:
            dropped.append((hit, it.get("title", "")))
        else:
            kept.append(it)
    if dropped:
        print(f"  [동명이인] {len(dropped)}건 제외 — {celeb}")
        for hit, title in dropped[:5]:
            print(f"      · ({hit}) {title[:44]}")
    return kept


# 제목 앞 이 글자 수 안에 이름이 있으면 그 기사의 주인공으로 본다.
SPOTLIGHT_HEAD = 12


def _spotlight(celeb: str, items):
    """주인공이 **주인공인** 기사를 앞으로, 곁다리로 스친 기사를 뒤로 보낸다.

    2026-08-19 실측 사고: "재산" 쿼리를 넣었더니 이영애 편에 이런 게 들어왔다.
        "'9440억 재산 분할' 노소영, 깜짝 근황…최수종 공연 인증샷→이영애도 방문"
    이영애 재산 기사가 아니라 **노소영 재산 기사**다. 이런 걸 근거로 삼으면
    남의 수치가 우리 인물의 조건으로 둔갑한다. 동명이인 필터는 이름이 같은
    사람만 걸러서 이건 못 잡는다.

    지우지는 않는다 — 제목에 이름이 없어도 본문이 그 인물 얘기인 경우가 있다.
    순서만 내린다. summarize() 가 앞에서부터 자르므로 이것으로 충분하다.
    """
    name = (celeb or "").replace(" ", "")
    if not name:
        return items

    def tier(it):
        title = (it.get("title") or "").replace(" ", "")
        pos = title.find(name)
        if pos < 0:
            return 2          # 제목에 없음 — 본문 얘기일 수도 있으니 버리진 않는다
        # 이름이 제목 앞쪽에 있으면 그 기사의 주인공이다.
        # "노소영 9440억 재산분할…이영애도 방문" 에서 이영애는 18번째다.
        return 0 if pos <= SPOTLIGHT_HEAD else 1

    return sorted(items, key=tier)      # 안정 정렬이라 tier 안에서는 인기순 유지


def _finish(celeb: str, items):
    """동명이인·곁다리 기사를 정리한 뒤 좋아요 순으로 정렬한다.

    정렬이 여기 붙는 이유: summarize() 가 앞에서부터만 프롬프트에 넣으므로
    순서가 곧 선별이다. 실패하면 원래 순서가 그대로 나온다(popularity.rank).
    """
    import popularity
    ranked = popularity.rank(drop_namesakes(celeb, items))
    return _spotlight(celeb, ranked)


def collect(celeb: str, sort: str = None, display: int = None, mode: str = None,
            extra_queries=()):
    """한 인물에 대한 소스를 모은다. 이미 쓴 링크·동명이인 기사는 걸러낸다.

    반환 순서는 좋아요 수 기준이다(뉴스/블로그 자리 배치는 유지).

    extra_queries: 기본 쿼리(QUERY_SUFFIXES) 뒤에 덧붙일 완성된 검색어들.
      bench 슬롯이 벤치마크에서 뽑은 앵글을 여기로 넣는다 — "한소희 데일리룩"
      처럼. 인물명만 검색하면 옛 광고성 기사가 위로 오는데, 지금 반응 있는
      앵글을 붙이면 그 자리의 기사가 들어온다 (2026-08-17 사용자 확정).
    """
    sort = sort or C.DEFAULT_SORT
    display = display or C.NAVER_DISPLAY
    mode = mode or C.SOURCE_MODE
    extra_queries = list(extra_queries or ())

    seen_links = set()

    if mode == "naver":
        return _finish(celeb, _collect_naver(celeb, sort, display, seen_links,
                                             extra_queries))
    if mode == "google":
        return _finish(celeb, _collect_google(celeb, seen_links, extra_queries))
    if mode != "auto":
        raise ValueError(f"알 수 없는 수집 경로: {mode}")

    # auto — 네이버를 먼저 시도하되, 한 건도 못 받으면 구글로 넘어간다.
    # 키가 있다고 되는 게 아니다. 검색 API는 스코프가 따로 있어서 앱에 그 권한이
    # 없으면 401 "Scope Status Invalid"가 난다(실측). 키 만료·한도 초과도 마찬가지다.
    # 여기서 안 넘어가면 그날 글이 통째로 안 나온다.
    if naver_ready():
        items = _collect_naver(celeb, sort, display, seen_links, extra_queries)
        if items:
            return _finish(celeb, items)
        print("  [알림] 네이버에서 한 건도 못 받았습니다 (키·스코프·한도를 확인하세요). "
              "Google News RSS로 전환합니다.")
        seen_links.clear()
    else:
        print("  [알림] 네이버 키가 없어 Google News RSS로 수집합니다. "
              "헤드라인만 얻으므로 근거가 얇습니다(글이 짧게 나갑니다).")
    return _finish(celeb, _collect_google(celeb, seen_links, extra_queries))


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
    # 2026-08-19: 조건·사건 축을 추가하면서 이 신호도 같이 넓혔다. 안 그러면
    # 재산·가족 기사만 잔뜩 모여도 "근거 얇음"으로 판정돼 분량 하한이 풀린다.
    hits += sum(body.count(w) for w in
                ("재산", "자산", "남편", "아내", "결혼", "이혼", "열애",
                 "학력", "졸업", "집안", "수상", "데뷔"))
    return "thin" if hits < 12 else "normal"


def _balance_by_query(items, limit, floor=1):
    """쿼리마다 최소 floor 건을 보장하고, 남는 자리는 원래 순서(좋아요 순)로 채운다.

    두 가지 요구가 부딪히는 자리다.
      1) popularity.rank() 가 좋아요 순으로 정렬한다(2026-08-17 사용자 확정).
         summarize() 가 앞 25건만 쓰므로 순서가 곧 선별이기 때문이다.
      2) 그런데 그 정렬만 쓰면 **앞 쿼리가 25칸을 독식한다.** 실측:
         옛 방식은 8개 쿼리 중 4종만 뽑혔고, 8/13 에 추가한 작품 쿼리
         (명장면·명대사 재조명·상대역 케미·예능 출연)는 5일간 **0칸**이었다.
         관계형 후킹의 재료가 거기서 나오는데 프롬프트에 도달한 적이 없다.

    순수 라운드로빈으로 바꿨더니 이번엔 좋아요 평균이 111 -> 81 로 떨어졌다.
    그래서 절충한다 — 쿼리별 1등만 자리를 보장하고(재료 확보), 나머지 17칸은
    좋아요 순 그대로 간다(질 확보). 버킷 내부는 입력 순서를 유지하므로
    보장분도 그 쿼리의 최고 인기 항목이다.
    """
    buckets = {}
    order = []
    for it in items:
        q = it.get("query") or "_"
        if q not in buckets:
            buckets[q] = []
            order.append(q)
        buckets[q].append(it)

    picked, seen = [], set()
    for _ in range(max(0, floor)):                 # 쿼리별 보장분
        for q in order:
            for it in buckets[q]:
                if id(it) not in seen:
                    picked.append(it)
                    seen.add(id(it))
                    break
            if len(picked) >= limit:
                return picked
    for it in items:                               # 남는 자리는 원래(좋아요) 순서
        if len(picked) >= limit:
            break
        if id(it) not in seen:
            picked.append(it)
            seen.add(id(it))
    return picked


def summarize(items, limit=None):
    """프롬프트에 넣을 소스 묶음. 너무 길면 자른다.

    limit 기본값은 config.SOURCE_ITEMS_MAX 다. 쿼리 수가 늘면 여기도 같이
    늘려야 한다 — _balance_by_query 의 쿼리별 보장분이 자리를 먹기 때문이다.
    """
    limit = limit or getattr(C, "SOURCE_ITEMS_MAX", 25)
    out = []
    for i, it in enumerate(_balance_by_query(items, limit), 1):
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
