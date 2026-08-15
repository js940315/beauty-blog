"""형식 검증은 프롬프트가 아니라 코드로 한다.

모델은 줄을 못 센다. 체크리스트를 100개 넣어도 "통과했습니다"라고 자기보고만 한다.
여기서 실제로 세고, 위반 목록을 되먹여 재작성시키고, 마지막에 _polish()가 강제 복구한다.

⚠️ 파이썬 strip()은 U+2800(점자 빈칸)을 공백으로 인식하지 않는다.
   빈 줄 판정은 반드시 is_blank()를 쓴다. 직접 strip() 하면 줄 수가 전부 어긋난다.
"""

import re

import config as C

BR = C.BRAILLE
_BULLET = re.compile(r"^\s*(?:[-*•·▪◦]|\d+[.)]|[가-힣][.)])\s+")
_SUBHEAD = re.compile(
    "^" + re.escape(C.SUBHEAD_OPEN) + ".+" + re.escape(C.SUBHEAD_CLOSE) + "$"
)
# 마크다운은 네이버에서 해석되지 않고 기호가 그대로 노출된다
_MARKDOWN = re.compile(r"\*\*|^#{1,6}\s|`")
_HASHTAG = re.compile(r"#[^\s#]+")
# 한글로 풀어쓴 한자어 수치. "육 년" → "6년", "오 센티미터" → "5cm"
# 앞에 한글이 붙은 경우(근육 세포, 수십 년, 교육 센터)는 수사가 아니므로 제외한다.
#   뒤쪽 조사·접미사("육 년을", "백 그램만")까지 걸러내면 정작 잡을 걸 놓친다.
#   앞쪽 검사만으로 오탐은 이미 0이다(실측).
_SPELLED_NUM = re.compile(
    r"(?<![가-힣])(" + "|".join(C.SINO_NUMERALS) + r")\s?("
    + "|".join(C.NUMERAL_UNITS) + r")"
)


# 목록에 없는 신생 매체까지 잡는 꼬리표. 앞에 붙은 글자가 있어야 걸리므로
# "그 뉴스가 퍼졌어요" 같은 일반 표현은 통과한다.
_MEDIA_SUFFIX = re.compile(
    r"[가-힣A-Za-z]{1,6}(?:" + "|".join(C.MEDIA_SUFFIXES) + r")"
)


def _media_hits(text: str) -> list:
    """매체명·출처 표현을 찾는다. 목록은 config 한 곳에서만 늘린다."""
    hits = [t for t in C.MEDIA_TERMS if t in text]
    hits += [p for p in C.SOURCE_PHRASES if p in text]
    hits += [m.group(0) for m in _MEDIA_SUFFIX.finditer(text)]
    return list(dict.fromkeys(hits))


def subhead(text: str) -> str:
    """소제목 한 줄을 만든다. 표시 방식은 config에서만 정한다."""
    return f"{C.SUBHEAD_OPEN}{text}{C.SUBHEAD_CLOSE}"


def subhead_flags(body):
    """줄마다 '이게 소제목인가'를 판정한다. body 순서 그대로 반환.

    큰따옴표로만 이뤄졌다고 다 소제목이 아니다. 본문에서 남의 말을 한 줄로
    인용해도 모양이 똑같다. 실제로 클라우드 루틴이 그렇게 썼다:

        뉴시스와의 인터뷰에서
        "야식 땡길 때 콜라비"     ← 인용이지 소제목이 아니다
        라고 밝혔더라고요.

    구분은 위치로 한다. 소제목은 앞뒤가 빈 줄인 자기 단락이고, 인용은 문장
    한가운데다. 이걸 안 가리면 인용 줄이 길이 검사를 통째로 빠져나간다.
    """
    flags = []
    for i, ln in enumerate(body):
        if is_blank(ln) or not _SUBHEAD.match(visible(ln)):
            flags.append(False)
            continue
        before = all(is_blank(x) for x in body[:i][::-1][:1]) if i else True
        after = all(is_blank(x) for x in body[i + 1:][:1]) if i + 1 < len(body) else True
        flags.append(before and after)
    return flags


def is_blank(line: str) -> bool:
    """빈 줄인가. U+2800을 제거한 뒤에 판정해야 한다 — 이게 그 버그다."""
    return line.replace(BR, "").strip() == ""


def visible(line: str) -> str:
    """U+2800을 걷어낸 실제 글자."""
    return line.replace(BR, "").strip()


def visible_len(line: str) -> int:
    return len(visible(line))


def _strip_trailing_braille(line: str) -> str:
    while line.endswith(BR):
        line = line[: -len(BR)]
    return line.rstrip()


def split_sections(text: str):
    """본문을 (본문줄들, 해시태그줄들, 안내문줄들)로 나눈다."""
    lines = text.replace("\r\n", "\n").split("\n")
    body, tags, notices = [], [], []
    for ln in lines:
        v = visible(ln)
        if v.startswith("#"):
            tags.append(ln)
        elif v.startswith("※") or v == C.AD_DISCLOSURE_TEXT or v == C.HEALTH_DISCLAIMER_TEXT:
            notices.append(ln)
        else:
            body.append(ln)
    return body, tags, notices


def content_lines(body):
    """빈 줄이 아닌 줄."""
    return [ln for ln in body if not is_blank(ln)]


def hook_echo_problems(title: str, body) -> list:
    """제목의 후킹(숫자·인용구)이 도입부에서 회수되는지.

    제목에서 어그로를 걸어놓고 본문이 그 얘기를 안 하면 독자는 낚시로 읽고
    바로 이탈한다. 도입부 = 첫 14개 내용 줄 안에서 회수돼야 한다.
    """
    intro = " ".join(visible(ln) for ln in body if not is_blank(ln))
    intro = " ".join(intro.split())
    head = " ".join(visible(ln) for ln in
                    [l for l in body if not is_blank(l)][:14])
    probs = []
    for num in re.findall(r"\d+(?:\.\d+)?", title):
        if num not in head:
            probs.append(f"제목의 숫자 {num} 를 도입부에서 회수 안 함 (낚시로 읽힘)")
    m = re.match(r'^["“]([^"“”]+)["”]', title)
    if m:
        toks = [t.strip('.,?!…"') for t in m.group(1).split()]
        toks = [t for t in toks if len(t) >= 2 and not t.isdigit()]
        if toks:
            hit = sum(1 for t in toks if t in head)
            if hit * 2 < len(toks):
                probs.append("제목 인용구를 도입부에서 다시 다루지 않음 (낚시로 읽힘)")
    return probs


def validate(text: str, richness: str = "normal", cta: str = None, celeb: str = None,
             title: str = None):
    """위반 목록을 반환한다. 빈 리스트면 통과."""
    problems = []
    body, tags, notices = split_sections(text)
    cl = content_lines(body)

    # 1. 빈 줄은 정확히 ⠀⠀⠀ 여야 한다
    for i, ln in enumerate(body, 1):
        if is_blank(ln) and ln != C.BLANK_LINE:
            problems.append(f"{i}번째 줄: 빈 줄이 점자빈칸 3개가 아님 (현재 {len(ln)}자)")

    # 2. 내용 줄은 U+2800으로 끝나야 한다
    for i, ln in enumerate(body, 1):
        if not is_blank(ln) and not ln.endswith(BR):
            problems.append(f"{i}번째 줄 끝에 점자빈칸 없음: {visible(ln)[:20]}")

    # 3. 한 줄 9~19자 (소제목만 예외. 인용은 예외가 아니다)
    flags = subhead_flags(body)
    for i, ln in enumerate(body):
        if is_blank(ln):
            continue
        if flags[i]:
            continue
        n = visible_len(ln)
        if n < C.LINE_MIN or n > C.LINE_MAX:
            problems.append(f"줄 길이 {n}자 (허용 {C.LINE_MIN}~{C.LINE_MAX}): {visible(ln)[:24]}")

    # 4. 한 단락 최대 3줄
    run = 0
    for ln in body:
        if is_blank(ln):
            run = 0
            continue
        run += 1
        if run > C.PARA_MAX_LINES:
            problems.append(f"단락이 {run}줄 (최대 {C.PARA_MAX_LINES}): {visible(ln)[:20]}")

    # 5. 내용 줄 수
    lo = C.THIN_SOURCE_LINES_MIN if richness == "thin" else C.CONTENT_LINES_MIN
    if len(cl) < lo:
        problems.append(f"내용 줄 {len(cl)}개 — {lo}개 이상 필요")
    if len(cl) > C.CONTENT_LINES_MAX:
        problems.append(f"내용 줄 {len(cl)}개 — {C.CONTENT_LINES_MAX}개 이하여야 함")

    # 6. 분량 — 네이버 글자수세기 공백제외 기준 (본문만, 제목·해시태그 제외)
    #    네이버는 점자 빈칸을 글자로 세므로 점자 개수를 더해야 실측과 맞는다.
    chars = sum(len(re.sub(r"\s", "", visible(ln))) for ln in cl)
    chars += sum(ln.count(BR) for ln in body)
    lo_chars = C.THIN_CHARS_MIN if richness == "thin" else C.BODY_CHARS_MIN
    if chars < lo_chars:
        problems.append(f"네이버기준 {chars}자 — {lo_chars}자 이상이어야 함 (분량 미달)")
    if chars > C.BODY_CHARS_MAX:
        problems.append(f"네이버기준 {chars}자 — {C.BODY_CHARS_MAX}자 이하여야 함 (장황)")

    # 7. 불릿 금지 (네이버는 마크다운을 해석하지 않는다)
    for ln in cl:
        if _BULLET.match(visible(ln)):
            problems.append(f"불릿 사용: {visible(ln)[:20]}")

    # 8. 해시태그 정확히 8개
    tag_text = " ".join(visible(t) for t in tags)
    n_tags = len(_HASHTAG.findall(tag_text))
    if n_tags != C.HASHTAG_COUNT:
        problems.append(f"해시태그 {n_tags}개 — 정확히 {C.HASHTAG_COUNT}개여야 함")
    if celeb:
        for ft in C.FIXED_HASHTAGS:
            if ft not in tag_text:
                problems.append(f"고정 해시태그 누락: {ft}")
        if f"#{celeb}" not in tag_text:
            problems.append(f"인물 해시태그 누락: #{celeb}")
        # 2026-08-14: 하트 CTA 는 폐지됐다. 예전엔 여기서 ❤ 를 강제했다.
        if "❤" in text:
            problems.append("하트 구걸 문구 — 폐지됐다. 행동 지시로 마감해라")

    # 9. 소제목 형식 / 마크다운 노출
    #    네이버는 마크다운을 해석하지 않는다. **가 있으면 화면에 별표가 그대로 보인다.
    for ln in cl:
        v = visible(ln)
        if _MARKDOWN.search(v):
            problems.append(f"마크다운 기호 노출 (네이버는 해석 못 함): {v[:24]}")
    if not any(flags):
        problems.append(f"소제목이 하나도 없음 ({subhead('소제목')} 형식, 앞뒤가 빈 줄이어야 함)")

    # 10. CTA — 코드가 로테이션시킨 문장이 실제로 들어갔는지.
    #     한 줄 상한(19자)보다 CTA가 기니 줄 나눔은 허용하고, 공백을 지운 뒤 비교한다.
    if cta:
        flat = re.sub(r"\s", "", "".join(visible(ln) for ln in cl))
        if re.sub(r"\s", "", cta) not in flat:
            problems.append(f"CTA 문장이 본문에 없음: {cta}")

    # 11. 제목 후킹 회수 — 어그로 걸었으면 도입부에서 다뤄야 한다
    if title:
        problems.extend(hook_echo_problems(title, body))

    # 11-2. 수치는 아라비아 숫자로
    for ln in cl:
        v = visible(ln)
        for m in _SPELLED_NUM.finditer(v):
            problems.append(f"수치를 한글로 풀어씀 (숫자로 쓸 것): {m.group(0)}")

    # 12. 누출 검사
    full = " ".join(visible(ln) for ln in body)
    # 실명 규칙: 제목은 블라인드(titles.py가 잡는다), 본문은 도입부에서 공개한다.
    if celeb:
        if celeb not in full:
            problems.append(f"실명 리빌 없음: 도입부에서 {celeb} 이름을 밝혀야 함")
        for name in C.CELEB_POOL:
            if name != celeb and name in full:
                problems.append(f"다른 여자 연예인 실명 노출: {name}")
    else:
        # 인물 없이 호출된 경우(자체 테스트)는 예전처럼 전부 잡는다
        for name in C.CELEB_POOL:
            if name in full:
                problems.append(f"여자 실명 노출: {name}")
    for term in C.OVERPOLITE_TERMS:
        if term in full:
            problems.append(f"과잉 높임: {term}")
    for term in C.LEAK_TERMS:
        if term in full:
            problems.append(f"지침 용어 누출: {term}")
    for w in C.EXTREME_WORDS:
        if w in full:
            problems.append(f"외모 조롱 표현: {w}")
    for w in C.BANNED_HOOKS:
        if w in full:
            problems.append(f"금지선 소재: {w}")

    # 13. 매체명·출처 표기 (2026-08-13 사용자 확정)
    #     2026-08-13 사고: 본문에 "스포츠조선 인터뷰에서", "헬스조선에서는",
    #     "뉴스컬처 인터뷰에서" 가 줄줄이 박혔다. 원인은 모델이 아니라 예전
    #     프롬프트가 "어디서 한 말인지 밝혀라"라고 시키고 있었던 것이다.
    #     프롬프트를 뒤집었고, 다시 새지 않도록 여기서 실제로 센다.
    for hit in _media_hits(full):
        problems.append(
            f"매체·출처 표기: {hit} — 출처는 한 글자도 쓰지 않는다 "
            "('이렇게 말한 적이 있어요' 처럼 출처 없이 풀 것)")

    # 14. INSTRUCTION.md 강제 항목 (2026-08-14) — v13 검증기와 같은 기준
    for p in C.BANNED_PHRASES:
        if p in full:
            problems.append(f"금지 문구: '{p}' — 상투구다. 다른 말로 바꿔 써라")

    voices = sum(full.count(p) for p in C.SELF_VOICE_PATTERNS)
    if voices > C.SELF_VOICE_MAX:
        problems.append(f"화자 소감 {voices}회 (최대 {C.SELF_VOICE_MAX}) — "
                        "소감 자리에 사실 한 조각을 넣어라")

    bridges = [b for b in C.BRIDGE_POOL if b in full]
    if len(bridges) < C.BRIDGE_MIN:
        problems.append(f"브릿지 {len(bridges)}개 (최소 {C.BRIDGE_MIN}) — "
                        f"소제목 사이에 다음 궁금증을 만드는 한 줄을 넣어라")

    numbered = sum(1 for ln in cl if re.match(r"^\d+\s+\S", visible(ln)))
    if numbered < 3 and "❌" not in full and "⭕" not in full:
        problems.append("스크롤 유인 블록 없음 — 번호 리스트 3줄 또는 ❌/⭕ 대비 블록")

    return problems


# ── 안내문 (제휴 표시 · 건강 면책) ──────────────────────────────────────

def notice_lines():
    out = []
    if C.AD_DISCLOSURE:
        out.append("※ " + C.AD_DISCLOSURE_TEXT)
    if C.HEALTH_DISCLAIMER:
        out.append("※ " + C.HEALTH_DISCLAIMER_TEXT)
    return out


def ensure_notices(text: str) -> str:
    """제휴 표시·건강 면책을 해시태그 앞에 넣는다. 이미 있으면 그대로 둔다.

    ⚠️ 예전에는 polish() 안에서만 붙였다. 그러면 본문이 한 번에 검증을 통과한
       '좋은 날'에만 면책 문구가 빠지는 사고가 난다. 그래서 밖으로 뺐다.
    """
    wanted = notice_lines()
    if not wanted:
        return text

    lines = text.replace("\r\n", "\n").split("\n")
    have = {visible(ln) for ln in lines}
    missing = [n for n in wanted if n not in have]
    if not missing:
        return text

    # 해시태그 블록 앞에 끼워 넣는다. 해시태그가 없으면 맨 끝.
    cut = len(lines)
    for i, ln in enumerate(lines):
        if visible(ln).startswith("#"):
            cut = i
            break

    head = lines[:cut]
    while head and is_blank(head[-1]):
        head.pop()
    tail = lines[cut:]

    block = [C.BLANK_LINE] + missing
    if tail:
        block.append(C.BLANK_LINE)
    return "\n".join(head + block + tail)


# ── 기계적 강제 복구 ────────────────────────────────────────────────────

def _wrap(text: str, lo: int, hi: int):
    """긴 줄을 어절 경계에서 hi자 이하로 쪼갠다."""
    words = text.split()
    out, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip()
        if len(cand) <= hi:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    # 너무 짧은 꼬리는 앞 줄에 붙인다
    merged = []
    for seg in out:
        if merged and len(seg) < lo and len(merged[-1]) + len(seg) + 1 <= hi:
            merged[-1] = merged[-1] + " " + seg
        else:
            merged.append(seg)
    return merged or [text]


def polish(text: str) -> str:
    """마지막 방어선. 재작성으로도 안 맞으면 여기서 기계적으로 맞춘다."""
    body, tags, notices = split_sections(text)

    fixed = []
    run = 0
    for ln in body:
        if is_blank(ln):
            if fixed and fixed[-1] == C.BLANK_LINE:
                continue  # 연속 빈 줄 제거
            fixed.append(C.BLANK_LINE)
            run = 0
            continue

        v = _strip_trailing_braille(ln)
        v = _BULLET.sub("", v).strip()
        # 옛 형식 **"소제목"** 과 남은 마크다운 기호를 걷어낸다.
        # 네이버에 그대로 노출되므로 살려둘 이유가 없다.
        if v.startswith("**") and v.endswith("**") and len(v) > 4:
            v = v[2:-2].strip()
            if not _SUBHEAD.match(v):
                v = subhead(v.strip(C.SUBHEAD_OPEN + C.SUBHEAD_CLOSE))
        v = v.replace("**", "").replace("`", "").strip()
        if not v:
            continue

        if _SUBHEAD.match(v):
            if fixed and fixed[-1] != C.BLANK_LINE:
                fixed.append(C.BLANK_LINE)
            fixed.append(v + BR)
            fixed.append(C.BLANK_LINE)
            run = 0
            continue

        for seg in _wrap(v, C.LINE_MIN, C.LINE_MAX):
            if run >= C.PARA_MAX_LINES:
                fixed.append(C.BLANK_LINE)
                run = 0
            fixed.append(seg + BR)
            run += 1

    while fixed and fixed[0] == C.BLANK_LINE:
        fixed.pop(0)
    while fixed and fixed[-1] == C.BLANK_LINE:
        fixed.pop()

    out = list(fixed)

    # 해시태그 정확히 8개
    all_tags = _HASHTAG.findall(" ".join(visible(t) for t in tags))
    all_tags = list(dict.fromkeys(all_tags))[: C.HASHTAG_COUNT]
    if all_tags:
        out.append(C.BLANK_LINE)
        out.extend(all_tags)

    return ensure_notices("\n".join(out))


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    # 일부러 망가뜨린 입력 — 검출과 복구가 되는지 본다
    broken = "\n".join([
        "이것은 아주 길어서 열아홉 자를 확실하게 넘어가는 문장입니다",  # 길이 위반 + 점자 없음
        "",                                                            # 빈 줄 형식 위반
        "- 불릿을 쓰면 안 된다",                                        # 불릿
        "짧다",                                                         # 길이 미달
        '**"옛 소제목 형식"**',                                          # 마크다운 노출
        "#태그1 #태그2",                                                # 해시태그 부족
    ])
    print("── 검증 (망가진 입력) ──")
    for p in validate(broken):
        print("  ·", p)

    print()
    print("── polish() 결과 ──")
    fixed = polish(broken)
    for ln in fixed.split("\n"):
        print(f"  [{visible_len(ln):>2}] {ln}")

    print()
    print("── 복구 후 재검증 (줄 수·태그 수는 원문이 짧아 남는 게 정상) ──")
    for p in validate(fixed):
        print("  ·", p)

    # polish를 안 거치는 '한 번에 통과' 경로에서도 안내문이 붙어야 한다
    print()
    print("── ensure_notices (검증 1회 통과 경로) ──")
    clean = "\n".join([
        "오늘은 옷장 이야기입니다" + BR,
        C.BLANK_LINE,
        "#공항패션", "#동안피부",
    ])
    got = ensure_notices(clean)
    for ln in got.split("\n"):
        print("  ", ln)
    if C.AD_DISCLOSURE or C.HEALTH_DISCLAIMER:
        assert "※" in got, "안내문 누락"
        assert ensure_notices(got) == got, "안내문이 두 번 붙음"
        print("  → 안내문 삽입 OK / 중복 없음 OK")
    else:
        assert got == clean, "안내문이 꺼져 있는데 뭔가 붙음"
        print("  → 안내문 OFF (config) — 아무것도 안 붙음 OK")
