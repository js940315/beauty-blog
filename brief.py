"""클로드 코드(또는 클라우드 루틴)에게 넘길 작업지시서를 만든다.

ANTHROPIC_API_KEY가 없어도 파이프라인이 돌게 하는 경로다.
파이썬이 하는 일(수집·제목 점수·형식 검증)은 그대로 두고, LLM이 하는 일만
에이전트에게 넘긴다. 규칙은 prompts.py 한 곳에서만 읽어오므로 두 곳이 어긋나지 않는다.

  1단계  main.py --stage crawl   → sources.json + 1_팩트시트_지시서.md
  2단계  에이전트가 factsheet.json + title_candidates.json 을 쓴다
  3단계  main.py --stage title   → titles.json + 2_본문_지시서.md  (제목은 파이썬이 확정)
  4단계  에이전트가 body.txt 를 쓴다
  5단계  main.py --stage finish  → 검증 → 0번 본문.txt + 사진

지시서와 중간 산출물은 전부 output/날짜/_작업/ 에 둔다.
아침에 폴더를 열면 본문과 사진만 보여야 한다.
"""

import json
import os

import config as C
import prompts


def workdir(outdir):
    """그 날짜의 중간 산출물 폴더. output 밖에 있다."""
    w = os.path.join(C.WORK_DIR, os.path.basename(outdir.rstrip("/\\")))
    os.makedirs(w, exist_ok=True)
    return w


def _w(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def factsheet_text(fs: dict) -> str:
    """팩트시트를 프롬프트에 넣기 좋은 형태로 편다.

    writer.py 가 아니라 여기 있는 이유: writer 는 anthropic SDK를 끌고 온다.
    에이전트 경로는 키도 SDK도 없이 돌아야 하므로 의존성을 섞지 않는다.
    """
    lines = ["[팩트시트]"]
    label = {
        "age": "나이", "job": "직업", "weight_before": "이전 체중",
        "weight_after": "현재 체중", "height": "키", "duration": "기간",
        "notes": "비고",
    }
    for k, ko in label.items():
        v = (fs.get(k) or "").strip()
        if v:
            lines.append(f"- {ko}: {v}")
    for k, ko in (("foods", "아이템"), ("exercises", "관리 루틴"),
                  ("habits", "습관"), ("life_events", "인생 사건")):
        v = [x for x in (fs.get(k) or []) if x]
        if v:
            lines.append(f"- {ko}: {', '.join(v)}")
    q = [x for x in (fs.get("quotes") or []) if x]
    src = fs.get("quote_sources") or []
    if q:
        lines.append("- 본인 발언 (출처를 본문에 밝힐 것):")
        for i, x in enumerate(q):
            where = src[i] if i < len(src) and src[i] else "출처 불명 — 쓰지 말 것"
            lines.append(f'    "{x}"  [{where}]')
    return "\n".join(lines)


def factsheet_brief(outdir, celeb, richness, sources_text, n_sources):
    """1단계 산출물. 팩트시트와 제목 후보를 뽑아달라는 지시서."""
    schema = json.dumps(prompts.FACTSHEET_SCHEMA, ensure_ascii=False, indent=2)
    text = f"""\
# 1단계 지시서 — 팩트시트 + 제목 후보

인물: {celeb}
수집: {n_sources}건
richness: {richness}   {"(근거가 얇다. 억지로 채우지 말 것)" if richness == "thin" else ""}

이 파일을 읽고 아래 **두 개 파일을 이 폴더에 직접 써라.**

  - factsheet.json         (아래 JSON 스키마를 그대로 따른다)
  - title_candidates.json  ({{"titles": ["...", ...]}} 형태, 정확히 10개)

제목을 **고르지 마라.** 후보만 낸다. 선택은 `main.py --stage title` 의 점수 함수가 한다.
그러니 안전한 것만 내지 말고 서로 다른 각도로 최대한 벌려서 내라.

---

## 팩트시트 규칙

{prompts.FACTSHEET_SYSTEM}

{prompts.FACTSHEET_USER.format(celeb=celeb, sources="(소스는 아래 [소스] 절에 있다)")}

### factsheet.json 스키마
```json
{schema}
```

---

## 제목 후보 규칙

{prompts.TITLE_SYSTEM}

{prompts.TITLE_USER.format(factsheet="(방금 네가 만든 factsheet.json)")}

---

## [소스]

{sources_text}
"""
    return _w(os.path.join(workdir(outdir), "1_팩트시트_지시서.md"), text)


def body_brief(outdir, title, fs_text, richness, cta, has_exercise, celeb, heart):
    """3단계 산출물. 확정된 제목으로 본문을 써달라는 지시서."""
    text = f"""\
# 2단계 지시서 — 본문

확정 제목: {title}
(점수 함수가 고른 제목이다. **바꾸지 마라.**)

이 파일을 읽고 **body.txt 를 이 폴더에 직접 써라.** 본문만 넣는다.
제목 줄은 넣지 마라 — `main.py --stage finish` 가 앞에 붙인다.

다 쓰면 `python main.py --stage finish` 를 실행해라.
형식 위반이 나오면 그 목록을 보고 body.txt 를 고쳐서 다시 실행한다.
줄 수·글자 수·점자빈칸은 네가 세지 말고 **검증기가 세게 둬라.**

---

{prompts.BODY_SYSTEM}

---

{prompts.body_user(title, fs_text, richness, cta, has_exercise, celeb, heart)}
"""
    return _w(os.path.join(workdir(outdir), "2_본문_지시서.md"), text)


def repair_note(outdir, problems):
    """검증 실패 목록. 에이전트가 body.txt를 고칠 때 본다."""
    text = "# 형식 위반 — body.txt 를 고쳐서 다시 `--stage finish` 를 실행해라\n\n"
    text += "\n".join(f"- {p}" for p in problems)
    text += """

주의:
- 내용을 새로 지어내지 말고 **형식만** 고쳐라.
- 줄이 길어서 걸렸으면 문장을 쪼개고, 짧아서 걸렸으면 앞뒤를 합쳐라.
- 줄 끝 점자 빈칸과 빈 줄 3개는 눈에 안 보이니 특히 신경 써라.
- 그래도 안 맞으면 `--stage finish --polish` 로 기계적 강제 복구를 걸 수 있다.
  단 polish는 문장을 어절 단위로 자르므로 읽는 맛이 떨어진다. 최후 수단이다.
"""
    return _w(os.path.join(workdir(outdir), "위반목록.md"), text)
