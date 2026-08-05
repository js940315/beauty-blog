# -*- coding: utf-8 -*-
"""v13 검증기 — 세는 일은 전부 여기서 한다.

설계 원칙 (설계서 8절): 모델에게 시키던 검증·기억·랜덤을 전부 코드로 이관.
검증 실패 시 전체 재작성이 아니라 errs 목록을 그대로 모델에 되먹인다.

⚠️ 기존 validator.py(일일 자동 파이프라인용)와 별개다. v13은 자체 규격
   (제목 .jpg 마감, 공백·점자 제외 850~1000자, 볼드 소제목)을 쓴다.
"""

import json
import re

U2800 = "⠀"

# 알파벳 허용 목록. 팩트시트의 영문 고유명은 load 시 자동 추가된다.
ALPHA_ALLOW = {"jpg", "kg", "cm", "SM", "SK", "JYP", "YG", "CJ", "LG", "MBC",
               "KBS", "SBS", "tvN", "JTBC", "MZ"}

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
    return errs


# ── 검증 2: 제목 ───────────────────────────────────────────────────────

def validate_titles(titles: list, factsheet: dict) -> list:
    errs = []
    name = (factsheet.get("person") or {}).get("name", "")
    allow = alpha_allow_from(factsheet)

    if len(titles) != 10:
        errs.append(f"제목 {len(titles)}개 — 10개 필요")

    firsts, liz_endings, combos, angles = [], [], [], []
    for t in titles:
        s = t.get("title", "")
        no = t.get("no", "?")
        if not s.endswith(".jpg"):
            errs.append(f"{no}번 .jpg 마감 누락")
        if name and name in s:
            errs.append(f"{no}번 본명 노출: {name}")
        for w in re.findall(r"[A-Za-z]+", s[:-4] if s.endswith(".jpg") else s):
            if w not in allow:
                errs.append(f"{no}번 알파벳 혼입: {w}")
        parts = s.split()
        firsts.append(parts[0] if parts else "")
        m = re.search(r"(\d+세\s*\S+)", s)
        if m:
            combos.append(m.group(1))
        if re.search(r"리즈\s?시절\.jpg$", s):
            liz_endings.append(no)
        if t.get("heat", 0) < 7:
            errs.append(f"{no}번 화력 {t.get('heat')} — 7 미만 재생성")
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


def validate_body(body: str, factsheet: dict, state: dict) -> list:
    errs = []
    person = factsheet.get("person") or {}
    job = person.get("job", "")
    allow = alpha_allow_from(factsheet)

    # 글자수 (공백·점자빈칸 제외). 2026-08-05 사용자 조정: 850~1000 → 780~900.
    # 목표는 820자 안팎 — 끝부분이 길어지는 것보다 짧고 밀도 있게.
    n = len(re.sub(r"[\s⠀]", "", body))
    if not (780 <= n <= 900):
        hint = ("부족: 12줄 미만 챕터에 3줄 문단 추가" if n < 780
                else "초과: 마지막 챕터·마무리에서 곁가지 문단 삭제")
        errs.append(f"글자수 {n}자 (허용 780~900) — {hint}")

    lines = body.split("\n")

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
    if "**" in body:
        errs.append("소제목에 별표(**) 사용 — 네이버에 그대로 노출된다. 큰따옴표만 쓸 것")

    # 큰따옴표 단독 줄 = 도입 인용구 1 + 소제목 4, 딱 다섯이어야 한다
    quote_lines = [l for l in lines
                   if _is_quote_only(l.replace(U2800, "").strip())]
    if len(quote_lines) != 5:
        errs.append(f"큰따옴표 단독 줄 {len(quote_lines)}개 — "
                    "도입 인용구 1 + 소제목 4 = 5개여야 함")

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

    # 마감·CTA 중복 (state 대조)
    for old in state.get("recent_endings", []):
        if old and old in body:
            errs.append(f"최근 글과 마감 문장 중복: {old[:20]}…")
    return errs


# ── 자체 테스트 ─────────────────────────────────────────────────────────

def _selftest():
    fs = {"person": {"name": "홍길동", "gender": "여", "job": "무용가"},
          "quotes": [{"speaker": "홍길동", "text": "말", "context": ""}],
          "hot_materials": [{"material": "x", "why_hot": "낙차"}]}

    # 검증 1: 정상 통과 / 직업 빠지면 잡힘
    assert validate_factsheet(fs) == []
    bad_fs = json.loads(json.dumps(fs))
    bad_fs["person"]["job"] = ""
    assert any("person.job" in e for e in validate_factsheet(bad_fs))

    # 검증 2: 본명 노출·jpg 누락·화력 미달·후킹 1개가 전부 잡히는지
    # "N세 직업" 덩어리는 4개까지만 허용되므로 정상 케이스는 형식을 섞는다.
    forms = ["40세 무용가의 반전 순간{i}.jpg", "1986년생 무용가의 그 장면{i}.jpg",
             "무대를 멈춘 무용가의 선택{i}.jpg"]
    titles = [{"no": i, "title": f"수식어{i} " + forms[i % 3].format(i=i),
               "angle": f"관점{(i - 1) // 4}", "hook": "낙차+장면", "heat": 8}
              for i in range(1, 11)]
    assert validate_titles(titles, fs) == []
    bad = json.loads(json.dumps(titles))
    bad[0]["title"] = "홍길동의 리즈시절"          # 본명 + .jpg 누락
    bad[1]["heat"] = 5                             # 화력 미달
    bad[2]["hook"] = "낙차"                        # 후킹 1개
    errs = validate_titles(bad, fs)
    assert any("본명 노출" in e for e in errs)
    assert any(".jpg 마감 누락" in e for e in errs)
    assert any("7 미만" in e for e in errs)
    assert any("2개 이상" in e for e in errs)

    # 도배 검출: 같은 "40세 무용가" 5개
    combo = [{"no": i, "title": f"40세 무용가 사건{i}의 진짜 이유{i}.jpg",
              "angle": f"관점{i % 3}", "hook": "낙차+격차", "heat": 8}
             for i in range(1, 11)]
    assert any("도배" in e for e in validate_titles(combo, fs))

    # 검증 3: 형식 위반 검출
    B = U2800
    good_lines = ['"그 말 한마디였습니다"' + B, B * 3]
    for ch in range(4):
        good_lines.append(f'"소제목 어쩌고 {ch + 1}번"' + B)
        good_lines.append(B * 3)
        for p in range(4):
            good_lines += ["여기 열다섯 자짜리 문장이" + B,
                           "이어지는 열네 자 문장과" + B,
                           "마무리 열세 자 문장이" + B, B * 3]
    good_lines += ["#홍길동", "#홍길동근황", "#홍길동미모", "#미모비결",
                   "#관계인물", "#대표작", "#무용가미모", "#무용가관리"]
    body = "\n".join(good_lines)
    errs = validate_body(body, fs, {})
    # 글자수는 샘플이라 부족할 수 있다 — 글자수 외 오류가 없어야 한다
    assert all("글자수" in e for e in errs), errs

    broken = body.replace(B * 3, "", 1).replace('"그 말', "그 말", 1) + "\n덧붙임"
    errs = validate_body(broken, fs, {})
    assert any("큰따옴표" in e for e in errs)
    assert any("해시태그가 아님" in e for e in errs)

    # 별표 소제목은 즉시 잡혀야 한다 (네이버 노출 사고 방지)
    starred = body.replace('"소제목 어쩌고 1번"' + B, '**"소제목 어쩌고 1번"**' + B, 1)
    errs = validate_body(starred, fs, {})
    assert any("별표" in e for e in errs), errs

    # 직업 불일치 태그
    errs = validate_body(body.replace("#무용가미모", "#여배우미모"), fs, {})
    assert any("직업 불일치" in e for e in errs)

    print("validate.py 자체 테스트 전부 통과")


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    _selftest()
