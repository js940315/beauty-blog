# -*- coding: utf-8 -*-
"""v13 미모·이슈 파이프라인 오케스트레이터.

ANTHROPIC_API_KEY 가 없는 환경이 기본이므로 main.py 와 같은 '에이전트 경로'다:
파이썬은 지시서 생성·검증·랜덤 주입·상태 기억을 맡고, 글 쓰는 일은
클로드 코드(에이전트)가 한다.

    python pipeline.py --source 소스.txt      # A 지시서 생성
      (에이전트가 factsheet.json 작성)
    python pipeline.py --stage titles         # 검증1 → B 지시서 생성
      (에이전트가 titles.json 작성)
    python pipeline.py --stage body           # 검증2 → 1위 자동 확정 → C 지시서
      (에이전트가 body.txt 작성)
    python pipeline.py --stage finish         # 검증3 → 완성본 저장 + state 갱신
    python pipeline.py --retitle 3            # 예비 3번 제목으로 C 지시서 재생성
    python pipeline.py --retitle "직접 쓴 제목"

검증 실패 시 종료 코드 4 + 위반 목록 출력. 해당 파일만 고쳐 같은 명령을 다시 돌린다.
"""

import argparse
import datetime
import json
import os
import random
import re
import sys

import validate as V

ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPTS = os.path.join(ROOT, "prompts")
DATA = os.path.join(ROOT, "data")
WORK_ROOT = os.path.join(ROOT, "state", "v13work")
CURRENT = os.path.join(WORK_ROOT, "CURRENT")
STATE = os.path.join(ROOT, "state.json")
OUT_ROOT = os.path.join(ROOT, "output")

MAX_REPAIR = 3


def _utf8():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    """숨김 속성 파일도 안전하게 덮어쓴다.

    윈도우는 숨김 파일을 open("w") 로 열면 PermissionError 를 낸다.
    로컬 편의로 루트 파일을 숨겨두는 운영 방식과 충돌하므로,
    쓰기 전에 잠깐 풀었다가 끝나면 되살린다.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    hidden = False
    if os.name == "nt" and os.path.exists(path):
        import ctypes
        HIDDEN = 2
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        if attrs not in (-1, 0xFFFFFFFF) and attrs & HIDDEN:
            hidden = True
            ctypes.windll.kernel32.SetFileAttributesW(path, attrs & ~HIDDEN)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if hidden:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(path)
        ctypes.windll.kernel32.SetFileAttributesW(path, attrs | 2)


def _jload(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _jdump(path, obj):
    _write(path, json.dumps(obj, ensure_ascii=False, indent=2))


def _state():
    return _jload(STATE, {"recent_posts": []})


def _workdir(create=False, force=False):
    if create:
        # 미완성 세션 덮어쓰기 방지: 직전 세션이 finish 를 못 끝냈으면 막는다.
        # (--auto 를 연달아 치면 CURRENT 가 넘어가 앞 글을 잃는 사고 — 실측 2회)
        if not force and os.path.exists(CURRENT):
            prev = _read(CURRENT).strip()
            if (os.path.isdir(prev)
                    and os.path.exists(os.path.join(prev, "A_지시서.md"))
                    and not os.path.exists(os.path.join(prev, "DONE"))):
                raise SystemExit(
                    f"직전 세션이 아직 finish 되지 않았습니다: {prev}\n"
                    "먼저 그 인물을 끝내거나, 버리려면 --force 를 붙이세요.")
        d = os.path.join(WORK_ROOT,
                         datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        os.makedirs(d, exist_ok=True)
        _write(CURRENT, d)
        return d
    if not os.path.exists(CURRENT):
        raise SystemExit("진행 중인 세션이 없습니다. --source 로 시작하세요.")
    return _read(CURRENT).strip()


def _fail(errs, what):
    print(f"■ {what} 검증 실패 {len(errs)}건:")
    for e in errs:
        print("   ·", e)
    print("→ 위 항목만 고쳐서 같은 명령을 다시 실행하세요.")
    raise SystemExit(4)


# ── 단계 0: 소스 → A 지시서 ─────────────────────────────────────────────

def stage_source(source_path, force=False):
    src = _read(source_path).strip()
    if not src:
        raise SystemExit("소스가 비어 있습니다.")
    d = _workdir(create=True, force=force)
    _write(os.path.join(d, "source.txt"), src)
    prompt = _read(os.path.join(PROMPTS, "A_factsheet.md"))
    _write(os.path.join(d, "A_지시서.md"),
           prompt.replace("{source_text}", src))
    print(f"■ 세션 시작 → {d}")
    print("   에이전트가 할 일: A_지시서.md 를 전부 읽고 같은 폴더에 factsheet.json 작성")
    print("   그 다음: python pipeline.py --stage titles")


def recent_persons(days=45):
    """최근 N일 안에 v13이 쓴 인물 목록 (재등장 방지)."""
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=days)).isoformat()
    return {p["person"] for p in _state()["recent_posts"]
            if p.get("date", "") >= cutoff}


def stage_auto(celeb, force=False):
    """crawler 로 소스를 자동 구성해 세션을 연다 (하루 10편 운영의 기본 진입점).

    발언(따옴표)·직업 표기 기사를 앞세워 32건을 뽑는다 — 직업 확정과
    quotes 확보가 팩트시트 검증 1의 관문이기 때문이다.
    """
    if celeb in recent_persons():
        print(f"⚠ {celeb} 은 최근 45일 안에 이미 썼습니다. 계속 진행은 하지만 확인할 것.")
    import crawler
    import store
    store.init()
    items = crawler.collect(celeb, mode="google")
    if len(items) < 6:
        raise SystemExit(f"{celeb}: 수집 {len(items)}건 — 소스가 얇아 진행 불가. 다른 인물로.")
    keyed = [it for it in items
             if '"' in it["title"] or "“" in it["title"]
             or "배우" in it["title"] or "가수" in it["title"]]
    seen, picked = set(), []
    for it in keyed + items:
        t = it["title"].strip()
        if t in seen:
            continue
        seen.add(t)
        picked.append(it)
        if len(picked) >= 32:
            break
    lines = [f"[{i}] ({it.get('media') or it.get('source', '')}) {it['title']}"
             for i, it in enumerate(picked, 1)]
    tmp = os.path.join(WORK_ROOT, f"_auto_source_{celeb}.txt")
    _write(tmp, "\n".join(lines))
    print(f"■ {celeb}: {len(items)}건 수집 → 소스 {len(picked)}건 구성")
    stage_source(tmp, force=force)


def stage_plan(n):
    """오늘 쓸 인물 N명 추천 — 최근 45일 미사용 인물을 config 풀에서 뽑는다.

    v13 사용 이력(state.json)과 일일 시스템 쿨다운(store.db)을 둘 다 거른다.
    (2026-08-07: 일일이 쓴 김유정을 v13이 다음날 또 쓴 교차 중복 실측 → 보강)
    """
    import config as CFG
    import store
    store.init()
    used = recent_persons()
    avail = [c for c in CFG.CELEB_POOL
             if c not in used and not store.celeb_on_cooldown(c)]
    print(f"■ 후보 {len(avail)}명 (풀 {len(CFG.CELEB_POOL)} - 최근 사용 {len(used)})")
    print("   오늘의 추천:", ", ".join(avail[:n]))
    print("   시작: python pipeline.py --auto <인물명>")


# ── 단계 1: 검증1 → B 지시서 ────────────────────────────────────────────

def stage_titles():
    d = _workdir()
    fs = _jload(os.path.join(d, "factsheet.json"))
    if fs is None:
        raise SystemExit("factsheet.json 이 없습니다. A_지시서.md 를 먼저 처리하세요.")
    errs = V.validate_factsheet(fs)
    if errs:
        _fail(errs, "팩트시트")

    st = _state()
    recent = st["recent_posts"]
    banned_first = [p["title"].split()[0] for p in recent[-2:]
                    if p.get("title", "").split()]
    recent_hooks = sorted({h for p in recent[-3:]
                           for h in p.get("hooks_used", [])})

    # 슬롯이 제목 문형을 정한다 (INSTRUCTION.md §6). 제목을 뽑기 전에 확정돼야 한다.
    plan = slot_plan(next_slot())
    _jdump(os.path.join(d, "slot.json"), plan)

    prompt = _read(os.path.join(PROMPTS, "B_titles.md"))
    prompt = (prompt
              .replace("{factsheet}", json.dumps(fs, ensure_ascii=False, indent=2))
              .replace("{banned_first_words}",
                       ", ".join(banned_first) or "(없음)")
              .replace("{recent_hooks}", ", ".join(recent_hooks) or "(없음)")
              .replace("{slot_no}", str(plan["slot"]))
              .replace("{title_type}", plan["title_type"]))
    _write(os.path.join(d, "B_지시서.md"), prompt)
    print(f"■ 팩트시트 검증 통과 — {plan['slot']}번 슬롯 / "
          f"포맷 {plan['format']} / 제목 문형 {plan['title_type']}")
    print(f"   에이전트가 할 일: B_지시서.md 를 전부 읽고 titles.json 작성 (정확히 10개)")
    print("   그 다음: python pipeline.py --stage body")


# ── 단계 2: 검증2 → 1위 자동 확정 → C 지시서 ────────────────────────────

def _kst_now():
    """클라우드(UTC)에서도 폴더 날짜가 한국 기준이 되도록 KST 고정."""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def next_slot() -> int:
    """오늘 몇 번 슬롯인지. 방을 만들기 전에 미리 안다 (2026-08-14).

    ⚠️ 슬롯 번호가 **제목을 뽑기 전에** 확정돼야 한다.
       INSTRUCTION.md §2·§6 의 포맷·제목 문형 로테이션이 슬롯 번호로 갈리는데,
       예전엔 제목을 다 뽑은 뒤(stage_body)에야 방을 배정해서 로테이션을
       걸 자리가 아예 없었다. 그래서 매일 같은 문형이 나왔다.
    """
    day_dir = os.path.join(OUT_ROOT, _kst_now().strftime("%m%d"))
    n = 2                                  # 1번은 일일 자동 포스팅 몫
    while os.path.exists(os.path.join(day_dir, str(n))):
        n += 1
    return n


def slot_plan(slot: int) -> dict:
    """슬롯 번호 → 포맷·제목 문형. 코드가 강제 주입하는 값이다."""
    import config as CFG
    return {
        "slot": slot,
        "format": CFG.SLOT_FORMATS.get(slot, "A"),
        "title_type": CFG.SLOT_TITLE_TYPES.get(slot, "수치충돌"),
    }


def _alloc_outdir(person):
    """output/MMDD/번호/ 방 배정. 1번은 일일 자동 포스팅 몫이라 2번부터 쓴다.

    (2026-08-06 사용자 확정: 경제비버와 같은 MMDD/번호 구조로 전 블로그 통일)
    """
    day_dir = os.path.join(OUT_ROOT, _kst_now().strftime("%m%d"))
    os.makedirs(day_dir, exist_ok=True)
    n = next_slot()
    out = os.path.join(day_dir, str(n))
    os.makedirs(out)
    return out


def _pool_pick(pool, used_recent):
    avail = [x for x in pool if x not in used_recent] or list(pool)
    return random.choice(avail)


def _make_c_brief(d, fs, title, marker=""):
    """C 지시서 생성. 포맷·브릿지·논쟁질문·마감은 코드가 골라 주입한다.

    2026-08-14 전면 개편 (INSTRUCTION.md):
      - 포맷 A~E 는 슬롯이 정한다. 프롬프트에 "섞어라"라고 적으면 안 갈린다.
      - 마감 격언(endings.json 회고형)은 §3 금지 목록에 올라 폐기했다.
      - 하트 CTA 는 논쟁 질문 + 행동 지시 2단으로 교체했다.
    """
    import config as CFG
    st = _state()
    recent = st["recent_posts"][-3:]
    conn_pool = _jload(os.path.join(DATA, "connectors.json"), {}).get("pool", [])

    plan = _jload(os.path.join(d, "slot.json")) or slot_plan(next_slot())
    fmt = CFG.FORMAT_SPECS[plan["format"]]

    debate = _pool_pick(CFG.DEBATE_POOL, [p.get("cta_used") for p in recent])
    closing = _pool_pick(CFG.CLOSING_POOL, [p.get("closing_used") for p in recent])
    # 브릿지는 2개를 서로 다르게 뽑는다 (§4-2)
    bridges = random.sample(list(CFG.BRIDGE_POOL), CFG.BRIDGE_MIN)
    conn = random.choice(conn_pool) if conn_pool else ""

    prompt = _read(os.path.join(PROMPTS, "C_body.md"))
    prompt = (prompt
              .replace("{title}", title)
              .replace("{factsheet}", json.dumps(fs, ensure_ascii=False, indent=2))
              .replace("{slot_no}", str(plan["slot"]))
              .replace("{format_key}", plan["format"])
              .replace("{format_name}", fmt["name"])
              .replace("{format_open}", fmt["open"])
              .replace("{format_skeleton}", fmt["skeleton"])
              .replace("{bridge_picks}", " / ".join(bridges))
              .replace("{debate_pick}", debate)
              .replace("{closing_pick}", closing)
              .replace("{connector_pick}", conn))
    _write(os.path.join(d, "C_지시서.md"), prompt)
    _jdump(os.path.join(d, "meta.json"),
           {"title": title, "slot": plan["slot"], "format": plan["format"],
            "cta": debate, "closing": closing, "bridges": bridges,
            "connector": conn, "marker": marker})
    print(f"■ 확정 제목: {title}")
    print(f"   슬롯 {plan['slot']} / 포맷 {plan['format']}({fmt['name']})")
    print(f"   주입: 논쟁질문 「{debate}」")
    print(f"         마감 「{closing}」")
    print("   에이전트가 할 일: C_지시서.md 를 전부 읽고 body.txt 작성 (평문 아님 — 점자 포함 최종형)")
    print("   그 다음: python pipeline.py --stage finish")


def stage_body():
    d = _workdir()
    fs = _jload(os.path.join(d, "factsheet.json"))
    tj = _jload(os.path.join(d, "titles.json"))
    if tj is None:
        raise SystemExit("titles.json 이 없습니다. B_지시서.md 를 먼저 처리하세요.")
    titles = tj.get("titles", tj if isinstance(tj, list) else [])
    errs = V.validate_titles(titles, fs)
    if errs:
        _fail(errs, "제목")

    # 확정은 점수 함수가 한다. 모델이 아니다 (2026-08-13 사용자 확정).
    #
    # 예전엔 heat(모델이 자기 제목에 스스로 매긴 점수)로 1위를 뽑았다. 그러면
    # 저장소 설계 원칙 ①("제목은 LLM이 고르지 않는다")이 v13 경로에서만 깨진다.
    # 실제 사고: "부활절 인사 남긴 근황.jpg" 가 heat 최고점으로 확정됐다.
    # 인물도 없고 후킹도 없는 제목인데 모델은 자기 것이라 높게 준다.
    # heat 는 동점일 때 순서만 가른다.
    import config as CFG
    import titles as TS
    ranked = []
    for t in titles:
        s, why = TS.score(t.get("title", ""))
        ranked.append({**t, "score": s, "score_reasons": why})
    ranked.sort(key=lambda t: (-t["score"], -t.get("heat", 0)))
    top = ranked[0]

    if top["score"] < CFG.TITLE_SCORE_FLOOR:
        _fail([f"1위 {top['score']}점 — 확정 하한 {CFG.TITLE_SCORE_FLOOR}점 미달: "
               f"{top['title']}"]
              + [f"   · {r}" for r in top["score_reasons"]]
              + ["10개 전부 다시 뽑아라. 감점 사유를 그대로 뒤집으면 된다 — "
                 "여성 인물 지칭을 넣고, 앞 20자에 숫자·장면·실명을 박고, "
                 "진부한 마무리를 버리고, 24자 이상으로 늘려라."],
              "제목 화력")

    print(f"■ 제목 점수 1위 {top['score']}점 "
          f"(2위 {ranked[1]['score'] if len(ranked) > 1 else '-'}점)")
    for r in top["score_reasons"]:
        print("   ·", r)

    # 예비 후보 저장 (2~10위).
    # 라벨·번호 없이 제목만, 빈 줄 간격 — 복사 붙여넣기 최우선 (사용자 확정 2026-08-05).
    # 화력·후킹 라벨이 필요하면 state/v13work/<세션>/ranked.json 에 그대로 있다.
    lines = ["[확정]", "", top["title"], "", "─── 예비 ───"]
    for t in ranked[1:]:
        lines += ["", t["title"]]
    out_dir = _alloc_outdir(fs["person"]["name"])
    _write(os.path.join(out_dir, "예비제목.txt"), "\n".join(lines) + "\n")
    _jdump(os.path.join(d, "ranked.json"), ranked)
    _write(os.path.join(d, "OUTDIR"), out_dir)

    _make_c_brief(d, fs, top["title"], marker="auto#1")


# ── --wrap: 평문 초안 → 점자 최종형 ─────────────────────────────────────

def stage_wrap():
    """세션 폴더의 draft_plain.txt 를 body.txt(점자 부착)로 변환한다.

    점자 빈칸을 손으로 붙이면 반드시 빠진다(실측) — 본문은 평문으로 쓰고
    이 명령으로 변환하는 게 정석이다. 해시태그 줄은 점자를 붙이지 않는다.
    """
    d = _workdir()
    src = os.path.join(d, "draft_plain.txt")
    if not os.path.exists(src):
        raise SystemExit("draft_plain.txt 가 없습니다. 본문을 평문으로 먼저 써라.")
    B = V.U2800
    out = []
    for ln in _read(src).split("\n"):
        s = ln.rstrip()
        if not s:
            out.append(B * 3)
        elif s.startswith("#"):
            out.append(s)
        else:
            out.append(s + B)
    while out and out[-1] == B * 3:
        out.pop()
    _write(os.path.join(d, "body.txt"), "\n".join(out))
    n = len(re.sub(r"[\s⠀]", "", "\n".join(out)))
    print(f"■ body.txt 변환 완료 — {len(out)}줄 / 공백·점자 제외 {n}자 (목표 850~1000)")
    print("   그 다음: python pipeline.py --stage finish")


# ── --retitle: 본문만 재생성 ────────────────────────────────────────────

def stage_retitle(arg):
    d = _workdir()
    fs = _jload(os.path.join(d, "factsheet.json"))
    ranked = _jload(os.path.join(d, "ranked.json"), [])
    if re.fullmatch(r"\d+", arg):
        idx = int(arg)
        if not (2 <= idx <= len(ranked)):
            raise SystemExit(f"예비 번호는 2~{len(ranked)} 사이여야 합니다.")
        title = ranked[idx - 1]["title"]
        marker = f"retitle#{idx}"
    else:
        # 2026-08-14 사용자 확정: .jpg 마감 폐지. 옛 습관으로 붙여 넣으면 떼준다.
        title = arg[:-4].rstrip() if arg.endswith(".jpg") else arg
        name = fs["person"]["name"]
        if name and name in title:
            raise SystemExit(f"제목에 본명({name})이 들어 있습니다.")
        marker = "retitle#manual"
    _make_c_brief(d, fs, title, marker=marker)


# ── 단계 3: 검증3 → 완성본 + state 갱신 ─────────────────────────────────

def stage_finish():
    d = _workdir()
    fs = _jload(os.path.join(d, "factsheet.json"))
    meta = _jload(os.path.join(d, "meta.json"))
    body_path = os.path.join(d, "body.txt")
    if not os.path.exists(body_path):
        raise SystemExit("body.txt 가 없습니다. C_지시서.md 를 먼저 처리하세요.")
    body = _read(body_path).rstrip("\n")

    st = _state()
    errs = V.validate_body(
        body, fs,
        {"recent_endings": [p.get("ending_used") for p in
                            st["recent_posts"][-3:] if p.get("ending_used")]},
        debate=meta.get("cta", ""))     # 코드가 주입한 논쟁 질문

    # 제목 훅 회수 검증 — 어그로만 걸고 본문이 딴소리하면 낚시로 읽힌다.
    # 제목의 숫자와 인용구가 본문에 실제로 있는지 대조한다 (일일 시스템과 동일 원칙).
    title_now = meta["title"]
    flat = re.sub(r"[\s⠀]", "", body)
    for num in set(re.findall(r"\d+", title_now)):
        if num not in flat:
            errs.append(f"제목의 숫자 {num} 이 본문에 없음 — 훅은 본문에서 회수해야 한다")
    for q in re.findall(r'"([^"]{2,20})"', title_now):
        if re.sub(r"\s", "", q) not in flat:
            errs.append(f'제목의 인용구 "{q}" 가 본문에 없음 — 도입부에서 회수해야 한다')

    attempts = _jload(os.path.join(d, "attempts.json"), {"n": 0})
    attempts["n"] += 1
    _jdump(os.path.join(d, "attempts.json"), attempts)

    if errs:
        _write(os.path.join(d, "위반목록.md"),
               "\n".join("- " + e for e in errs) + "\n")
        if attempts["n"] >= MAX_REPAIR:
            print(f"⚠ {attempts['n']}회째 실패 — 반자동 설계상 여기서 사람에게 넘깁니다.")
        _fail(errs, f"본문 ({attempts['n']}회차)")

    if not os.path.exists(os.path.join(d, "OUTDIR")):
        raise SystemExit("OUTDIR 기록이 없습니다. --stage body 를 먼저 실행하세요.")
    out_dir = _read(os.path.join(d, "OUTDIR")).strip()
    title = meta["title"]
    _write(os.path.join(out_dir, "완성본.txt"), title + "\n\n" + body + "\n")

    # 이미지메모.txt — 발행할 때 사진을 어디서 구할지 알려준다.
    # (2026-08-10 사용자 확정: 이미지 조달 시간이 시스템 최대 병목)
    import config as C
    name = fs["person"]["name"]
    sns = [s for s in (fs.get("sns_materials") or []) if s]
    memo = [f"■ {name} — 이미지 어디서 구하나", ""]
    if sns:
        memo.append("본인이 SNS에 직접 올린 소재라 계정에서 바로 찾을 수 있습니다:")
        for s in sns[:5]:
            memo.append(f"  · {s}")
        memo.append("")
    memo.append(C.INSTAGRAM_SEARCH.format(name=name))
    memo.append("")
    memo.append("※ 못 찾으면 얼굴 사진 없이 발행해도 되는 글입니다.")
    memo.append("   (본문이 착장을 특정하지 않으므로 어떤 근황 사진이든 어울립니다)")
    _write(os.path.join(out_dir, "이미지메모.txt"), "\n".join(memo) + "\n")

    # 날짜 폴더 목차 갱신: 몇 번 방이 누구·무슨 제목인지 한눈에
    day_dir = os.path.dirname(out_dir)
    slot = os.path.basename(out_dir)
    toc_path = os.path.join(day_dir, "목차.txt")
    toc = [ln for ln in (_read(toc_path).split("\n") if os.path.exists(toc_path) else [])
           if ln.strip() and not ln.startswith(f"{slot}. ")]
    toc.append(f"{slot}. {fs['person']['name']} — {title}")
    toc.sort(key=lambda x: int(x.split(".")[0]) if x.split(".")[0].isdigit() else 0)
    _write(toc_path, "\n".join(toc) + "\n")

    # 마감 문장 실사용분 추출 (state 중복 대조용).
    # 문장이 여러 줄에 걸치므로 본문을 평문으로 합친 뒤 틀의 고정부로 찾는다.
    tpl = meta.get("ending_template", "")
    segs = [s.strip() for s in tpl.split("___") if s.strip()]
    key = max(segs, key=len) if segs else ""
    # 빈칸 뒤 첫 어절은 조사가 붙어 변형된다 ("___을" → "태도를") — 버리고 찾는다
    if key and " " in key:
        key = key.split(" ", 1)[1]
    used_ending = ""
    if key:
        flat = " ".join(ln.replace(V.U2800, "").strip()
                        for ln in body.split("\n") if ln.replace(V.U2800, "").strip())
        m = re.search(r"[^.!?]*" + re.escape(key), flat)
        if m:
            used_ending = m.group(0).strip()

    _write(os.path.join(d, "DONE"), title)

    ranked = _jload(os.path.join(d, "ranked.json"), [])
    top = next((t for t in ranked if t["title"] == title), ranked[0] if ranked else {})
    # finish 재실행으로 같은 글이 두 번 기록되는 것 방지 (로테이션 오염 방지)
    st["recent_posts"] = [p for p in st["recent_posts"]
                          if p.get("title") != title]
    st["recent_posts"].append({
        "date": _kst_now().date().isoformat(),
        "person": fs["person"]["name"],
        "title": title,
        "ending_template": meta.get("ending_template", ""),
        "ending_used": used_ending,
        "cta_used": meta.get("cta", ""),
        "hooks_used": [h for h in re.split(r"[+|,\s]+", top.get("hook", "")) if h],
        "angle": top.get("angle", ""),
    })
    # 하루 10편 운영: 인물 45일 재등장 방지에 쓰이므로 넉넉히 보관한다.
    # (마감·CTA 로테이션은 여전히 최근 3개만 본다)
    st["recent_posts"] = st["recent_posts"][-500:]
    _jdump(STATE, st)

    n = len(re.sub(r"[\s⠀]", "", body))
    print(f"■ 완성 → {os.path.join(out_dir, '완성본.txt')}")
    print(f"   공백·점자 제외 {n}자 / {attempts['n']}회 만에 위반 0 / state.json 갱신")
    print("   사람 검수 후 발행하세요. 제목 교체: python pipeline.py --retitle <번호|제목>")


def main():
    _utf8()
    ap = argparse.ArgumentParser(description="v13 미모·이슈 생성 파이프라인")
    ap.add_argument("--source", help="소스 텍스트 파일 경로 (새 세션 시작)")
    ap.add_argument("--auto", metavar="인물명",
                    help="crawler 로 소스 자동 구성 후 세션 시작 (일일 운영 기본)")
    ap.add_argument("--plan", type=int, metavar="N",
                    help="오늘 쓸 인물 N명 추천 (최근 45일 미사용)")
    ap.add_argument("--stage", choices=("titles", "body", "finish"))
    ap.add_argument("--wrap", action="store_true",
                    help="draft_plain.txt → body.txt 점자 변환")
    ap.add_argument("--retitle", help="예비 번호(2~10) 또는 직접 쓴 제목")
    ap.add_argument("--force", action="store_true",
                    help="미완성 직전 세션을 버리고 새 세션 시작")
    a = ap.parse_args()

    if a.plan:
        stage_plan(a.plan)
    elif a.wrap:
        stage_wrap()
    elif a.auto:
        stage_auto(a.auto, force=a.force)
    elif a.source:
        stage_source(a.source, force=a.force)
    elif a.retitle:
        stage_retitle(a.retitle)
    elif a.stage == "titles":
        stage_titles()
    elif a.stage == "body":
        stage_body()
    elif a.stage == "finish":
        stage_finish()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
