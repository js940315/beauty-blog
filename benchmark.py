# -*- coding: utf-8 -*-
"""벤치마킹 — 경쟁 블로그에서 지금 먹히는 소재를 좋아요 순으로 본다.

네이버 검색 API 없이 돌아간다. 2026-07-31 로 개발자센터 검색 API 신규 신청이
종료돼(기존 키 보유자만 2027-06-30 까지) 새로 키를 받을 수 없다. 대신
로그인·키가 전혀 필요 없는 두 경로를 쓴다.

    1) RSS      rss.blog.naver.com/{blogId}.xml    → 최근 50편의 제목·태그·날짜
    2) 좋아요    popularity.like_count()            → 편당 실제 좋아요 수

## 본문을 가져오지 않는다 (중요)

RSS description 에 본문 앞부분 550~600자가 실려 있지만 **읽지 않는다.**
남의 블로그 본문을 근거로 넣으면 모델이 그걸 바꿔 쓰게 되고, 그게 네이버가
판정하는 유사문서다. `dedup.py` 는 우리 과거 글만 검사하므로 이 경로는
막지 못한다. 그래서 여기서는 **제목·좋아요·태그·카테고리만** 수집한다.

얻는 것: "지금 어떤 인물·앵글이 반응을 얻고 있나"
얻지 않는 것: 베껴 쓸 수 있는 문장

## 목록은 실측으로 정했다 (2026-08-17)

후보 22개의 RSS 최근 5편씩 좋아요를 재서 셀럽패션·스타패션 계열만 남겼다.
좋아요 중앙값: 제인 61 / May 29(최고 249) / 오늘도예쁘게 22 / 예뚱이 19 /
김미 14 / 시아 11. 카테고리가 푸드·요리인 곳(kwang5166 중앙 218,
howmany70 23)은 반응이 높아도 패션 소재가 아니라 제외했다 — 건강비버용이다.
"""

import html
import json
import re
import time
import urllib.error
import urllib.request

import config as C
import popularity

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
RSS = "https://rss.blog.naver.com/{blog_id}.xml"
_TAG = re.compile(r"<[^>]+>")


def _field(name: str, blob: str) -> str:
    """RSS 필드 하나. CDATA 로 싸여 있어 그냥 <link>[^<]* 로는 못 뽑는다."""
    m = re.search(rf"<{name}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{name}>", blob, re.S)
    if not m:
        return ""
    return html.unescape(_TAG.sub("", m.group(1))).strip()


def posts(blog_id: str, limit: int = None):
    """블로그 한 곳의 최근 글 목록. description(본문)은 의도적으로 버린다."""
    limit = limit or C.BENCHMARK_PER_BLOG
    req = urllib.request.Request(RSS.format(blog_id=blog_id),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=C.BENCHMARK_TIMEOUT_S) as r:
        raw = r.read().decode("utf-8", "ignore")

    out = []
    for blob in re.findall(r"<item>(.*?)</item>", raw, re.S):
        link = _field("link", blob)
        ref = popularity.blog_ref(link)
        if not ref:
            continue                      # 채널 자체의 <link> 등은 건너뛴다
        out.append({
            "blog_id": ref[0],
            "post_id": ref[1],
            "title": _field("title", blob),
            "link": link.split("?")[0],
            "date": _field("pubDate", blob),
            "category": _field("category", blob),
            "tags": [t.strip() for t in _field("tag", blob).split(",") if t.strip()],
            "likes": None,
        })
        if len(out) >= limit:
            break
    return out


def collect(blogs=None, budget: int = None, verbose: bool = True):
    """벤치마크 블로그들의 최근 글 + 좋아요. 좋아요 내림차순으로 돌려준다.

    한 블로그가 죽어도 나머지는 계속 모은다. 좋아요 조회는 포스트당 1요청이라
    budget 으로 총량을 묶는다(배치 API 가 없다).
    """
    blogs = blogs or C.BENCHMARK_BLOGS
    budget = budget if budget is not None else C.BENCHMARK_LIKE_BUDGET

    rows, dead = [], []
    for blog_id in blogs:
        try:
            rows.extend(posts(blog_id))
        except (urllib.error.URLError, OSError, ValueError) as e:
            dead.append(f"{blog_id}({type(e).__name__})")

    # 최신순으로 정렬한 뒤 예산만큼만 좋아요를 조회한다. 오래된 글까지
    # 다 재봐야 얻을 게 없다 — 지금 먹히는 소재를 보려는 것이다.
    rows.sort(key=lambda r: r["date"], reverse=True)

    looked = failed = 0
    for r in rows:
        if looked >= budget:
            break
        try:
            r["likes"] = popularity.like_count(r["blog_id"], r["post_id"])
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            failed += 1
        looked += 1
        if C.BENCHMARK_SLEEP_MS:
            time.sleep(C.BENCHMARK_SLEEP_MS / 1000.0)

    scored = [r for r in rows if r["likes"] is not None]
    scored.sort(key=lambda r: -r["likes"])

    if verbose:
        print(f"  [벤치마킹] 블로그 {len(blogs) - len(dead)}곳에서 {len(rows)}편 수집, "
              f"{looked}편 좋아요 조회 (유효 {len(scored)}, 실패 {failed})")
        if looked < len(rows):
            print(f"  [벤치마킹] 예산({budget}) 때문에 {len(rows) - looked}편은 조회하지 않음")
        if dead:
            print(f"  [경고] RSS 실패: {', '.join(dead)}")
    return scored


def for_celeb(celeb: str, rows=None):
    """특정 인물이 등장하는 벤치마크 글만. 제목·태그만 본다(본문은 없다)."""
    rows = rows if rows is not None else collect(verbose=False)
    key = celeb.replace(" ", "")
    return [r for r in rows
            if key in r["title"].replace(" ", "")
            or any(key in t.replace(" ", "") for t in r["tags"])]


def hot_celebs(rows=None, pool=None, limit: int = None):
    """벤치마크 상위 글에 등장하는 **우리 풀 인물** — 좋아요 합계 순.

    2026-08-17 사용자 확정. 하루 10편 중 5편은 소재를 여기서 고른다.
    기존 방식(CELEB_POOL 순서대로 돌리기)은 "누가 지금 반응을 얻고 있나"를
    전혀 안 본다. 풀 순서는 그냥 우리가 적어둔 순서다.

    제목·태그에서만 찾는다 — 본문은 애초에 수집하지 않는다(모듈 상단 참고).

    ⚠️ 부분 문자열로 찾는다. 태그가 "김지원패션" 처럼 이름에 붙어 오기 때문에
       어쩔 수 없다. 두 글자 예명은 엉뚱한 단어에 걸릴 수 있으니(풀에서
       '지수·슬기' 류를 이미 빼둔 이유와 같은 문제) 결과는 추천 목록이고
       확정은 사람이 --auto 로 한다.
    """
    rows = rows if rows is not None else collect(verbose=False)
    pool = pool if pool is not None else C.CELEB_POOL

    hit = {}
    for r in rows:
        blob = (r["title"] + " " + " ".join(r["tags"])).replace(" ", "")
        for name in pool:
            if name.replace(" ", "") not in blob:
                continue
            e = hit.setdefault(name, {"celeb": name, "likes": 0, "posts": []})
            e["likes"] += r["likes"] or 0
            e["posts"].append(r)
    out = sorted(hit.values(), key=lambda e: (-e["likes"], e["celeb"]))
    return out[:limit] if limit else out


def angle_words(rows=None, limit: int = None):
    """지금 좋아요를 받는 앵글 단어 — [(단어, 좋아요합, 편수)] 내림차순.

    이 단어를 크롤러 쿼리에 붙여 소재 수집 자체를 그쪽으로 끌어온다.
    ("한소희" 만 검색하면 광고성 옛 기사가 오지만 "한소희 데일리룩" 은
     지금 사람들이 보고 있는 자리의 기사를 준다)

    어휘는 config.BENCHMARK_ANGLE_WORDS 로 고정하고 **순위만 실측으로** 낸다.
    태그를 그대로 쓰면 "김민희패션" 같은 인물명 태그와 "더로우알마바게트백"
    같은 브랜드 태그가 상위에 올라 쿼리로 못 쓴다 (2026-08-17 실측).
    """
    rows = rows if rows is not None else collect(verbose=False)
    limit = limit if limit is not None else C.BENCHMARK_ANGLE_MAX

    agg = {}
    for r in rows:
        blob = (r["title"] + " " + " ".join(r["tags"])).replace(" ", "")
        for w in C.BENCHMARK_ANGLE_WORDS:
            if w.replace(" ", "") in blob:
                lk, n = agg.get(w, (0, 0))
                agg[w] = (lk + (r["likes"] or 0), n + 1)
    out = sorted(((w, lk, n) for w, (lk, n) in agg.items()),
                 key=lambda x: (-x[1], x[0]))
    return out[:limit] if limit else out


def summarize(rows, limit: int = 15) -> str:
    """프롬프트에 넣을 형태. 제목·좋아요·태그만 — 베낄 본문이 없다."""
    out = []
    for i, r in enumerate(rows[:limit], 1):
        line = f"[{i}] ♥{r['likes']} ({r['category'] or '-'}) {r['title']}"
        if r["tags"]:
            line += f"\n    태그: {', '.join(r['tags'][:8])}"
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]

    rows = collect()
    if args:
        hit = for_celeb(args[0], rows)
        print(f"\n── '{args[0]}' 등장 {len(hit)}편 ──")
        print(summarize(hit) if hit else "  (없음 — 최근 글에 안 나온다)")
    else:
        print(f"\n── 지금 먹히는 소재 상위 15 ──")
        print(summarize(rows))
        cats = {}
        for r in rows[:40]:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        print("\n── 상위 40편의 카테고리 분포 ──")
        for c, n in sorted(cats.items(), key=lambda x: -x[1])[:8]:
            print(f"  {n:>3}편  {c or '-'}")

        print("\n── 지금 반응 있는 앵글 (크롤러 쿼리에 붙는다) ──")
        for w, lk, n in angle_words(rows, limit=10):
            print(f"  ♥{lk:>4}  {n:>2}편  {w}")

        print("\n── 우리 풀 인물 중 지금 등장하는 사람 ──")
        hot = hot_celebs(rows, limit=12)
        if not hot:
            print("  (없음 — 벤치마크 상위에 우리 풀 인물이 안 보인다)")
        for e in hot:
            print(f"  ♥{e['likes']:>4}  {len(e['posts']):>2}편  {e['celeb']}")
        print("\n  bench 슬롯 시작: python pipeline.py --auto <인물명>")
