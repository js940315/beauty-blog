"""제목 10개 → 점수 → 1개 확정.

설계 원칙: 제목 선택은 LLM이 하지 않는다.
모델은 후보만 만들고, 고르는 건 아래 점수 함수다.
홈판은 앞 20자만 노출되므로 총점의 절반 이상이 앞 20자에서 나온다.

튜닝은 프롬프트가 아니라 config.WEIGHTS 숫자로 한다.
"""

import re

import config as C

_NUM = re.compile(r"\d+")
_AGE = re.compile(r"\d+\s*(?:세|대)")
# 나이 값을 꺼내 쓰기 위한 판. _age_gap() 이 대비를 계산한다.
_AGE_TOKEN = re.compile(r"(\d+)\s*(?:세|대|살)")
_JOB = re.compile(
    r"여배우|배우|가수|모델|아나운서|방송인|개그우먼|코미디언|트로트|"
    r"여신|스타|아이돌|디바|MC|셀럽|인플루언서|댄서|크리에이터|무용가"
)
# 정답이 너무 쉬운 조합: 유명 남자 실명 + 혼인 관계어.
# "장동건과 16년째 부부"는 누가 봐도 답이 나와서 클릭베이트가 죽는다.
_MARRIAGE = re.compile(r"남편|부부|아내|와이프|배우자|결혼|재혼")
# 유지 기간형: 37년째 / 20년간 / 15년 동안
_DURATION = re.compile(r"\d+\s*년\s*(?:째|간|동안|넘게|째로)")
_WEIGHT_TOKEN = re.compile(r"\d+\s*(?:kg|KG|킬로|키로)|\d+\s*사이즈|몸무게|체중")
# 감량 낙차형: 58kg에서 44kg으로 / 58 → 44 / 70kg대에서
_DROP = re.compile(
    r"\d+\s*(?:kg|KG|킬로|키로)?\s*(?:대)?\s*(?:에서|부터|→|->)\s*\d+"
)
_QUOTE = re.compile(r'["“”‘’\']')
# 이슈 선행형: 제목이 짧은 인용/이슈로 시작하고 따옴표가 17자 안에 닫힌다.
# 길게 늘어지는 인용은 앞 20자를 다 먹어서 오히려 감점 대상이다.
_ISSUE_LEAD = re.compile(r'^["“]([^"“”]{4,20})["”]')
# 선두 인용구 전체(길이 무관). 아래 _lead_is_named() 가 이 안을 들여다본다.
_LEAD_QUOTE = re.compile(r'^["“]([^"“”]{4,44})["”]')
# 선두 인용이 ..으로 끝나며 여운을 남기는 형태.  "어제도 했다는.."
_TEASE = re.compile(r'^["“][^"“”]{4,20}(?:\.\.|…)["”]?')
_HEIGHT = re.compile(r"(\d{2,3})\s*(?:cm|CM|센치|센티)")
_KG = re.compile(r"(\d{2,3})\s*(?:kg|KG|킬로|키로)")
_QUESTION_END = re.compile(r"(?:\?|까|까요|나요|을까|ㄹ까|일까|는지)\s*$")
# 모순 구조: 지위·스펙과 어긋나는 행동을 한 문장에 붙인 형태.
# "아이돌인데 뚱뚱하다고" / "32kg 뺐는데도 실패" / "평판 1위인데 돌연 사라진"
_CONTRAST = re.compile(r"인데|는데도|은데도|았는데|었는데|고도 ")
_OPEN_END = re.compile(r"(?:\.\.\.|…)\s*$")
# 진부한 마무리. "그래서 뭐?"가 나오는 종결어미·명사로 끝나는 제목.
# .jpg 마감(v13)과 말줄임은 떼고 본다.
_VAGUE_TAIL = re.compile(
    r"(?:" + "|".join(re.escape(w) for w in C.VAGUE_TAILS) + r")"
    r"\s*[.?!]*\s*(?:\.jpg)?\s*$"          # v13 제목의 .jpg 마감은 떼고 본다
)


def _age_gap(head: str) -> bool:
    """앞 20자에 나이 대비가 있는가. "58세인데 20대 피부" 같은 형태.

    2026-08-14 사용자 확정. 이 블로그의 핵심 톤이 "관리를 어떻게 했길래 아직도
    20대처럼 보이지" 인데, 나이를 숫자로 안 치는 규칙(어느 제목에나 붙는
    빈칸이라서) 때문에 이 대비까지 통째로 죽고 있었다.

        올해 52세 여배우의 목주름, 20대와 구분이 안 된다는 반응   ← 20점(반려)

    나이 하나는 빈칸이 맞다. 하지만 둘이 15살 이상 벌어져 마주 놓이면
    그건 빈칸이 아니라 이 블로그가 파는 후킹 그 자체다.
    """
    ages = [int(m.group(1)) for m in _AGE_TOKEN.finditer(head)]
    return len(ages) >= 2 and (max(ages) - min(ages)) >= 15


def _lead_is_named(title: str) -> bool:
    """선두 인용구 안에 관계 인물 실명이 들어 있는가.

    2026-08-13 사용자 확정. 원래 이슈 선행형은 인용구가 20자 이내여야 가점을
    받았다 — 길게 늘어지면 앞 20자를 다 먹는다는 이유였다. 그런데 작품 관계형은
    인용구가 길어도 맨 앞이 상대역 실명이라 후킹이 그대로 산다.

        "이광수에게 오늘 밤 같이 있고 싶다고 말한" 여배우   ← 인용구 22자

    이 규칙이 없을 때 위 제목이 36점이었다. 실명이 앞에 있으면 길이는 안 본다.
    """
    m = _LEAD_QUOTE.match(title)
    if not m:
        return False
    lead = m.group(1)
    return any(n in lead for n in C.MALE_CELEB_NAMES + C.FEMALE_RELATION_NAMES)


def head_specifics(head: str) -> list:
    """앞 20자 안에 있는 '구체 요소'를 센다. validate.py도 이 함수를 그대로 쓴다.

    2026-08-13 사용자 확정. 홈판은 앞 20자만 노출하는데, 그 자리가
    "34세 배우" 같은 덩어리로 채워지면 독자 머릿속에 아무 그림도 안 그려진다.
    나이는 구체 요소로 치지 않는다 — 어느 제목에나 붙일 수 있는 빈칸이라서다.

    ⚠️ 판정 기준은 여기 한 곳에만 둔다. 검증기에 같은 로직을 복사해 두면
       한쪽만 고쳤을 때 "점수는 통과인데 검증기는 반려" 같은 교착이 생긴다.
    """
    found = []
    # 나이 표기를 걷어낸 뒤에도 숫자가 남는가 (금액·연도·순위·kg)
    if _NUM.search(_AGE.sub("", head)):
        found.append("숫자")
    if _age_gap(head):
        found.append("나이반전")
    if any(w in head for w in C.SCENE_WORDS):
        found.append("장면")
    if any(n in head for n in C.MALE_CELEB_NAMES + C.FEMALE_RELATION_NAMES):
        found.append("실명")
    if any(w in head for w in C.DIET_SIGNAL_WORDS):
        found.append("신호어")
    if any(g in head for g in C.GAP_MARKERS):
        found.append("격차")
    if _CONTRAST.search(head):
        found.append("모순")
    return found


def _head(title: str) -> str:
    return title[: C.HEAD_LEN]


def _fully_inside(pattern: re.Pattern, head: str, title: str) -> bool:
    """패턴이 앞 20자 안에서 '온전히' 끝나는지. 잘린 매치는 인정하지 않는다."""
    for m in pattern.finditer(title):
        if m.end() <= len(head):
            return True
    return False


def _bmi(title: str):
    """제목에 키와 몸무게가 함께 병기됐을 때만 BMI를 계산한다."""
    h = _HEIGHT.search(title)
    w = _KG.search(title)
    if not (h and w):
        return None
    cm = int(h.group(1))
    kg = int(w.group(1))
    if cm < 120 or cm > 210 or kg < 25 or kg > 200:
        return None
    return kg / ((cm / 100) ** 2)


def disqualify_reason(title: str):
    """즉시 탈락 사유. 없으면 None."""
    for w in C.EXTREME_WORDS:
        if w in title:
            return f"외모 조롱 표현: {w}"
    for w in C.BANNED_HOOKS:
        if w in title:
            return f"금지선: {w}"
    # 프롬프트 예시를 그대로 베낀 제목은 여기서 죽인다.
    # 프롬프트에 "쓰지 마라"라고 적는 것만으로는 안 지켜진다는 걸 건강비버에서 봤다.
    for w in getattr(C, "CLICHE_OPENERS", ()):
        if w in title:
            return f"상투 문구 복붙: {w}"
    for name in C.CELEB_POOL:
        if name in title:
            return f"여자 실명 노출: {name}"
    return None


def score(title: str):
    """(점수, 사유 목록) 반환. 사유는 titles.json에 그대로 남긴다."""
    title = title.strip()
    reasons = []

    dq = disqualify_reason(title)
    if dq:
        return C.DISQUALIFIED, [f"{dq} → 즉시 탈락"]

    # v13 제목은 .jpg 로 끝난다. 길이를 잴 때는 떼고 센다.
    core = title[:-4].rstrip() if title.endswith(".jpg") else title
    head = _head(title)
    w = C.WEIGHTS
    total = 0

    # 앞 20자 숫자 — 단, 나이는 숫자로 치지 않는다 (2026-08-13 사용자 확정).
    # 개선 전에는 "34세 배우" 한 덩어리만으로 +30을 받았다. 어느 제목에나 붙일 수
    # 있는 값이라 이게 최고 가중치를 먹으면 밋밋한 제목이 그대로 1위로 올라간다.
    # 실제 사고 제목이 이 경로로 확정됐다. 금액·연도·순위·kg 만 인정한다.
    if _NUM.search(_AGE.sub("", head)):
        total += w["head_number"]
        reasons.append(f"앞20자 숫자 +{w['head_number']}")

    if any(s in head for s in C.DIET_SIGNAL_WORDS):
        total += w["head_diet_signal"]
        reasons.append(f"앞20자 다이어트 신호어 +{w['head_diet_signal']}")

    if _fully_inside(_AGE, head, title) or _fully_inside(_JOB, head, title):
        total += w["head_person"]
        reasons.append(f"앞20자 인물 특정 +{w['head_person']}")

    # 앞 20자 구체성 — 홈판에 실제로 보이는 자리에 그림이 그려지는가
    specifics = head_specifics(head)
    if not specifics:
        total += w["head_thin"]
        reasons.append(f"앞20자에 구체 요소 없음(나이·직업만) {w['head_thin']}")
    if "장면" in specifics:
        total += w["head_scene"]
        reasons.append(f"앞20자 장면어 +{w['head_scene']}")
    if "실명" in specifics:
        total += w["head_male_celeb"]
        reasons.append(f"앞20자 관계 실명 +{w['head_male_celeb']}")
    if "모순" in specifics:
        total += w["head_contrast"]
        reasons.append(f"앞20자 모순 구조 +{w['head_contrast']}")
    if "격차" in specifics:
        total += w["head_gap"]
        reasons.append(f"앞20자 정보 격차 +{w['head_gap']}")
    if "나이반전" in specifics:
        total += w["age_gap"]
        reasons.append(f"앞20자 나이 반전 +{w['age_gap']}")

    if _VAGUE_TAIL.search(title):
        total += w["vague_tail"]
        reasons.append(f"진부한 마무리 {w['vague_tail']}")

    if _DURATION.search(title) and _WEIGHT_TOKEN.search(title):
        total += w["duration_form"]
        reasons.append(f"유지 기간형 +{w['duration_form']}")

    if _DROP.search(title):
        total += w["drop_form"]
        reasons.append(f"감량 낙차형 +{w['drop_form']}")

    if _ISSUE_LEAD.match(title) or _lead_is_named(title):
        total += w["issue_lead"]
        reasons.append(f"이슈 선행형(인용 선두) +{w['issue_lead']}")
        if _TEASE.match(title):
            total += w["tease"]
            reasons.append(f"말줄임 여운 +{w['tease']}")
    elif _QUOTE.search(title):
        total += w["quote"]
        reasons.append(f"본인 발언 인용 +{w['quote']}")

    if any(e in title for e in C.LIFE_EVENTS):
        total += w["life_event"]
        reasons.append(f"인생 사건 +{w['life_event']}")

    if any(n in title for n in C.MALE_CELEB_NAMES):
        if _MARRIAGE.search(title):
            total += w["too_obvious"]
            reasons.append(f"남편 실명+혼인어 = 정답 노출 {w['too_obvious']}")
        else:
            total += w["male_celeb"]
            reasons.append(f"관계 남자 연예인 +{w['male_celeb']}")

    if _CONTRAST.search(title):
        total += w["contrast"]
        reasons.append(f"모순 구조(~인데 ~했다) +{w['contrast']}")

    if _OPEN_END.search(title):
        pass  # 말줄임 종결은 단정형도 질문형도 아니다
    elif _QUESTION_END.search(title):
        total += w["question_end"]
        reasons.append(f"질문형 종결 {w['question_end']}")
    else:
        total += w["assertive_end"]
        reasons.append(f"단정형 종결 +{w['assertive_end']}")

    n = min(len(_NUM.findall(title)), C.MAX_COUNTED_NUMBERS)
    if n:
        total += n * w["per_number"]
        reasons.append(f"숫자 {n}개 +{n * w['per_number']}")

    # 인물이 주인공인 블로그다. 사람이 빠진 제목은 "누구 얘긴데?"조차 안 나온다.
    if not any(lab in title for lab in C.PERSON_LABELS):
        total += w["no_person"]
        reasons.append(f"여성 인물 지칭 없음(여배우·여가수·여자 모델 등) {w['no_person']}")

    over = len(core) - C.TITLE_LEN_SOFT_MAX
    if over > 0:
        total -= over
        reasons.append(f"{C.TITLE_LEN_SOFT_MAX}자 초과 {over}자 -{over}")
    if len(core) < C.TITLE_LEN_MIN:
        total += w["too_short"]
        reasons.append(
            f"{len(core)}자 — {C.TITLE_LEN_MIN}자 미만이라 홈판 앞 20자를 다 못 씀 "
            f"{w['too_short']}")

    bmi = _bmi(title)
    if bmi is not None and bmi < C.BMI_FLOOR:
        total += w["underweight"]
        reasons.append(f"저체중 스펙 BMI {bmi:.1f} {w['underweight']}")

    return total, reasons


def pick(candidates):
    """후보 리스트 → [{title, score, reasons}] 내림차순. 1등이 확정 제목."""
    scored = []
    for t in candidates:
        t = (t or "").strip()
        if not t:
            continue
        s, r = score(t)
        scored.append({"title": t, "score": s, "reasons": r})
    scored.sort(key=lambda x: -x["score"])
    return scored


# ── 자체 테스트 ─────────────────────────────────────────────────────────
# 뷰티판 config로 실측한 점수를 그대로 재현하는지 확인한다 (2026-08-05).
KNOWN = [
    ("30년째 같은 피부라는 58세 여배우의 아침 습관", 86),
    # ── 2026-08-13 사고 재현: 밋밋한 제목이 1위로 확정되던 유형 ──────────
    # 실제 확정 제목. 앞 20자에 남는 게 "식단 안 한다"와 "34세"뿐이라
    # 홈판에서 그림이 안 그려지고, "~라는데" 로 끝나 다 아는 소리로 읽힌다.
    # 개선 전에는 이슈 선행형 +22를 그대로 받아 1위로 올라갔다.
    ('"엄격한 식단 안 한다는" 34세 배우, 그런데 피부는 도자기라는데', 24),
    # ── 2026-08-13 사고 2: 인물이 통째로 빠진 v13 제목 10개 ─────────────
    # heat(모델 자평)로 1위를 뽑던 시절 실제로 확정된 제목. 사람도 없고
    # 후킹도 없고 11자라 홈판 노출 면적도 못 쓴다.
    ("부활절 인사 남긴 근황.jpg", -63),
    # 김연경은 관계 실명으로 인정받아 감점이 덜하지만, 인물 지칭이 없고
    # 13자라 여전히 하한 근처도 못 간다.
    ("김연경과 절친이라는 사실.jpg", -17),
    # 숫자는 있지만 여전히 인물이 없고 짧다.
    ("166cm에 47kg이라는 몸무게.jpg", -10),
    # 같은 소재를 인물·모순·길이로 살린 쪽. 키·몸무게 병기는 피했다(저체중 가드).
    ("47kg인데 야식은 끊은 적 없다는 40대 여배우.jpg", 76),
    # 숫자가 하나도 없어도 앞 20자 모순이 살아 있으면 하한(30점)을 넘긴다.
    ("톱스타인데 주말엔 고양이랑 뜨개질만 한다는 여배우.jpg", 48),
    # ── 작품 관계형 (2026-08-13 사용자가 제시한 유형) ────────────────────
    # 상대역 실명이 맨 앞. 인용구가 22자로 길지만 실명이 앞에 있어 후킹이 산다.
    # head_male_celeb 8→20, 실명 선두 인용 인정 전에는 36점이었다.
    ('"이광수에게 오늘 밤 같이 있고 싶다고 말한" 여배우.jpg', 58),
    ('"정우성이 정색하며 사과하라고 했던" 여배우의 그때 그 눈빛.jpg', 58),
    # 여기에 연도까지 얹으면 최상급이 된다.
    ('18년 전 "소지섭이 같이 죽자고 했던" 여배우의 그 장면.jpg', 84),
    # 같은 소재를 앞 20자에 숫자·모순으로 채운 쪽. 54점 차로 확실히 앞선다.
    ("48kg인데 식단표가 없다는 34세 배우, 대신 새벽마다 한강을 뛴다", 90),
    ('"남편과 어제도 키스했다는.." 55세 여배우의 동안 피부 비결', 76),
    # 저체중 스펙 가드는 뷰티판에서도 그대로 살아 있다 (-15).
    ('"165cm 43kg" 유지한다는 62세 여배우의 공항패션', 63),
    ("명품 앰버서더인데 시장 옷 입는다는 40대 여배우", 62),
    # 의혹 제기·실명 노출은 즉시 탈락.
    ("성형 의혹 불거진 40대 여배우의 근황", C.DISQUALIFIED),
    ("전지현 공항패션이 또 화제라는데", C.DISQUALIFIED),
]

if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ok = True
    for title, expected in KNOWN:
        got, reasons = score(title)
        mark = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"{mark} 기대 {expected:>5} / 실제 {got:>5}  {title}")
        for r in reasons:
            print(f"        - {r}")
    print()
    print("전부 일치" if ok else "불일치 있음 — 가중치 확인 필요")
    sys.exit(0 if ok else 1)
