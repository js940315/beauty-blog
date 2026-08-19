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
# 인용구 안에 "A는 B, C는 D" 로 조건을 두 개 쌓은 형태 (2026-08-19)
_STACK = re.compile(
    r'["“][^"”]*[가-힣]{2,8}[은는][^"”,]{2,24},\s*'
    r'[가-힣]{2,8}[은는][^"”]*["”]')
_TEASE = re.compile(r'^["“][^"“”]{4,20}(?:\.\.|…)["”]?')
_HEIGHT = re.compile(r"(\d{2,3})\s*(?:cm|CM|센치|센티)")
_KG = re.compile(r"(\d{2,3})\s*(?:kg|KG|킬로|키로)")
_QUESTION_END = re.compile(r"(?:\?|까|까요|나요|을까|ㄹ까|일까|는지)\s*$")
# 모순 구조: 지위·스펙과 어긋나는 행동을 한 문장에 붙인 형태.
# "아이돌인데 뚱뚱하다고" / "32kg 뺐는데도 실패" / "평판 1위인데 돌연 사라진"
# 2026-08-18: 맨 "~는데" 가 빠져 있었다. "뱃살이 없는데 라면을 먹는다",
# "샤넬백 들고 모자는 쿠팡인데" 같은 명백한 상식 파괴가 화력 0으로 폐기됐다.
# 인데·았는데도 흔한 연결어미인데 이미 넣어 뒀으니 기준을 맞춘다.
_CONTRAST = re.compile(r"인데|는데|은데|았는데|었는데|고도 ")
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
    # 2026-08-18: 강조 구문도 구체 요소다. 이게 빠져 있어서 사용자가 직접 예로 든
    # "얼마나 예뻤으면 감독이 대본을 고쳤다는…" 이 **-14 를 맞고 22점**이 됐다.
    # 앞 20자가 강조 구문으로 채워진 건 빈 자리가 아니라 후킹 그 자체다.
    if _INTENSITY.search(head):
        found.append("강조")
    return found


def _head(title: str) -> str:
    return title[: C.HEAD_LEN]


# ── 문형 판정 ───────────────────────────────────────────────────────────
# 큰따옴표로 묶인 인용구. 홑따옴표·어포스트로피는 세지 않는다
# (_QUOTE 는 점수용이라 그것들까지 보지만, 문형은 실제 발언 인용만 본다).
_DQUOTE = re.compile(r'["“][^"“”]{2,}["”]')


def form_of(title: str) -> str:
    """제목의 문형. INSTRUCTION.md §6 의 5종 + "기타".

    2026-08-17 사용자 확정. 문형 하루 상한(config.TITLE_FORM_DAILY_MAX)을
    코드가 세려면 먼저 문형을 코드가 가려야 한다. 슬롯이 배정한 문형
    (SLOT_TITLE_TYPES)은 B 지시서에 적어주는 지시일 뿐이고, 실제로 뽑힌
    제목이 그 문형인지는 아무도 확인하지 않고 있었다.

    ⚠️ 판정 순서가 결과를 바꾼다. 제목 하나가 여러 표지를 동시에 갖기 때문이다
       ("18년 전 \"소지섭이 같이 죽자고 했던\" 여배우" = 숫자 + 인용 + 실명).
       상한이 걸린 문형은 "인용폭로" 하나이므로, 정확해야 하는 건 앞 두 줄뿐이다:

       ① 제3자반응 먼저.  앞 20자에 관계 인물 실명이 있으면 인용구가 있어도
          이쪽이다. 이게 사용자가 3편 배정한 "작품 관계형"이라 상한에 걸리면
          안 된다 (B_titles.md §77).
       ② 그다음 인용폭로.  큰따옴표 인용구가 어디에 있든 인용형으로 센다.
          선두 인용만 세면 모델이 인용구를 뒤로 옮겨 상한을 피해 간다 —
          점수는 issue_lead(+22)가 아니라 quote(+8)로 줄지만 제목의 모양은
          여전히 "발언 따오기" 한 가지다. 독자가 보는 건 모양이다.

       나머지 순서는 상한과 무관하니 §6 표 순서를 따랐다.
    """
    title = (title or "").strip()
    if not title:
        return "기타"
    head = _head(title)

    if any(n in head for n in C.MALE_CELEB_NAMES + C.FEMALE_RELATION_NAMES):
        return "제3자반응"
    if _DQUOTE.search(title):
        return "인용폭로"
    if any(w in title for w in C.TITLE_FORM_NEGATION):
        return "부정금지"
    if any(w in title for w in C.TITLE_FORM_LOSS):
        return "손해경고"
    spec = head_specifics(head)
    if "숫자" in spec or "나이반전" in spec:
        return "수치충돌"
    if "모순" in spec:
        return "모순"
    return "기타"


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


# 2026-08-18: 상식 파괴를 문법 역접(~인데)만으로 잡으면 절반을 놓친다.
# "명품 다 팔고 에코백 택했다" 처럼 **구체 명사 두 개를 맞바꾼** 형태가
# 사용자가 말한 "머릿속에 그림이 그려지는" 반전이다. 역접 어미가 없어 기존
# _CONTRAST 에 안 걸렸고, 그래서 그 제목이 80점에 머물렀다.
_SWAP = re.compile(
    r"(?:팔고|버리고|접고|끊고|치우고|털고|내려놓고|포기하고)\s*\S+"
    r"|대신\s*\S+|말고\s*\S+|아니라(?!고)\s*\S+|에서\s*\S+(?:으?로)\s*(?:바꾸|갈아|옮)"
)


# ── 2026-08-18 사용자 지시: "도파민 터지게. 얼마나 예뻤으면~~ 같은 강조표현도 좋음" ──
# 강조·과장 구문은 그 자체로 궁금증을 만든다. "얼마나 예뻤으면 감독이 대본을 고쳤을까"
# 는 사실을 하나도 안 바꾸고 화력만 올린다. 기존 채점기에 이 항목이 통째로 없었다.
_INTENSITY = re.compile(
    r"얼마나\s*\S{1,12}(?:으면|었으면|했으면|길래)"
    r"|오죽(?:했으면|하면)"
    r"|도대체\s*얼마나|대체\s*얼마나"
    r"|\S+까지\s*\S{1,6}(?:다|다는|았다|었다|해|했다|버렸|말았)"
    r"|심지어|무려|단\s*\d|겨우\s*\d|\S+조차\s*안"
    r"|이 정도면|이쯤 되면"
)

# "진라면 금지" — 화력 요소가 하나도 없는 제목은 홈판에서 안 눌린다.
# 사용자: "밋밋한 아무 영양가 없는 제목이 내 홈판 제목으로 온다는 걸 상상하지 마.
#          진라면 맵기로는 경쟁력 없어. 최소 불닭볶음면 이상이어야 한다고."
# 아래 넷 중 하나도 없으면 그 제목은 사실 나열이지 후킹이 아니다.
def _has_firepower(title: str) -> bool:
    return bool(_CONTRAST.search(title)      # 모순  "48세인데 20대 쇄골"
                or _SWAP.search(title)       # 대비  "명품 다 팔고 에코백"
                or _INTENSITY.search(title)  # 강조  "얼마나 예뻤으면"
                or _age_gap(_head(title)))   # 나이 반전


# 지시어로 인물을 가리는 형태를 패턴으로 잡는다 (목록 방식은 수식어가 끼면 뚫린다).
#   그 배우 / 그 톱배우 / 이 여배우 / 해당 인기 가수 / 그 국민 여동생
_PRONOUN_PAT = re.compile(
    r"(?:그|이|저|해당)\s*"
    r"(?:[가-힣]{1,4}\s*){0,2}"
    r"(?:여?배우|여?가수|모델|아나운서|방송인|개그우먼|아이돌|셀럽|연예인|스타|톱스타)"
)


# 경력·기간을 숫자로 밝힌 표현. 이것도 "누군데?" 를 만드는 수식어다.
_TENURE = re.compile(r"\d+\s*년\s*(?:차|째|만에)|데뷔\s*\d+|\d+\s*기|\d+\s*년\s*활동")


def bare_job(title: str) -> bool:
    """직업어를 맨몸으로 썼는가 (수식어가 하나도 없는가).

    2026-08-18 사용자 확정, 두 블로그 동일 규칙:
    "앞에 충분히 궁금해 할 만한 포인트가 들어가면서 수식어가 붙으면 사용 가능."

    이름을 못 쓰니 여배우·톱스타·아이돌·모델 로 가리는 건 정상이고 필요하다.
    금지가 아니다. 다만 맨몸으로 쓰면 특정성이 0이라 아무 그림도 안 생긴다.
        40대 여배우        누구에게나 붙는다
        원조 얼짱 여배우    "그게 누군데?" 가 생긴다
    """
    if not _JOB.search(title):
        return False
    if any(e in title for e in getattr(C, "BLIND_EPITHETS", ())):
        return False
    # 숫자형 수식어도 수식어다 — "19년 차 배우", "데뷔 27년 가수", "3년 만에".
    # 목록에만 의존하면 "19년 차 배우, 명품 다 팔고 에코백"(좋은 제목)이
    # 맨몸 취급을 받아 -15 를 맞는다(2026-08-18 실측: 82 -> 67 로 하한 미달).
    return not _TENURE.search(title)


def _name_hit(title: str, name: str) -> bool:
    """실명이 **독립된 이름으로** 들어 있는가.

    2026-08-18: 단순 부분문자열 검사라 `윤유선` 이 `유선`(다른 배우)으로 잡혀
    멀쩡한 제목이 폐기됐다. 앞뒤에 한글이 이어지면 다른 이름의 일부다.
    """
    if not name or len(name) < 2:
        return False
    for i in range(len(title) - len(name) + 1):
        if title[i:i + len(name)] != name:
            continue
        before = title[i - 1] if i > 0 else ""
        after = title[i + len(name)] if i + len(name) < len(title) else ""
        if "가" <= before <= "힣" or "가" <= after <= "힣":
            continue                      # 더 긴 이름의 일부다
        return True
    return False


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
        if _name_hit(title, name):
            return f"여자 실명 노출: {name}"

    # ── 2026-08-18 사용자 지시로 신설 ──────────────────────────────────
    # "추상적인 표현은 절대 사용하지 말고 구체적인 키워드식의 제목을 만들라고.
    #  딱 보면 머릿속에 그림이 그려지도록."
    # 감점이 아니라 폐기다. 감점으로 두면 다른 항목으로 벌충해서 살아남는다 —
    # 실측 0818: "오디션 4번 거친 그 배우, 피부까지 완벽했다" 가 80점으로 확정됐다.
    for w in getattr(C, "PRONOUN_SUBJECTS", ()):
        if w in title:
            return f"인물을 지시대명사로 가림(특정성 0): {w}"
    # 목록만으로는 샌다. 2026-08-18 실측: "그 **톱배우**" 가 목록에 없어서
    # 99점짜리로 통과했다. 수식어가 하나 끼면 목록 방식은 무한히 뚫린다.
    # 지시어 + (수식어 0~2개) + 직업어 를 패턴으로 잡는다.
    m = _PRONOUN_PAT.search(title)
    if m:
        return f"인물을 지시대명사로 가림(특정성 0): {m.group(0)}"
    for w in getattr(C, "ABSTRACT_WORDS", ()):
        if w in title:
            return f"추상 표현(그림이 안 그려짐): {w}"

    if not _has_firepower(title):
        return ("화력 0 — 반전·대비·강조·나이반전이 하나도 없다 "
                "(사실 나열은 홈판에서 안 눌린다)")

    # 앞 20자 구체성은 하드 폐기가 아니라 점수로 다룬다(head_thin).
    # 한 번 하드 폐기로 넣었다가 "19년 차 배우, 명품 다 팔고 에코백 택했다" 처럼
    # **좋은 제목까지 죽였다** — 반전(명품→에코백)이 21자 뒤에 있었을 뿐이다.
    # 잘라내는 기준은 뭉툭할수록 좋은 걸 같이 버린다.
    return None


def below_floor(ranked):
    """최고점이 하한선 미만이면 전부 폐기하고 후보를 다시 뽑아야 한다.

    2026-08-18 신설. 이 저장소에는 하한선이 없어서 58점짜리도 그대로 확정됐다.
    건강비버는 MIN_SCORE 60 + 재생성 3회로 약한 제목을 걸러낸다(실측: 그 덕에
    슬롯 하나가 '재배정 3회 소진'으로 비워졌고, 나머지 9건 품질이 올라갔다).
    빈 슬롯이 밋밋한 제목보다 낫다 — 밋밋한 제목은 홈판에서 어차피 안 눌린다.
    """
    if not ranked:
        return True
    return ranked[0]["score"] < getattr(C, "MIN_SCORE", 0)


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

    # 주제어(패션·피부·명품…)가 앞에 오면 감점이다 — 훅 자리를 명분이 먹었다는 뜻.
    # 단 **반전의 재료로 쓰였으면 감점하지 않는다.** "명품 다 팔고 에코백" 의 명품은
    # 필러가 아니라 그림을 만드는 구체 명사다. 구분 없이 깎았더니 사용자가 원한
    # 형태가 52점, 밋밋한 제목이 83점으로 뒤집혔다(2026-08-18 실측).
    if any(x in head for x in C.DIET_SIGNAL_WORDS):
        if _SWAP.search(title) or _CONTRAST.search(title):
            reasons.append("앞20자 주제어 — 반전 재료라 감점 면제")
        else:
            total += w["head_diet_signal"]
            reasons.append(f"앞20자가 주제어로 채워짐 {w['head_diet_signal']}")

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

    # 직업어만 달랑 쓴 경우 (사용자 확정: 수식어가 붙으면 써도 된다)
    if bare_job(title):
        total += w["bare_job"]
        reasons.append(f"직업어에 수식어가 없다(특정성 0) {w['bare_job']}")

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

    # 인용구 안에 조건 두 개를 쉼표로 병렬 배치한 제목 (2026-08-19).
    # 실적 5위 글(조회 3.3만) 제목이 이 형태다:
    #   "아버지는 멘사 회장, 남편은 서울대 의사에요.."
    # 조건 하나짜리보다 "이게 다 한 사람이라고?" 가 훨씬 세게 걸린다.
    if _STACK.search(title):
        total += w["quote_stack"]
        reasons.append(f"인용구 안 조건 2개 병렬 +{w['quote_stack']}")

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

    # 구체 명사 맞바꾸기 = 사용자가 말한 "상식 파괴". 역접 어미가 없어도 반전이다.
    # "명품 다 팔고 에코백 택했다" / "관리 대신 물만" / "샐러드 말고 삼겹살"
    if _SWAP.search(title):
        total += w["swap"]
        reasons.append(f"구체 대비(A 버리고 B) +{w['swap']}")

    if _INTENSITY.search(title):
        total += w["intensity"]
        reasons.append(f"강조·과장 구문 +{w['intensity']}")
        if _INTENSITY.search(head):
            total += w["head_intensity"]
            reasons.append(f"강조 구문이 앞{C.HEAD_LEN}자 안 +{w['head_intensity']}")

    # 장면어가 제목 어디에든 있으면 소액 가점. "머릿속에 그림"이 핵심 요구라
    # 앞 20자 밖이어도 값을 쳐준다(앞에 있으면 head_scene 로 이미 크게 받는다).
    if any(x in title for x in C.SCENE_WORDS):
        total += w["scene_any"]
        reasons.append(f"장면어 포함 +{w['scene_any']}")

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
    ("30년째 같은 피부라는 58세 여배우의 아침 습관", C.DISQUALIFIED),
    # ── 2026-08-13 사고 재현: 밋밋한 제목이 1위로 확정되던 유형 ──────────
    # 실제 확정 제목. 앞 20자에 남는 게 "식단 안 한다"와 "34세"뿐이라
    # 홈판에서 그림이 안 그려지고, "~라는데" 로 끝나 다 아는 소리로 읽힌다.
    # 개선 전에는 이슈 선행형 +22를 그대로 받아 1위로 올라갔다.
    ('"엄격한 식단 안 한다는" 34세 배우, 그런데 피부는 도자기라는데', 37),
    # ── 2026-08-13 사고 2: 인물이 통째로 빠진 v13 제목 10개 ─────────────
    # heat(모델 자평)로 1위를 뽑던 시절 실제로 확정된 제목. 사람도 없고
    # 후킹도 없고 11자라 홈판 노출 면적도 못 쓴다.
    ("부활절 인사 남긴 근황.jpg", C.DISQUALIFIED),
    # 김연경은 관계 실명으로 인정받아 감점이 덜하지만, 인물 지칭이 없고
    # 13자라 여전히 하한 근처도 못 간다.
    ("김연경과 절친이라는 사실.jpg", C.DISQUALIFIED),
    # 숫자는 있지만 여전히 인물이 없고 짧다.
    ("166cm에 47kg이라는 몸무게.jpg", C.DISQUALIFIED),
    # 같은 소재를 인물·모순·길이로 살린 쪽. 키·몸무게 병기는 피했다(저체중 가드).
    ("47kg인데 야식은 끊은 적 없다는 40대 여배우.jpg", 72),
    # 숫자가 하나도 없어도 앞 20자 모순이 살아 있으면 하한(30점)을 넘긴다.
    ("톱스타인데 주말엔 고양이랑 뜨개질만 한다는 여배우.jpg", 77),
    # ── 작품 관계형 (2026-08-13 사용자가 제시한 유형) ────────────────────
    # 상대역 실명이 맨 앞. 인용구가 22자로 길지만 실명이 앞에 있어 후킹이 산다.
    # head_male_celeb 8→20, 실명 선두 인용 인정 전에는 36점이었다.
    ('"이광수에게 오늘 밤 같이 있고 싶다고 말한" 여배우.jpg', C.DISQUALIFIED),
    ('"정우성이 정색하며 사과하라고 했던" 여배우의 그때 그 눈빛.jpg', C.DISQUALIFIED),
    # 여기에 연도까지 얹으면 최상급이 된다.
    ('18년 전 "소지섭이 같이 죽자고 했던" 여배우의 그 장면.jpg', C.DISQUALIFIED),
    # 같은 소재를 앞 20자에 숫자·모순으로 채운 쪽. 54점 차로 확실히 앞선다.
    ("48kg인데 식단표가 없다는 34세 배우, 대신 새벽마다 한강을 뛴다", 126),
    ('"남편과 어제도 키스했다는.." 55세 여배우의 동안 피부 비결', C.DISQUALIFIED),
    # 저체중 스펙 가드는 뷰티판에서도 그대로 살아 있다 (-15).
    ('"165cm 43kg" 유지한다는 62세 여배우의 공항패션', C.DISQUALIFIED),
    ("명품 앰버서더인데 시장 옷 입는다는 40대 여배우", 88),
    # 의혹 제기·실명 노출은 즉시 탈락.
    ("성형 의혹 불거진 40대 여배우의 근황", C.DISQUALIFIED),
    ("전지현 공항패션이 또 화제라는데", C.DISQUALIFIED),
]


# ── 문형 판정 자체 테스트 ───────────────────────────────────────────────
# 2026-08-17 실제 확정본 9편이 표본이다. 사용자가 센 값은 "9편 중 6편이 인용문"
# 이고, 그 6편은 **큰따옴표로 시작하는** 제목이다. form_of() 는 그보다 넓게
# 8편을 인용폭로로 센다 — 인용구를 뒤로 민 2편(슬롯 2·8)까지 포함하기 때문이다.
# 이건 판정 오류가 아니라 의도다. 좁게 세면 모델이 인용구를 한 어절 뒤로
# 옮기는 것만으로 상한을 피해 가고, 독자가 보는 모양은 그대로 남는다.
# 아래 테스트는 두 숫자를 다 박아둔다 — 나중에 어느 쪽이 변해도 드러나게.
FORMS = [
    # 0817 슬롯 2~10. 이 중 인용폭로가 6편이어야 한다.
    ('3가지 운동만 지킨다는 배우, "노력은 해야죠"라며', "인용폭로"),
    ('"행복했다"는 여배우, 20년 만에 만난 몽정기 그 얼굴들', "인용폭로"),
    ('"1일 1식이 비결"이라던 배우, 20년 전 바지가 아직도', "인용폭로"),
    ('"얼굴팩만 8종류"라던 여배우, 냉장고엔 30장', "인용폭로"),
    ('"피부는 정원과 같다"던 여배우, 52세 관리법', "인용폭로"),
    ('"상처 열기 힘들었다"던 방송인, 15년만의 화해', "인용폭로"),
    # 인용구가 뒤에 있어도 인용형이다. 앞으로 빼든 뒤로 밀든 모양은 같다.
    ('8개 자격증 땄다는 여가수, "운동하면 힘 난다"', "인용폭로"),
    # 상대역 실명이 앞 20자에 있으면 제3자반응(=작품 관계형). 상한 대상이 아니다.
    ("이동욱과 5년 만에 재회한 여배우, 그때 그 커플", "제3자반응"),
    ('"값진 시간"이라던 여배우, 그 이유는 그 작품', "인용폭로"),
    # ── 실명 선두 인용은 인용형으로 세지 않는다 (사용자가 3편 배정한 유형) ──
    ('"이광수에게 오늘 밤 같이 있고 싶다고 말한" 여배우', "제3자반응"),
    ('18년 전 "소지섭이 같이 죽자고 했던" 여배우의 그 장면', "제3자반응"),
    # ── 0816 은 반대로 하루가 전부 이 모양이었다 ──────────────────────────
    ("50대 배우인데 30년 전 옷장이 그대로라는 그 이유", "수치충돌"),
    ("58세인데 900샷 맞는다는 여배우, 연 1회 꼬박꼬박", "수치충돌"),
    # 숫자가 없어도 모순이 살아 있으면 모순형
    ("톱스타인데 주말엔 고양이랑 뜨개질만 한다는 여배우", "모순"),
    # §6 표의 나머지 두 문형
    ("세안 후 이건 절대 안 쓴다는 여배우, 대신 손바닥으로", "부정금지"),
    ("이거 모르고 하면 피부만 늙는다는 여배우의 아침 습관", "손해경고"),
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
    print("── 문형 판정 ──")
    for title, expected in FORMS:
        got = form_of(title)
        mark = "OK " if got == expected else "FAIL"
        if got != expected:
            ok = False
        print(f"{mark} 기대 {expected:<6} / 실제 {got:<6}  {title}")

    # 0817 실측 재현. FORMS 앞 9개가 그날 확정본이다.
    day = [t for t, _ in FORMS[:9]]
    lead = sum(1 for t in day if t.lstrip().startswith(('"', "“")))
    quoted = sum(1 for t in day if form_of(t) == "인용폭로")
    for label, got, want in (("선두 인용(사용자가 센 값)", lead, 6),
                             ("form_of 인용폭로", quoted, 8)):
        mark = "OK " if got == want else "FAIL"
        if got != want:
            ok = False
        print(f"\n{mark} 0817 재현 — {label}: 9편 중 {got}편 (기대 {want}편)")
    print("   상한 3편이면 그날 인용형은 3편에서 끊긴다. 남은 6편은 다른 문형으로 간다.")

    print()
    print("전부 일치" if ok else "불일치 있음 — 가중치·문형 표지 확인 필요")
    sys.exit(0 if ok else 1)
