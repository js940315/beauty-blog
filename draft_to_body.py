"""평문 초안 → body.txt (점자 빈칸 자동 부착 + 길이 점검).

점자 빈칸 U+2800은 눈에 안 보인다. 손으로 붙이면 반드시 어디선가 빠지고,
그러면 --stage finish 가 수십 건씩 튕겨낸다. 그 왕복을 없애는 게 이 파일이다.

  python draft_to_body.py draft.txt output/2026-08-04/body.txt

초안 규칙 — 점자는 절대 손으로 넣지 않는다.
  · 한 줄이 한 줄이다 (네이버가 알아서 감싸주지 않는다. 여기서 끊은 대로 나간다)
  · 빈 줄 하나 = 단락 구분  → ⠀⠀⠀ 로 바뀐다
  · 소제목은 config.SUBHEAD_OPEN/CLOSE 형식 (기본 "소제목") — 길이 검사 예외
  · 마크다운 기호는 쓰지 않는다. 네이버가 해석 못 해서 화면에 그대로 보인다
  · 해시태그는 맨 끝에 한 줄에 하나씩

길이·단락 위반은 고쳐주지 않고 **알려만 준다.** 기계가 자르면 읽는 맛이 죽는다.
문장을 어디서 끊을지는 쓰는 사람이 정하는 게 맞다.
(그래도 안 맞으면 마지막 수단으로 main.py --stage finish --polish 가 있다)
"""

import sys

import config as C
import validator


def convert(raw_text: str):
    """(body_text, 문제목록, 내용줄수, 공백제외글자수)"""
    lines = raw_text.replace("\r\n", "\n").split("\n")

    out, problems = [], []
    content = run = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            if out and out[-1] == C.BLANK_LINE:
                continue          # 빈 줄이 겹치면 하나로
            out.append(C.BLANK_LINE)
            run = 0
            continue

        out.append(s + C.BRAILLE)
        if s.startswith("#"):
            continue              # 해시태그는 길이·단락 검사 대상이 아니다

        content += 1
        run += 1
        issues = []
        if not validator._SUBHEAD.match(s) and not (C.LINE_MIN <= len(s) <= C.LINE_MAX):
            issues.append(f"길이 {len(s)}자")   # 소제목은 길이 예외
        if run > C.PARA_MAX_LINES:
            issues.append(f"단락 {run}줄")
        if issues:
            problems.append(f"[{' / '.join(issues)}] {s}")

    while out and out[0] == C.BLANK_LINE:
        out.pop(0)
    while out and out[-1] == C.BLANK_LINE:
        out.pop()

    body = "\n".join(out)
    # 네이버 글자수세기 공백제외 기준: 본문 글자 + 점자 빈칸 (해시태그 제외)
    chars = sum(len(validator.visible(l).replace(" ", ""))
                for l in out if not validator.is_blank(l)
                and not validator.visible(l).startswith("#"))
    chars += sum(l.count(C.BRAILLE)
                 for l in out if not validator.visible(l).startswith("#"))
    return body, problems, content, chars


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)

    with open(sys.argv[1], encoding="utf-8") as f:
        body, problems, content, chars = convert(f.read())
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(body)

    print(f"→ {sys.argv[2]}")
    print(f"   내용 줄 {content}개 (허용 {C.THIN_SOURCE_LINES_MIN}~{C.CONTENT_LINES_MAX}, "
          f"소스가 두꺼우면 {C.CONTENT_LINES_MIN} 이상)")
    print(f"   네이버 공백제외 {chars}자 (점자 포함·해시태그 제외, "
          f"목표 {C.BODY_CHARS_MIN}~{C.BODY_CHARS_MAX} / thin은 {C.THIN_CHARS_MIN} 이상)")
    if problems:
        print(f"   손볼 줄 {len(problems)}개:")
        for p in problems:
            print(f"     · {p}")
    else:
        print("   길이·단락 문제 없음")


if __name__ == "__main__":
    main()
