# -*- coding: utf-8 -*-
"""유사문서 판정 — 이미 발행한 글과 겹치면 막는다.

2026-08-16 사용자 확정:
    "중복유사문서로 판명나면 절대 안된다. 이미 내 블로그에 올라간
     제목 패턴 본문 일치도 판단해서 겹치면 안돼"

네이버는 유사문서로 판정되면 검색에서 걷어낸다. 하루 10편씩 같은 틀로
찍어내는 구조라 이 위험이 가장 크다. 실제로 2026-08-16 회차에서
"저는 이 대목에서 제 화장대를 다시 살펴봤습니다" 가 5편 전부에 들어갔다.

세 축으로 잰다.
  1. 문장 지문   — 문장이 통째로 재사용됐는가 (가장 확실한 신호)
  2. 본문 유사도 — 문자 4-gram 자카드. 문장이 조금씩 달라도 잡는다
  3. 제목 패턴   — 숫자·직업을 자리표로 바꾼 뒤 같은 골격이 반복되는가

state.json 에 편당 지문을 남긴다. 본문 전체를 저장하지 않으므로 가볍다.
"""

import hashlib
import re

BR = "⠀"
# 본문 유사도를 잴 때 쓰는 n-gram 길이. 4면 조사 변화에도 견딘다.
NGRAM = 4
# MinHash 서명 길이. 편당 이만큼의 정수만 저장한다.
SIGNATURE = 96

# ── 임계값 ──────────────────────────────────────────────────────────────
# 감으로 정하면 안 된다. 너무 느슨하면 아무것도 안 잡히고, 너무 조이면
# 매일 반려가 난다. 기존 발행분 56편(쌍 1540개)을 실측해서 정했다:
#     본문 유사도  중앙 3% / 상위 1% 7% / 최대 11%
#     문장 재사용  최대 1개
# 즉 정상적으로 쓴 글끼리는 11% 를 넘지 않는다. 15% 를 넘으면 그건
# 같은 문장을 돌려 쓰고 있다는 뜻이다.
SENTENCE_HITS_MAX = 1      # 과거 글과 통째로 같은 문장은 1개까지 (실측 최대 1)
BODY_SIM_MAX = 0.15        # 본문 4-gram 자카드 (실측 최대 11%)
TITLE_SIM_MAX = 0.65       # 제목 골격끼리의 3-gram 자카드


def _norm(text: str) -> str:
    """점자·공백·문장부호를 걷어낸 비교용 문자열."""
    t = text.replace(BR, "")
    t = re.sub(r"[^\w가-힣]", "", t)
    return t


def sentences(body: str):
    """본문을 문장 단위로 자른다. 줄바꿈이 문장 중간에 들어가므로 먼저 합친다."""
    flat = " ".join(l.replace(BR, "").strip()
                    for l in body.split("\n") if l.replace(BR, "").strip())
    flat = re.sub(r"^#.*$", "", flat, flags=re.M)
    out = []
    for s in re.split(r"(?<=[.!?])\s+|(?<=습니다)\s+|(?<=었어요)\s+", flat):
        s = s.strip()
        if len(_norm(s)) >= 8 and not s.startswith("#"):
            out.append(s)
    return out


def _h(s: str) -> int:
    return int(hashlib.md5(s.encode("utf-8")).hexdigest()[:12], 16)


def _shingles(text: str) -> set:
    t = _norm(text)
    return {t[i:i + NGRAM] for i in range(len(t) - NGRAM + 1)}


def signature(body: str) -> list:
    """본문의 MinHash 서명. 가장 작은 해시값 SIGNATURE 개."""
    hs = sorted(_h(s) for s in _shingles(body))
    return hs[:SIGNATURE]


def _sig_jaccard(a, b) -> float:
    """서명끼리의 자카드 근사. 둘 다 '가장 작은 값들' 이라 교집합 비율이 유사도다."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / len(sa | sb)


def title_pattern(title: str) -> str:
    """제목의 골격만 남긴다. 숫자·직업·단위를 자리표로 바꾼다.

    "47세인데 30대 피부라는 여배우"  →  "#인데 # 피부라는 @"
    같은 골격이 반복되면 사람 눈에도 "또 그 제목" 으로 읽힌다.
    """
    t = title[:-4] if title.endswith(".jpg") else title
    t = re.sub(r"\d+\s*(?:세|대|kg|cm|억|만|년|개|위|주|살)", "#", t)
    t = re.sub(r"\d+", "#", t)
    t = re.sub(r"여배우|여가수|여자\s*\S+|배우|가수|모델|인플루언서|톱스타", "@", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _tri(text: str) -> set:
    t = re.sub(r"\s", "", text)
    return {t[i:i + 3] for i in range(len(t) - 2)}


def fingerprint(title: str, body: str) -> dict:
    """state.json 에 남길 지문. 본문 전체를 저장하지 않는다."""
    return {
        "sig": signature(body),
        "sents": sorted({_h(_norm(s)) for s in sentences(body)}),
        "tpat": title_pattern(title),
    }


def check(title: str, body: str, history) -> list:
    """이미 발행한 글들과 겹치는지 본다. history = state 의 recent_posts.

    반환: 위반 메시지 목록. 비어 있으면 통과.
    """
    errs = []
    fp = fingerprint(title, body)
    my_sents = set(fp["sents"])

    worst_sim, worst_who = 0.0, ""
    shared_all = []
    for past in history:
        pf = past.get("fp") or {}
        if not pf:
            continue
        who = f"{past.get('date', '?')} {past.get('person', '?')}"

        sim = _sig_jaccard(fp["sig"], pf.get("sig") or [])
        if sim > worst_sim:
            worst_sim, worst_who = sim, who

        shared = my_sents & set(pf.get("sents") or [])
        if len(shared) > SENTENCE_HITS_MAX:
            shared_all.append((who, len(shared)))

    if worst_sim >= BODY_SIM_MAX:
        errs.append(
            f"유사문서 위험 — 본문이 이미 발행한 글과 {worst_sim:.0%} 겹친다 "
            f"({worst_who}, 허용 {BODY_SIM_MAX:.0%} 미만). "
            "네이버가 유사문서로 판정하면 검색에서 걷어낸다. "
            "소재와 문장을 바꿔서 다시 써라")

    for who, n in shared_all[:3]:
        errs.append(
            f"문장 재사용 {n}개 — {who} 글과 통째로 같은 문장이 있다 "
            f"(허용 {SENTENCE_HITS_MAX}개). 상투구를 쓰고 있다는 뜻이다")

    # 제목 골격 — 완전 일치를 보면 안 된다. 숫자·직업만 치환해도 명사가 남아
    # 사실상 원문 비교가 되기 때문이다(실측: 56편 전부 골격이 달랐다).
    # 골격끼리 3-gram 자카드로 재야 "47세인데 30대 피부라는"과
    # "58세인데 20대 피부라는"이 같은 틀이라는 걸 잡는다.
    my_tp = _tri(fp["tpat"])
    for past in history:
        pf = past.get("fp") or {}
        other = _tri(pf.get("tpat") or "")
        if not other:
            continue
        sim = len(my_tp & other) / len(my_tp | other) if (my_tp | other) else 0.0
        if sim >= TITLE_SIM_MAX:
            errs.append(
                f"제목 골격 반복 — {past.get('date', '?')} "
                f"'{past.get('title', '')[:24]}' 와 {sim:.0%} 같은 틀이다. "
                "숫자 자리·직업 자리·어미를 바꿔라")
            break

    return errs


# ── 자체 테스트 ─────────────────────────────────────────────────────────

def _selftest():
    a = ("48kg인데 야식은 끊은 적 없다는 배우입니다. "
         "세안 뒤 3분 안에 수분크림을 바른다고 했습니다. "
         "새벽마다 한강을 뛴다고 알려졌습니다.")
    # 거의 같은 글 (어미만 바꿈)
    b = ("48kg인데 야식은 끊은 적 없다는 배우예요. "
         "세안 뒤 3분 안에 수분크림을 바른다고 했어요. "
         "새벽마다 한강을 뛴다고 알려졌어요.")
    # 완전히 다른 글
    c = ("65억 건물주라는 가수의 이야기입니다. "
         "하루 한 끼만 먹는다고 오래전에 밝혔습니다. "
         "잡티가 늘었다는 고백도 있었습니다.")

    hist = [{"date": "2026-08-16", "person": "홍길동",
             "fp": fingerprint("48kg인데 야식은 끊은 적 없다는 40대 여배우", a)}]

    assert check("전혀 다른 65억 건물주라는 35세 여가수", c, hist) == [], "다른 글을 잡으면 안 된다"
    errs = check("48kg인데 야식은 끊은 적 없다는 40대 여배우", b, hist)
    assert any("유사문서" in e for e in errs), errs

    # 문장 재사용 — 상투구가 두 편에 그대로 들어간 경우
    d = ("전혀 다른 이야기로 시작합니다. 65억 건물주라는 가수예요. "
         "세안 뒤 3분 안에 수분크림을 바른다고 했습니다. "
         "새벽마다 한강을 뛴다고 알려졌습니다.")
    errs = check("완전히 다른 제목 하나 65억 여가수", d, hist)
    assert any("문장 재사용" in e for e in errs), errs

    # 제목 골격 — 같은 틀은 잡고, 다른 틀은 놔둬야 한다
    h2 = [{"date": "2026-08-16", "person": "x", "title": "47세인데 30대 피부라는 여배우",
           "fp": fingerprint("47세인데 30대 피부라는 여배우", c)}]
    errs = check("58세인데 20대 피부라는 여배우", c, h2)
    assert any("제목 골격" in e for e in errs), errs
    errs = check("공유가 눈부셨다고 말해준 여배우, 그 검을 뽑던 장면", c, h2)
    assert not any("제목 골격" in e for e in errs), errs

    print("dedup.py 자체 테스트 전부 통과")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _selftest()
