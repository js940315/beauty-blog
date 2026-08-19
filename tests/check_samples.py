# -*- coding: utf-8 -*-
"""실적글 회귀 — 사용자가 준 상위 랭킹 글이 지금 검증기를 통과하는가.

2026-08-19 신설. 이 저장소는 규격을 여러 번 뒤집었고, 그때마다 **실제로 잘
나가는 글이 반려되는 상태**가 됐는데 아무도 몰랐다. 08-16 까지의 규격은
아래 4편을 한 편도 통과시키지 못했다.

이제 규칙을 손댈 때마다 이걸 돌린다:

    python tests/check_samples.py

01~04 는 반드시 위반 0 이어야 한다.
05 는 **일부러 통과 못 하는 예외**다 — 같은 계정 글이지만 형식이 다르다
(19자 초과 줄 11개 · 소제목 5개 · 1050자). 01~04 가 공유하는 모바일
가독성 규율과 정면으로 어긋나고, 다섯 편 중 조회수가 가장 낮다.
05 까지 통과시키려면 한 줄 상한을 29자로 풀어야 하는데, 그러면 규격이
사실상 사라진다. 그래서 통과 대상에서 뺐다 — 지우지는 마라, 판단 근거다.
"""
import io
import os
import re
import sys

# 콘솔 코드페이지가 cp949 라 em-dash 에서 죽는다 (실측). 출력만 utf-8 로 고정한다.
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config as C          # noqa: E402
import validate as V        # noqa: E402
import validator as VR      # noqa: E402

B = C.BRAILLE
NL = chr(10)
SAMPLES = "benchmark_samples"

# 파일명 → (인물명, 직업)
PEOPLE = {
    "01_김규리.txt": ("김규리", "배우"),
    "02_이영애.txt": ("이영애", "배우"),
    "03_신카와유아.txt": ("신카와 유아", "배우"),
    "04_채수빈.txt": ("채수빈", "배우"),
}
EXCEPTION = "05_이시원_예외.txt"

# 2026-08-19 신규격으로 직접 써 본 참고 본문. **실적글이 아니라 규격 검증용**이다.
# "규격이 실제로 만족 가능한가"를 증명한다 — 규칙을 조이다 보면 어느 순간
# 아무 글도 통과 못 하는 상태가 되는데, 그걸 이걸로 잡는다.
# 지시서에는 넣지 않는다. 예시를 주면 모델이 통째로 베낀다(CLICHE_OPENERS 참고).
REFERENCE = ("99_newspec_reference.txt", "이영애", "배우")


def to_body(raw):
    """평문을 점자 부착 최종형으로 바꾼다 (draft_to_body 와 같은 규칙)."""
    out, prev_blank = [], True
    for ln in raw.replace("\r\n", "\n").split("\n"):
        t = ln.replace(B, "").strip()
        if t:
            out.append(t + B)
            prev_blank = False
        else:
            if not prev_blank:
                out.append(B * 3)
            prev_blank = True
    while out and out[-1] == B * 3:
        out.pop()
    return out


def factsheet(name, job):
    return {"person": {"name": name, "gender": "여", "job": job,
                       "identity_anchor": f"{name} 본인"},
            "namesake_dropped": [],
            "quotes": [{"speaker": name, "text": "말", "context": ""}],
            "hot_materials": [{"material": "x", "why_hot": "낙차"}]}


def naver_chars(lines):
    body = [l for l in lines if not l.replace(B, "").strip().startswith("#")]
    return (sum(len(re.sub(r"\s", "", l.replace(B, ""))) for l in body)
            + sum(l.count(B) for l in body))


def run(fn, name, job, reactions=False):
    raw = open(os.path.join(HERE, SAMPLES, fn), encoding="utf-8").read()
    lines = to_body(raw)
    # 픽스처에 해시태그가 들어 있으면 떼어낸다 — 경로마다 태그 규칙이 달라서
    # (v13 은 자유 8개, 일일은 고정 4개 + 인물 2개) 여기서 각자 붙인다.
    while lines and lines[-1].replace(B, "").strip().startswith("#"):
        lines.pop()
    while lines and lines[-1] == B * 3:
        lines.pop()
    flat = name.replace(" ", "")

    # v13 경로 (pipeline.py → validate.validate_body)
    tags13 = [f"#{flat}", f"#{flat}근황", f"#{flat}미모", "#소재하나",
              "#소재둘", "#소재셋", "#여배우미모", "#연예인뷰티"]
    fs = factsheet(name, job)
    if reactions:
        # 반응 인용 검사는 재료가 있을 때만 켜진다. 참고본은 그 경로도 밟는다.
        fs["public_reactions"] = [{"text": "예쁜 게 아냐, 아름다워", "where": "방송"}]
    e13 = V.validate_body(NL.join(lines + [B * 3] + tags13), fs, {})

    # 일일 경로 (main.py → validator.validate)
    tags1 = list(C.FIXED_HASHTAGS) + [f"#{flat}", f"#{flat}패션",
                                      "#근황", "#미모비결"]
    e1 = VR.validate("\n".join(lines + [B * 3] + tags1),
                     richness="normal", cta=None, celeb=flat)

    content = [l for l in lines if l != B * 3]
    return e13, e1, naver_chars(lines), len(content)


def check_titles():
    """상위 20편 제목이 채점기에서 살아남는가 (2026-08-19 신설).

    이걸 만든 이유: 이 저장소의 제목 채점기는 **자기 블로그 상위 20편 중
    12편을 즉시 탈락**시키고 있었다. 원인은 규칙 자체가 아니라 판정 버그였다.
      · "현빈이 여배우 중" 의 조사 "이" 를 지시대명사로 오인
      · "이쁜 여배우" 의 "이" 를 지시대명사로 오인
      · 실명 뒤에 조사가 붙으면(송혜교도·손예진과) 실명으로 인식 못 함
      · CELEB_POOL(주인공 후보) 이름이면 제3자로 쓴 경우까지 탈락
      · "얼마나 예쁘면" 이 강조 표현으로 안 잡힘 (어미 "면" 누락)
    전부 고쳤고, 다시 뒤집히지 않게 여기서 매번 대조한다.
    """
    path = os.path.join(HERE, SAMPLES, "00_top20_titles.txt")
    if not os.path.exists(path):
        return 0
    import titles as T
    dq = low = 0
    for line in open(path, encoding="utf-8").read().strip().split(chr(10)):
        views, title = line.split("|", 1)
        sc, _ = T.score(title)
        units = T.hook_units(title)
        if sc == C.DISQUALIFIED:
            dq += 1
            print(f"FAIL 제목 탈락 ({int(views):,}회) — {T.disqualify_reason(title)}")
            print(f"       {title}")
        elif sc < C.MIN_SCORE:
            low += 1
            print(f"note 하한 미만 {sc}점 · 훅 {len(units)}개 "
                  f"({int(views):,}회) {title[:34]}")
    print(f"{'FAIL' if dq else 'OK  '} 상위 20편 제목 — 탈락 {dq} · "
          f"하한({C.MIN_SCORE}) 미만 {low}")
    return dq


def main():
    bad = 0
    bad += check_titles()
    print()
    for fn, (name, job) in sorted(PEOPLE.items()):
        e13, e1, n, rows = run(fn, name, job)
        ok = not e13 and not e1
        print(f"{'OK  ' if ok else 'FAIL'} {fn}  {n}자 / 내용줄 {rows}")
        for e in e13:
            print("       [v13]  ", e)
        for e in e1:
            print("       [일일] ", e)
        if not ok:
            bad += 1

    # 신규격 참고 본문 — 반드시 통과해야 한다 (규격이 만족 가능한가)
    fn, name, job = REFERENCE
    if os.path.exists(os.path.join(HERE, SAMPLES, fn)):
        e13, e1, n, rows = run(fn, name, job, reactions=True)
        ok = not e13 and not e1
        print(f"{'OK  ' if ok else 'FAIL'} {fn}  {n}자 / 내용줄 {rows}  (신규격 참고본)")
        for e in e13:
            print("       [v13]  ", e)
        for e in e1:
            print("       [일일] ", e)
        if not ok:
            bad += 1

    # 예외 파일은 "여전히 통과하지 못하는가"를 확인한다. 어느 날 통과하기
    # 시작했다면 규격이 풀렸다는 뜻이니 그것도 알려줘야 한다.
    e13, e1, n, rows = run(EXCEPTION, "이시원", "배우")
    if e13 or e1:
        print(f"note {EXCEPTION}  {n}자 / 내용줄 {rows} — "
              f"의도된 예외 (위반 v13 {len(e13)} · 일일 {len(e1)})")
    else:
        print(f"WARN {EXCEPTION} 이 통과했다 — 규격이 너무 느슨해졌는지 확인해라")
        bad += 1

    if bad:
        print(f"\n실적글 회귀 실패 {bad}건")
        return 1
    print("\n실적글 회귀 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
