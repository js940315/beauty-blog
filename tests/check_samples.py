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
SAMPLES = "benchmark_samples"

# 파일명 → (인물명, 직업)
PEOPLE = {
    "01_김규리.txt": ("김규리", "배우"),
    "02_이영애.txt": ("이영애", "배우"),
    "03_신카와유아.txt": ("신카와 유아", "배우"),
    "04_채수빈.txt": ("채수빈", "배우"),
}
EXCEPTION = "05_이시원_예외.txt"


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


def run(fn, name, job):
    raw = open(os.path.join(HERE, SAMPLES, fn), encoding="utf-8").read()
    lines = to_body(raw)
    flat = name.replace(" ", "")

    # v13 경로 (pipeline.py → validate.validate_body)
    tags13 = [f"#{flat}", f"#{flat}근황", f"#{flat}미모", "#소재하나",
              "#소재둘", "#소재셋", "#여배우미모", "#연예인뷰티"]
    e13 = V.validate_body("\n".join(lines + [B * 3] + tags13),
                          factsheet(name, job), {})

    # 일일 경로 (main.py → validator.validate)
    tags1 = list(C.FIXED_HASHTAGS) + [f"#{flat}", f"#{flat}패션",
                                      "#근황", "#미모비결"]
    e1 = VR.validate("\n".join(lines + [B * 3] + tags1),
                     richness="normal", cta=None, celeb=flat)

    content = [l for l in lines if l != B * 3]
    return e13, e1, naver_chars(lines), len(content)


def main():
    bad = 0
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
