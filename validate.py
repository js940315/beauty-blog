# -*- coding: utf-8 -*-
"""v13 검증기 — 세는 일은 전부 여기서 한다.

설계 원칙 (설계서 8절): 모델에게 시키던 검증·기억·랜덤을 전부 코드로 이관.
검증 실패 시 전체 재작성이 아니라 errs 목록을 그대로 모델에 되먹인다.

⚠️ 기존 validator.py(일일 자동 파이프라인용)와 별개다. v13은 자체 규격
   (공백·점자 제외 850~1000자, 볼드 소제목)을 쓴다.
"""

import json
import re

import config as C

U2800 = "⠀"

# 매체명·출처 표현 (2026-08-13 사용자 확정). 목록은 config 한 곳에서만 늘린다.
_MEDIA_SUFFIX = re.compile(
    r"[가-힣A-Za-z]{1,6}(?:" + "|".join(C.MEDIA_SUFFIXES) + r")"
)
# 진부한 마무리 — titles.py 와 같은 기준. v13 은 .jpg 로 끝난다.
_VAGUE_TAIL = re.compile(
    r"(?:" + "|".join(re.escape(w) for w in C.VAGUE_TAILS) + r")"
    r"\s*[.?!]*\s*(?:\.jpg)?\s*$"
)


# 한국어 의문 종결어미. 물음표 없이 끝나는 질문을 잡는다.
_Q_END = re.compile(
    r"(?:까요|나요|을까|ㄹ까|일까|는지|던가요|습니까|ㅂ니까|겠어요|가요)"
    r"\s*[.?!]*\s*$"
)


def intro_lines(body: str, n: int = 3) -> list:
    """도입 n줄 (해시태그·빈 줄 제외한 내용 줄 기준)."""
    out = []
    for ln in body.split("\n"):
        v = ln.replace(U2800, "").strip()
        if v and not v.startswith("#"):
            out.append(v)
        if len(out) >= n:
            break
    return out


def _bigrams(text: str) -> set:
    t = re.sub(r"\s", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def intro_overlap(intro: str, others) -> float:
    """같은 날 다른 슬롯의 도입과 얼마나 겹치는가 (문자 2-gram 자카드).

    한국어는 조사가 붙어 어절 단위 비교가 잘 안 먹는다. 문자 단위로 잰다.
    """
    a = _bigrams(intro)
    if not a:
        return 0.0
    worst = 0.0
    for o in others:
        b = _bigrams(o or "")
        if not b:
            continue
        worst = max(worst, len(a & b) / len(a | b))
    return worst


def intro_form_error(intro3: list, fmt: str) -> str:
    """슬롯이 정한 포맷의 도입 문형을 지켰는가 (INSTRUCTION.md §2).

    포맷 이름만 다르고 도입이 똑같으면 로테이션은 아무 의미가 없다.
    독자는 골격이 아니라 첫 세 줄로 "또 그 글"인지 판단한다.
    """
    spec = C.FORMAT_SPECS.get(fmt)
    if not spec or not intro3:
        return ""
    test = spec.get("intro_test") or {}
    joined = " ".join(intro3)
    kind = test.get("kind")

    if kind == "quote":
        ok = intro3[0].startswith('"') or intro3[0].startswith("“")
        miss = "첫 줄을 큰따옴표 인용으로 열어라"
    elif kind == "number":
        ok = bool(re.search(r"\d", joined))
        miss = "도입에 결과 수치(숫자)를 넣어라"
    elif kind == "question":
        # 물음표만 보면 안 된다. 한국어 의문문은 "답일까요" 처럼 물음표 없이
        # 성립하고, 이 블로그 문체가 실제로 그렇게 쓴다. 물음표만 요구하면
        # 멀쩡한 도입이 계속 반려돼 무한 재생성이 난다 (2026-08-14 샘플에서 실측).
        ok = ("?" in joined) or any(_Q_END.search(l) for l in intro3)
        miss = "도입을 질문으로 열어라"
    elif kind == "words":
        ok = any(w in joined for w in test.get("words", ()))
        miss = f"도입에 {'/'.join(test.get('words', ())[:4])} 같은 표지가 있어야 한다"
    else:
        return ""

    if ok:
        return ""
    return (f"포맷 {fmt}({spec['name']}) 도입 문형 위반 — {spec['open']} {miss}. "
            f"현재 도입: {joined[:40]}")


def banned_phrase_hits(text: str) -> list:
    """INSTRUCTION.md §3 금지 문구를 찾는다.

    실제 산출물 6편에서 중복 검출된 상투구다. 사람이 매일 10편을 눈으로
    잡아낼 수 없으니 코드가 센다. 걸리면 그 슬롯만 재생성한다.
    """
    return [p for p in C.BANNED_PHRASES if p in text]


def self_voice_count(text: str) -> int:
    """화자 소감이 몇 번 나오는가 (§0-4, 최대 3회).

    예전 글은 8번째 줄에서 답이 다 나오고 나머지가 감상문이었다.
    소감으로 분량을 채우는 걸 막는다.
    """
    return sum(text.count(p) for p in C.SELF_VOICE_PATTERNS)


def media_hits(text: str) -> list:
    """본문에 남은 매체명·출처 표현을 찾는다.

    2026-08-13 사고: "스포츠조선 인터뷰에서", "헬스조선에서는", "뉴스컬처
    인터뷰에서" 가 한 글에 다 들어갔다. 원인은 모델이 아니라 예전 프롬프트가
    출처를 밝히라고 시키고 있었던 것. 프롬프트를 뒤집었고, 여기서 실제로 센다.
    """
    hits = []
    for term in C.MEDIA_TERMS:
        if term in text:
            hits.append(term)
    for phrase in C.SOURCE_PHRASES:
        if phrase in text:
            hits.append(phrase)
    for m in _MEDIA_SUFFIX.finditer(text):
        if m.group(0) not in hits:
            hits.append(m.group(0))
    return list(dict.fromkeys(hits))


# 앞 20자 구체 요소 판정은 titles.py 한 곳에만 둔다.
# 여기 복사본을 두면 한쪽만 고쳤을 때 "점수 함수는 통과인데 검증기는 반려"로
# 영원히 못 끝내는 교착이 생긴다 (2026-08-13).
from titles import head_specifics  # noqa: E402

# 알파벳 허용 목록. 팩트시트의 영문 고유명은 load 시 자동 추가된다.
ALPHA_ALLOW = {"jpg", "kg", "cm", "SM", "SK", "JYP", "YG", "CJ", "LG", "MBC",
               "KBS", "SBS", "tvN", "JTBC", "MZ"}

# 착장 명칭 — 이 단어가 많을수록 "그 사진"이 있어야만 읽히는 글이 된다.
# 발행자가 이미지를 찾느라 시간을 쓰는 게 시스템의 최대 병목이라 코드로 막는다.
# (2026-08-10 사용자 확정: 이미지 독립 원칙)
WEAR_WORDS = (
    "시스루", "홀터넥", "크롭", "미니드레스", "백리스", "오프숄더", "원피스",
    "드레스", "슈트", "수트", "재킷", "자켓", "코트", "니트", "청바지", "데님",
    "스커트", "블라우스", "슬랙스", "팬츠", "부츠", "하이힐", "스니커즈",
    "가디건", "트렌치", "점프수트", "베스트", "턱시도", "크롭톱", "레깅스",
)
WEAR_MAX = 3

REQUIRED_PERSON_FIELDS = ("name", "gender", "job")
VALID_HOT_TAGS = {"낙차", "장면", "실명", "발언", "반전"}


def _count(xs):
    d = {}
    for x in xs:
        d[x] = d.get(x, 0) + 1
    return d


def alpha_allow_from(factsheet: dict) -> set:
    """팩트시트 안의 영문 고유명(기업·그룹명 등)을 허용 목록에 자동 추가."""
    text = json.dumps(factsheet, ensure_ascii=False)
    return ALPHA_ALLOW | set(re.findall(r"[A-Za-z]+", text))


# ── 검증 1: 팩트시트 ───────────────────────────────────────────────────

def validate_factsheet(fs: dict) -> list:
    errs = []
    person = fs.get("person") or {}
    for f in REQUIRED_PERSON_FIELDS:
        if not (person.get(f) or "").strip():
            errs.append(f"person.{f} 비어 있음 — 소스에서 확정 못 하면 진행 불가")
    for i, q in enumerate(fs.get("quotes") or [], 1):
        if not (q.get("speaker") or "").strip():
            errs.append(f"quotes[{i}] 화자 누락 — 화자 없는 발언은 버려라")
    for i, h in enumerate(fs.get("hot_materials") or [], 1):
        if h.get("why_hot") not in VALID_HOT_TAGS:
            errs.append(f"hot_materials[{i}] why_hot='{h.get('why_hot')}' — "
                        f"낙차|장면|실명|발언|반전 중 하나여야 함")
    if not fs.get("hot_materials"):
        errs.append("hot_materials 비어 있음 — 화력 원석 없이는 제목을 못 뽑는다")
    # 동명이인 판정을 실제로 했는지 (2026-08-13 사용자 확정)
    if not (person.get("identity_anchor") or "").strip():
        errs.append(
            "person.identity_anchor 비어 있음 — 소스에 같은 이름의 다른 사람이 "
            "섞여 있는지 먼저 가리고, 주 인물을 대표작·소속으로 못 박아라")
    if "namesake_dropped" not in fs:
        errs.append(
            "namesake_dropped 필드 없음 — 동명이인이 없었다면 빈 배열로 명시해라")
    return errs


# ── 검증 2: 제목 ───────────────────────────────────────────────────────

def validate_titles(titles: list, factsheet: dict) -> list:
    errs = []
    person = factsheet.get("person") or {}
    name = person.get("name", "")
    job = (person.get("job") or "").strip()
    allow = alpha_allow_from(factsheet)

    if len(titles) != 10:
        errs.append(f"제목 {len(titles)}개 — 10개 필요")

    firsts, liz_endings, combos, angles = [], [], [], []
    for t in titles:
        s = t.get("title", "")
        no = t.get("no", "?")
        # 옛 .jpg 마감이 붙어 오면 떼고 센다 (아래에서 위반으로도 잡는다)
        core = s[:-4].rstrip() if s.endswith(".jpg") else s
        # 2026-08-14 사용자 확정: .jpg 마감 폐지.
        # 저장소 어디에도 근거가 없었고(diet-blog 복제 때 딸려온 관행),
        # 사용자가 실측한 홈판 상위 80개 제목 중 .jpg 로 끝나는 건 0개였다.
        # 일일 경로는 원래부터 안 붙였다. 발행자가 손으로 떼던 작업이 사라진다.
        if s.endswith(".jpg"):
            errs.append(f"{no}번 제목 끝 .jpg — 폐지됐다. 확장자 없이 문장으로 끝내라")
        if name and name in s:
            errs.append(f"{no}번 본명 노출: {name}")
        for w in re.findall(r"[A-Za-z]+", core):
            if w not in allow:
                errs.append(f"{no}번 알파벳 혼입: {w}")
        parts = s.split()
        firsts.append(parts[0] if parts else "")
        m = re.search(r"(\d+세\s*\S+)", s)
        if m:
            combos.append(m.group(1))
        if re.search(r"리즈\s?시절\s*$", core):
            liz_endings.append(no)
        if t.get("heat", 0) < 7:
            errs.append(f"{no}번 화력 {t.get('heat')} — 7 미만 재생성")
        # 홈판은 앞 20자만 노출한다. 그 자리가 "34세 배우"로 채워지면 아무
        # 그림도 안 그려진다 (2026-08-13 사고 제목이 정확히 이 유형).
        # 인물이 주인공이다. 사람이 빠지면 "누구 얘긴데?"조차 안 나온다.
        # 2026-08-13 사고: 10개 전부 인물 없이 사건만 있었다
        # ("부활절 인사 남긴 근황", "은은한 촛불 아래 만찬 사진")
        if not (job and job in s) and not any(l in s for l in C.PERSON_LABELS):
            errs.append(
                f"{no}번 여성 인물 지칭 없음 — 이 블로그의 주인공은 사건이 아니라 "
                f"사람이다. '여{job}' 처럼 person.job 을 여성 지칭으로 넣어라")
        if len(core) < C.TITLE_LEN_MIN:
            errs.append(
                f"{no}번 {len(core)}자 — {C.TITLE_LEN_MIN}자 미만이라 홈판 앞 20자를 "
                "다 못 쓴다. 노출 면적을 스스로 버리는 제목이다")

        spec = head_specifics(s[:20])
        if not spec:
            errs.append(
                f"{no}번 앞 20자에 구체 요소 없음 — 홈판에는 『{s[:20]}....』 만 뜬다. "
                "숫자(나이 제외)·장면어·관계 실명·신호어·모순 중 2개를 앞으로 당겨라")
        if _VAGUE_TAIL.search(s):
            errs.append(f"{no}번 진부한 마무리 — '{s[-12:]}' 는 '그래서 뭐?'가 나온다")
        hooks = re.split(r"[+|,\s]+", t.get("hook", ""))
        if len([h for h in hooks if h]) < 2:
            errs.append(f"{no}번 후킹 {t.get('hook')} — 도파민 요소 2개 이상 필수")
        angles.append(t.get("angle", ""))

    for w, c in _count(firsts).items():
        if w and c >= 3:
            errs.append(f"첫 어절 '{w}' {c}회 반복 (최대 2)")
    for combo, c in _count(combos).items():
        if c > 4:
            errs.append(f"도배: '{combo}' {c}개 (최대 4)")
    if len(liz_endings) > 3:
        errs.append(f"'리즈시절' 마감 {len(liz_endings)}개 (최대 3)")
    for a, c in _count(angles).items():
        if a and c > 5:
            errs.append(f"관점 '{a}' {c}개 — 한 관점 최대 5개, 관점을 분산해라")

    # §6 "여배우" 로 가리는 건 10개 중 6개까지. 전부 같으면 어뷰징으로 읽힌다.
    veiled = sum(1 for t in titles if C.FEMALE_LABEL_WATCH in t.get("title", ""))
    if veiled > C.FEMALE_LABEL_MAX:
        errs.append(
            f"'{C.FEMALE_LABEL_WATCH}' 로 가린 제목 {veiled}개 "
            f"(최대 {C.FEMALE_LABEL_MAX}) — 나머지는 '40대 배우', "
            f"'1990년생 여가수', '천만 배우' 처럼 다른 지칭으로 분산해라")
    return errs


# ── 검증 3: 본문 ───────────────────────────────────────────────────────

def _is_quote_only(stripped: str) -> bool:
    """큰따옴표로 시작하고 끝나는 줄 — 도입 인용구(1개) 또는 소제목(4개)."""
    return (len(stripped) >= 3
            and stripped[0] in '"“' and stripped[-1] in '"”')


def _is_subhead(stripped: str) -> bool:
    # 소제목은 별표 없이 큰따옴표만 쓴다 (네이버가 마크다운을 해석 못 해
    # 별표가 그대로 노출된다 — 실측). 별표 형식은 아래에서 따로 잡는다.
    return _is_quote_only(stripped) or stripped.startswith("**")


def _is_tag(stripped: str) -> bool:
    return stripped.startswith("#")


def namesake_contamination(body: str, factsheet: dict) -> list:
    """동명이인이 섞였을 가능성을 본문에서 잡는다 (2026-08-14 사용자 지시).

    NAMESAKE_DROP 은 이미 당한 이름만 막는다. 처음 보는 동명이인은 못 막는다.
    본문에 나온 직업·이력 표식이 팩트시트에 근거가 없으면 남의 이력이 옮겨붙은 것이다.
    건강비버 2026-08-14 사고가 정확히 이 형태였다 — 배우 서현진 글에
    "미스코리아 출신이자 전직 MBC 아나운서"가 들어갔고 그건 동명이인 쪽 이력이었다.
    """
    if not isinstance(factsheet, dict):
        return []
    person = factsheet.get("person") or {}
    grounds = " ".join(str(person.get(k, "")) for k in ("job", "identity_anchor", "notes"))
    for k in ("life_events", "quotes", "habits"):
        v = person.get(k) or factsheet.get(k) or []
        if isinstance(v, list):
            grounds += " " + " ".join(str(x) for x in v)
    grounds += " " + " ".join(str(x) for x in (factsheet.get("namesake_dropped") or []))

    plain = body.replace("⠀", "")
    found = [m for m in getattr(C, "PROFESSION_MARKERS", ())
             if m in plain and m not in grounds]
    if not found:
        return []
    anchor = (person.get("identity_anchor") or "").strip() or "(비어 있음)"
    return [f"동명이인 의심: 본문에 팩트시트 근거가 없는 이력 {found} 가 나왔다. "
            f"주 인물은 '{anchor}' 다. 다른 사람 이력이 섞였는지 확인하고, "
            f"근거가 있으면 팩트시트에 넣어라"]


def validate_body(body: str, factsheet: dict, state: dict, debate: str = "",
                  fmt: str = "", same_day_intros=()) -> list:
    """debate: 코드가 주입한 논쟁 질문. 본문에 실제로 들어갔는지 대조한다.
    fmt: 슬롯이 정한 포맷 키(A~E). 도입 문형을 이걸로 판정한다.
    same_day_intros: 오늘 이미 나온 다른 슬롯들의 도입. 중복을 잡는다.
    """
    errs = []

    # §2 도입 3줄 — 포맷별 문형 + 같은 날 중복 (2026-08-14 사용자 확정)
    intro3 = intro_lines(body)
    if fmt:
        e = intro_form_error(intro3, fmt)
        if e:
            errs.append(e)
    if same_day_intros:
        joined = " ".join(intro3)
        ov = intro_overlap(joined, same_day_intros)
        if ov >= C.INTRO_OVERLAP_MAX:
            errs.append(
                f"도입 3줄이 오늘 다른 슬롯과 {ov:.0%} 겹친다 "
                f"(허용 {C.INTRO_OVERLAP_MAX:.0%} 미만) — 첫 세 줄을 통째로 다시 써라. "
                "독자는 골격이 아니라 도입으로 '또 그 글'인지 판단한다")
    errs += namesake_contamination(body, factsheet)
    person = factsheet.get("person") or {}
    job = person.get("job", "")
    allow = alpha_allow_from(factsheet)

    lines = body.split("\n")

    # 글자수 — 2026-08-14 사용자 확정으로 일일 경로와 자를 통일했다.
    #   기준: 네이버 글자수세기 공백제외 = 보이는 글자 + 점자 빈칸
    #   범위: 본문만 (제목·해시태그 제외)
    # 예전엔 v13만 "점자 제외 + 해시태그 포함 780~900" 이라 실제로는 네이버
    # 기준 980~1100자로 일일보다 길었다. 기존 완성본 63편 실측 중앙값 966자.
    _tagless = [ln for ln in lines if not _is_tag(ln.replace(U2800, "").strip())]
    n = (sum(len(re.sub(r"\s", "", ln.replace(U2800, ""))) for ln in _tagless)
         + sum(ln.count(U2800) for ln in _tagless))
    if not (C.BODY_CHARS_MIN <= n <= C.BODY_CHARS_MAX):
        hint = ("부족: 짧은 챕터에 3줄 문단 추가" if n < C.BODY_CHARS_MIN
                else "초과: 마지막 챕터·마무리에서 곁가지 문단 삭제")
        errs.append(f"네이버기준 {n}자 "
                    f"(허용 {C.BODY_CHARS_MIN}~{C.BODY_CHARS_MAX}) — {hint}")

    # 첫 글자 큰따옴표
    first_visible = ""
    for ln in lines:
        s = ln.replace(U2800, "").strip()
        if s:
            first_visible = s
            break
    if not first_visible.startswith('"') and not first_visible.startswith("“"):
        errs.append("본문 첫 글자가 큰따옴표가 아님 — 도입 1단은 인용구 단독 줄")

    para = 0
    for i, ln in enumerate(lines, 1):
        stripped = ln.replace(U2800, "").strip()
        if stripped:
            para += 1
            if not _is_tag(stripped) and not ln.endswith(U2800):
                errs.append(f"{i}행 점자 빈 칸 누락")
            if (len(stripped) > 19 and not _is_tag(stripped)
                    and not _is_subhead(stripped)):
                errs.append(f"{i}행 {len(stripped)}자 — 19자 초과: {stripped[:22]}")
            if re.match(r"^[·\-\*o]\s|^\d+\.\s", stripped):
                errs.append(f"{i}행 불릿·목차 마커")
            for w in re.findall(r"[A-Za-z]+", stripped):
                if w not in allow:
                    errs.append(f"{i}행 알파벳: {w}")
            if para > 3 and not _is_subhead(stripped) and not _is_tag(stripped):
                errs.append(f"{i}행 포함 단락 4줄 이상 — 벽돌. 빈 줄로 끊어라")
        else:
            if ln.replace(U2800, "").strip() == "" and ln != U2800 * 3:
                if ln.strip() == "" and ln != "":
                    pass  # 공백만 있는 줄도 아래에서 잡는다
                if ln != U2800 * 3:
                    errs.append(f"{i}행 빈 줄이 {U2800 * 3} 형식 아님")
            para = 0

    if ".jpg" in body:
        errs.append("본문에 .jpg 노출")

    # 착장 나열 검사 — 옷 이름이 쌓이면 사진 없이는 못 읽는 글이 된다
    visible_text = " ".join(l.replace(U2800, "").strip() for l in lines
                            if not _is_tag(l.replace(U2800, "").strip()))
    wear_hits = [w for w in WEAR_WORDS if w in visible_text]
    wear_n = sum(visible_text.count(w) for w in wear_hits)
    if wear_n > WEAR_MAX:
        errs.append(
            f"착장 명칭 {wear_n}회 (허용 {WEAR_MAX}회) — {', '.join(wear_hits[:5])}. "
            "옷 이름 대신 반응·발언·관리법으로 바꿔라 (사진 없이 읽혀야 한다)")
    if "**" in body:
        errs.append("소제목에 별표(**) 사용 — 네이버에 그대로 노출된다. 큰따옴표만 쓸 것")

    # 매체명·출처 표기 (2026-08-13 사용자 확정)
    for hit in media_hits(visible_text):
        errs.append(
            f"매체·출처 표기: '{hit}' — 출처는 한 글자도 쓰지 않는다. "
            "'이렇게 말한 적이 있어요' 처럼 출처 없이 풀어라")

    # 동명이인 (2026-08-13 사용자 확정)
    # 팩트시트가 버린 쪽의 표식이 본문에 나오면 두 사람을 섞어 쓴 것이다.
    for marker in factsheet.get("namesake_dropped") or []:
        marker = (marker or "").strip()
        if marker and marker in visible_text:
            errs.append(
                f"동명이인 소재 혼입: '{marker}' — 같은 이름의 다른 사람이다. "
                f"이 글은 {person.get('identity_anchor') or '주 인물'} 한 사람만 다룬다")

    # 큰따옴표 단독 줄 = 도입 인용구 1 + 소제목 5, 딱 여섯이어야 한다.
    # (2026-08-07 사용자 확정: 마무리가 무거워 챕터를 5개로 늘림)
    quote_lines = [l for l in lines
                   if _is_quote_only(l.replace(U2800, "").strip())]
    if len(quote_lines) != 6:
        errs.append(f"큰따옴표 단독 줄 {len(quote_lines)}개 — "
                    "도입 인용구 1 + 소제목 5 = 6개여야 함")

    # 해시태그
    tags = [l.replace(U2800, "").strip() for l in lines
            if _is_tag(l.replace(U2800, "").strip())]
    if len(tags) != 8:
        errs.append(f"해시태그 {len(tags)}개 — 8개 필요")
    if job and "배우" not in job:
        for tg in tags:
            if tg.startswith("#여배우") or tg.startswith("#배우"):
                errs.append(f"직업 불일치 태그: {tg} (실제 직업: {job})")
    name = person.get("name", "")
    if name and tags and not any(name in tg for tg in tags):
        errs.append(f"해시태그에 인물명 없음 — 1번 태그는 #{name}")

    # 8번 태그가 글의 끝
    trailing = [l for l in lines[::-1] if l.replace(U2800, "").strip()]
    if trailing and not _is_tag(trailing[0].replace(U2800, "").strip()):
        errs.append("마지막 줄이 해시태그가 아님 — 8번 태그 뒤에 아무것도 붙이지 마라")

    # ── INSTRUCTION.md 강제 항목 (2026-08-14) ──────────────────────────
    # 프롬프트에 적어두는 것만으로는 안 지켜진다. 여기서 실제로 센다.

    # §3 금지 문구 — 실제 산출물에서 반복 검출된 상투구
    for p in banned_phrase_hits(visible_text):
        errs.append(f"금지 문구: '{p}' — 상투구다. 다른 말로 바꿔 써라")

    # §0-4 화자 소감은 최대 3회. 소감으로 분량을 채우면 정보밀도가 죽는다
    voices = self_voice_count(visible_text)
    if voices > C.SELF_VOICE_MAX:
        errs.append(f"화자 소감 {voices}회 (최대 {C.SELF_VOICE_MAX}) — "
                    "소감 단락을 빼고 그 자리에 사실 한 조각을 넣어라")

    # §4-2 브릿지 — 정답을 뒤로 미룬 만큼 중간을 버텨줄 장치가 필요하다
    bridges = [b for b in C.BRIDGE_POOL if b in visible_text]
    if len(bridges) < C.BRIDGE_MIN:
        errs.append(f"브릿지 {len(bridges)}개 (최소 {C.BRIDGE_MIN}) — "
                    f"소제목 사이에 다음 궁금증을 만드는 한 줄을 넣어라. "
                    f"예: {C.BRIDGE_POOL[0]}")

    # §4-3 스크롤 유인 블록 — 번호 리스트(숫자+점자 들여쓰기) 또는 ❌/⭕ 대비
    numbered = sum(1 for l in lines
                   if re.match(r"^\d+\s+\S", l.replace(U2800, "").strip()))
    has_contrast_block = ("❌" in body) or ("⭕" in body)
    if numbered < 3 and not has_contrast_block:
        errs.append("스크롤 유인 블록 없음 — 번호 리스트 3줄(숫자 뒤 점자 들여쓰기) "
                    "또는 ❌/⭕ 대비 블록을 1개 넣어라. 눈이 걸려야 체류가 붙는다")

    # 반응 유도 장치 금지 (2026-08-16 사용자 확정으로 논쟁 질문·행동 지시 폐기).
    # 독자는 시키는 걸 알아채고, 알아채는 순간 창을 닫는다.
    for p in C.NUDGE_PHRASES:
        if p in visible_text:
            errs.append(f"반응 유도 문구: '{p}' — 시키지 말고 이야기로 자연스럽게 맺어라")

    # 하트 구걸 금지 (예전 규격은 ❤ 를 오히려 강제했다)
    if "❤" in body:
        errs.append("하트 구걸 문구 — 폐지됐다. 행동 지시 + 경험 요청으로 마감해라")

    # 마감·CTA 중복 (state 대조)
    for old in state.get("recent_endings", []):
        if old and old in body:
            errs.append(f"최근 글과 마감 문장 중복: {old[:20]}…")
    return errs


# ── 자체 테스트 ─────────────────────────────────────────────────────────

def _selftest():
    fs = {"person": {"name": "홍길동", "gender": "여", "job": "무용가",
                     "identity_anchor": "봄의 왈츠 안무를 맡은 무용가"},
          "namesake_dropped": [],
          "quotes": [{"speaker": "홍길동", "text": "말", "context": ""}],
          "hot_materials": [{"material": "x", "why_hot": "낙차"}]}

    # 검증 1: 정상 통과 / 직업 빠지면 잡힘
    assert validate_factsheet(fs) == []
    bad_fs = json.loads(json.dumps(fs))
    bad_fs["person"]["job"] = ""
    assert any("person.job" in e for e in validate_factsheet(bad_fs))

    # 동명이인 판정을 건너뛴 팩트시트는 통과하지 못한다 (2026-08-13)
    no_id = json.loads(json.dumps(fs))
    no_id["person"]["identity_anchor"] = ""
    del no_id["namesake_dropped"]
    errs = validate_factsheet(no_id)
    assert any("identity_anchor" in e for e in errs)
    assert any("namesake_dropped" in e for e in errs)

    # 검증 2: 본명 노출·jpg 누락·화력 미달·후킹 1개가 전부 잡히는지
    # "N세 직업" 덩어리는 4개까지만 허용되므로 정상 케이스는 형식을 섞는다.
    # 2026-08-13: 인물 지칭(job)과 24자 하한이 생겨서 예시를 그에 맞게 늘렸다.
    # 2026-08-14: .jpg 마감 폐지로 확장자를 뗐다.
    forms = ["40세 무용가의 무대를 멈춘 반전 순간{i}",
             "1986년생 무용가의 거울 앞 그 장면{i}",
             "무대를 멈춘 무용가가 택한 마지막 선택{i}"]
    titles = [{"no": i, "title": f"수식어{i} " + forms[i % 3].format(i=i),
               "angle": f"관점{(i - 1) // 4}", "hook": "낙차+장면", "heat": 8}
              for i in range(1, 11)]
    assert validate_titles(titles, fs) == []
    bad = json.loads(json.dumps(titles))
    bad[0]["title"] = "홍길동의 리즈시절"          # 본명 노출
    bad[1]["heat"] = 5                             # 화력 미달
    bad[2]["hook"] = "낙차"                        # 후킹 1개
    bad[3]["title"] = bad[3]["title"] + ".jpg"     # 폐지된 확장자 마감
    errs = validate_titles(bad, fs)
    assert any("본명 노출" in e for e in errs)
    assert any("제목 끝 .jpg" in e for e in errs), errs
    assert any("7 미만" in e for e in errs)
    assert any("2개 이상" in e for e in errs)

    # 도배 검출: 같은 "40세 무용가" 5개
    combo = [{"no": i, "title": f"40세 무용가 사건{i}의 진짜 이유{i}",
              "angle": f"관점{i % 3}", "hook": "낙차+격차", "heat": 8}
             for i in range(1, 11)]
    assert any("도배" in e for e in validate_titles(combo, fs))

    # 검증 3: 형식 위반 검출
    B = U2800
    good_lines = ['"그 말 한마디였습니다"' + B, B * 3]
    for ch in range(5):
        good_lines.append(f'"소제목 어쩌고 {ch + 1}번"' + B)
        good_lines.append(B * 3)
        for p in range(4):
            good_lines += ["여기 열다섯 자짜리 문장이" + B,
                           "이어지는 열네 자 문장과" + B,
                           "마무리 열세 자 문장이" + B, B * 3]
        # 2026-08-14 신규 규격: 브릿지 2개 · 스크롤 유인 블록 1개
        if ch == 1:
            good_lines += [C.BRIDGE_POOL[0] + B, B * 3]
        if ch == 2:
            good_lines += [C.BRIDGE_POOL[1] + B, B * 3]
        if ch == 3:
            good_lines += [B * 2 + "1 세안 뒤 3분 안에" + B,
                           B * 2 + "2 목까지 같이 바르기" + B,
                           B * 2 + "3 아침엔 물로만" + B, B * 3]
    # 논쟁 질문은 이제 한 줄에 들어간다 (INJECT_MAX_CHARS 이하로 줄였다)
    debate = C.DEBATE_POOL[0]
    good_lines += [debate + B, B * 3]
    good_lines += ["#홍길동", "#홍길동근황", "#홍길동미모", "#미모비결",
                   "#관계인물", "#대표작", "#무용가미모", "#무용가관리"]
    body = "\n".join(good_lines)
    errs = validate_body(body, fs, {}, debate=debate)
    # 글자수는 샘플이라 어긋날 수 있다 — 그 외 위반이 없어야 한다
    assert all("네이버기준" in e for e in errs), errs

    broken = body.replace(B * 3, "", 1).replace('"그 말', "그 말", 1) + "\n덧붙임"
    errs = validate_body(broken, fs, {})
    assert any("큰따옴표" in e for e in errs)
    assert any("해시태그가 아님" in e for e in errs)

    # 착장 나열은 잡혀야 한다 (이미지 독립 원칙)
    wearful = body.replace("여기 열다섯 자짜리 문장이" + B,
                           "드레스에 시스루 재킷 코트" + B, 1)
    wearful = wearful.replace("이어지는 열네 자 문장과" + B,
                              "니트와 청바지 부츠까지" + B, 1)
    errs = validate_body(wearful, fs, {})
    assert any("착장 명칭" in e for e in errs), errs

    # 별표 소제목은 즉시 잡혀야 한다 (네이버 노출 사고 방지)
    starred = body.replace('"소제목 어쩌고 1번"' + B, '**"소제목 어쩌고 1번"**' + B, 1)
    errs = validate_body(starred, fs, {})
    assert any("별표" in e for e in errs), errs

    # 직업 불일치 태그
    errs = validate_body(body.replace("#무용가미모", "#여배우미모"), fs, {})
    assert any("직업 불일치" in e for e in errs)

    # 도입 3줄 — 포맷별 문형 판정 (INSTRUCTION.md §2, 2026-08-14)
    assert intro_form_error(['"인용으로 열었다"', "둘째 줄", "셋째 줄"], "A") == ""
    assert intro_form_error(["인용이 없다", "둘째 줄", "셋째 줄"], "A") != ""
    assert intro_form_error(["48kg을 지킨다", "둘째", "셋째"], "B") == ""
    assert intro_form_error(["체중을 지킨다", "둘째", "셋째"], "B") != ""
    assert intro_form_error(["맞을까요?", "둘째", "셋째"], "C") == ""
    # 물음표 없는 한국어 의문문도 질문으로 인정해야 한다 (2026-08-14 실측)
    assert intro_form_error(["하루 한 끼가 답일까요", "둘째", "셋째"], "C") == ""
    assert intro_form_error(["어느 쪽이 나은가요", "둘째", "셋째"], "C") == ""
    assert intro_form_error(["맞습니다", "둘째", "셋째"], "C") != ""
    assert intro_form_error(["다들 그렇게 압니다", "둘째", "셋째"], "D") == ""
    assert intro_form_error(["그렇게 압니다", "둘째", "셋째"], "D") != ""
    assert intro_form_error(["혹시 하고 계신가요", "둘째", "셋째"], "E") == ""
    assert intro_form_error(["습관이 있습니다", "둘째", "셋째"], "E") != ""

    # 주입 문장 길이 — 30자를 넘으면 4줄이 되어 문장 한가운데에 빈 줄이 박힌다
    # (2026-08-16 실측 사고: "치워보세요. 몇 개나 / ⠀⠀⠀ / 나왔는지 궁금해요.")
    for pool_name, pool in (("CLOSING_POOL", C.CLOSING_POOL),
                            ("DEBATE_POOL", C.DEBATE_POOL),
                            ("BRIDGE_POOL", C.BRIDGE_POOL)):
        for s in pool:
            assert len(s) <= C.INJECT_MAX_CHARS, \
                f"{pool_name} 문장이 {len(s)}자 — {C.INJECT_MAX_CHARS}자 초과라 단락이 깨진다: {s}"

    # 도입 3줄 — 같은 날 중복 판정
    base = "그 말이 나온 자리에서 다들 숟가락을 놨습니다 식단 얘기였어요"
    near = "그 말이 나온 자리에서 다들 숟가락을 놨어요 식단 얘기였습니다"
    far = "48kg을 10년째 지킵니다 따라할 수 있는 건 딱 세 가지"
    assert intro_overlap(base, [near]) >= C.INTRO_OVERLAP_MAX
    assert intro_overlap(base, [far]) < C.INTRO_OVERLAP_MAX

    # 매체·출처 표기는 잡혀야 한다 (2026-08-13 사용자 확정)
    sourced = body.replace("여기 열다섯 자짜리 문장이" + B,
                           "스포츠조선 인터뷰에서" + B, 1)
    errs = validate_body(sourced, fs, {})
    assert any("스포츠조선" in e for e in errs), errs
    assert any("인터뷰에서" in e for e in errs), errs
    # 목록에 없는 신생 매체도 꼬리표로 잡는다
    unknown = body.replace("이어지는 열네 자 문장과" + B, "가나다일보에 따르면" + B, 1)
    assert any("가나다일보" in e for e in validate_body(unknown, fs, {}))

    # 동명이인 소재 혼입 (2026-08-13 사고 재현)
    ns_fs = json.loads(json.dumps(fs))
    ns_fs["namesake_dropped"] = ["솔로지옥5 출연자", "고니"]
    mixed = body.replace("마무리 열세 자 문장이" + B, "고니 얘기를 섞으면" + B, 1)
    errs = validate_body(mixed, ns_fs, {})
    assert any("동명이인" in e for e in errs), errs
    # 안 섞였으면 조용해야 한다
    assert not any("동명이인" in e for e in validate_body(body, ns_fs, {}))

    # 밋밋한 제목 (앞 20자 구체 요소 0 / 진부한 마무리) — 2026-08-13 사고 제목
    dull = json.loads(json.dumps(titles))
    dull[0]["title"] = '"엄격한 식단 안 한다는" 34세 무용가, 그런데 피부는 도자기라는데'
    errs = validate_titles(dull, fs)
    assert any("앞 20자에 구체 요소 없음" in e for e in errs), errs
    assert any("진부한 마무리" in e for e in errs), errs

    print("validate.py 자체 테스트 전부 통과")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _selftest()
