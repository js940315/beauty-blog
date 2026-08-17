# -*- coding: utf-8 -*-
"""벤치마킹 — 소재를 좋아요 수 순으로 정렬한다.

문제: crawler.py 가 모아오는 순서는 인기와 아무 상관이 없다.
쿼리 순서 × 타겟 순서(news, blog)로 쌓이는데, summarize() 가 앞에서
25건만 잘라 프롬프트에 넣는다. 즉 **아무 근거 없는 순서가 곧 선별**이었다.

그래서 네이버 블로그 소재에 실제 좋아요 수를 붙여 정렬한다.
반응이 많은 글은 후킹·구성이 검증된 글이라, 그게 팩트시트 위로 올라오면
벤치마킹 재료의 질이 올라간다.

## 좋아요 수를 어디서 얻나

검색 API 응답에는 좋아요·댓글 수가 없다. 별도 리액션 API 를 쓴다.

    GET apis.naver.com/blogserver/like/v1/search/contents?q=BLOG[{blogId}_{postId}]
    Referer: 해당 포스트 URL          ← 없으면 막힌다

로그인이 필요 없다(2026-08-17 실측). 링크 형식 4가지 전부 확인:
    blog.naver.com/howmany70/224369601024        → 34
    blog.naver.com/kwang5166/224377478861        → 234
    m.blog.naver.com/viva-a/224379269605         → 20
    PostView.naver?blogId=viva-a&logNo=...       → 13
편차가 10~234 로 크다. 정렬 신호로 쓸 만하다는 뜻이다.

**배치가 안 된다.** q 를 반복해도, 콤마로 묶어도 첫 항목만 돌아온다(실측).
포스트당 1요청이라 예산 상한이 필수다 — 아래 LIKE_LOOKUP_MAX.

## 뉴스 자리를 건드리지 않는 이유

리액션 API 는 네이버 블로그 글에만 있다. 뉴스 기사는 점수를 낼 수 없다.
그렇다고 블로그를 전부 위로 올리면 상위 25건에서 뉴스가 밀려나
소재 구성이 통째로 바뀐다 — 뉴스 description 이 팩트시트 근거의 주축이고
assess_richness() 도 그걸로 재는데, 정렬 하나 바꾸려다 글 성격이 변한다.

그래서 **자리 배치는 그대로 두고 같은 종류끼리만 순서를 바꾼다.**
블로그가 있던 자리에는 블로그가, 뉴스가 있던 자리에는 뉴스가 그대로 온다.
상위 25건의 뉴스:블로그 비율이 변하지 않으므로 richness 는 영향받지 않고,
살아남는 블로그 자리에 가장 인기 있는 글이 들어간다.

## 실패하면 원래 순서를 쓴다

API 가 죽거나 느려도 그날 글은 나가야 한다. 예외는 삼키고 원래 순서를
반환한다. 대신 **몇 건을 실제로 점수 냈는지 반드시 출력한다** — 조용히
0건 점수 내고 "정렬했다"고 하면 다음 사람이 못 찾는다.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config as C

API = "https://apis.naver.com/blogserver/like/v1/search/contents"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# blog.naver.com/{blogId}/{postId} — 모바일(m.)·PostView 쿼리형도 함께 받는다
_PATH = re.compile(r"blog\.naver\.com/([A-Za-z0-9_-]+)/(\d+)")
_QUERY = re.compile(r"blogId=([A-Za-z0-9_-]+)(?=.*?logNo=(\d+))", re.S)


def blog_ref(link: str):
    """네이버 블로그 링크에서 (blogId, postId) 를 뽑는다. 아니면 None.

    PostList/PostView 같은 경로 세그먼트가 blogId 로 잡히지 않도록
    숫자 postId 가 함께 있는 형태만 허용한다.
    """
    if not link:
        return None
    m = _PATH.search(link)
    if m and m.group(1) not in ("PostView.naver", "PostList.naver"):
        return m.group(1), m.group(2)
    m = re.search(r"blogId=([A-Za-z0-9_-]+)", link)
    n = re.search(r"logNo=(\d+)", link)
    if m and n:
        return m.group(1), n.group(1)
    return None


def like_count(blog_id: str, post_id: str, timeout: float = None):
    """좋아요 수. 못 얻으면 None (0 과 구분해야 한다 — 0 은 진짜 0이다)."""
    timeout = timeout or C.LIKE_TIMEOUT_S
    q = urllib.parse.quote(f"BLOG[{blog_id}_{post_id}]", safe="")
    req = urllib.request.Request(
        f"{API}?suppress_response_codes=true&q={q}",
        headers={
            "User-Agent": UA,
            # Referer 가 없으면 막힌다. 포스트 URL 이어야 한다.
            "Referer": f"https://blog.naver.com/{blog_id}/{post_id}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))

    for c in data.get("contents", []):
        # reactionMap 에도 같은 값이 중복으로 들어온다. reactions 만 본다.
        for rc in c.get("reactions", []):
            if rc.get("reactionType") == "like":
                return int(rc.get("count", 0))
    return None


def rank(items, budget: int = None, sleep_ms: int = None, verbose: bool = True):
    """소재를 좋아요 순으로 정렬한다. 뉴스/블로그 자리 배치는 유지한다.

    items 각 원소에 'likes' 키를 붙인다(못 구하면 None).
    실패하거나 점수 낸 게 2건 미만이면 원래 순서를 그대로 돌려준다.
    """
    if not getattr(C, "POPULARITY_ENABLED", True) or not items:
        return items

    budget = budget if budget is not None else C.LIKE_LOOKUP_MAX
    sleep_ms = sleep_ms if sleep_ms is not None else C.LIKE_SLEEP_MS

    # 블로그 소재의 자리(index)를 원래 순서대로 모은다
    slots = [i for i, it in enumerate(items) if blog_ref(it.get("link", ""))]
    if len(slots) < 2:
        return items

    # 점수는 이번 호출에서 실제로 조회한 것만 쓴다. items 의 'likes' 키로
    # 추론하면 예산 초과로 건너뛴 항목의 이전 호출 값이 섞여 들어온다.
    looked, failed = 0, 0
    score = {}                       # slot 번호 -> 좋아요 수
    for i in slots:
        if looked >= budget:
            break
        ref = blog_ref(items[i]["link"])
        try:
            n = like_count(*ref)
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            n, failed = None, failed + 1
        items[i]["likes"] = n
        if n is not None:
            score[i] = n
        looked += 1
        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)

    if len(score) < 2:
        if verbose:
            print(f"  [알림] 좋아요 조회 {looked}건 시도, 유효 {len(score)}건 "
                  f"— 정렬을 건너뛰고 원래 순서를 씁니다.")
        return items

    # 같은 종류끼리만 재배치. 점수 없는 건(예산 초과·조회 실패)은 뒤로.
    ordered = sorted(slots, key=lambda i: (i not in score, -score.get(i, 0)))
    out = list(items)
    for slot, src in zip(slots, ordered):
        out[slot] = items[src]

    if verbose:
        top = max(score.values())
        skipped = len(slots) - looked
        msg = (f"  [벤치마킹] 블로그 {len(slots)}건 중 {looked}건 조회 "
               f"(유효 {len(score)}, 실패 {failed}) — 최고 좋아요 {top}")
        if skipped:
            msg += f", 예산({budget}) 초과로 {skipped}건 미조회"
        print(msg)
    return out


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for link in sys.argv[1:]:
        ref = blog_ref(link)
        if not ref:
            print(f"  블로그 아님: {link}")
            continue
        try:
            print(f"  likes={like_count(*ref)}  {ref[0]}/{ref[1]}")
        except Exception as e:
            print(f"  실패 {ref}: {type(e).__name__}: {e}")
