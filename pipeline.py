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

def stage_source(source_path, force=False, bench_rows=None):
    src = _read(source_path).strip()
    if not src:
        raise SystemExit("소스가 비어 있습니다.")
    d = _workdir(create=True, force=force)
    _write(os.path.join(d, "source.txt"), src)
    if bench_rows:
        _jdump(os.path.join(d, "bench.json"), bench_rows)
    prompt = _read(os.path.join(PROMPTS, "A_factsheet.md"))
    _write(os.path.join(d, "A_지시서.md"),
           prompt
           .replace("{source_text}", src)
           .replace("{benchmark_block}", _bench_block(bench_rows)))
    print(f"■ 세션 시작 → {d}")
    print("   에이전트가 할 일: A_지시서.md 를 전부 읽고 같은 폴더에 factsheet.json 작성")
    print("   그 다음: python pipeline.py --stage titles")


def recent_persons(days=45):
    """최근 N일 안에 v13이 쓴 인물 목록 (재등장 방지)."""
    cutoff = (datetime.date.today()
              - datetime.timedelta(days=days)).isoformat()
    return {p["person"] for p in _state()["recent_posts"]
            if p.get("date", "") >= cutoff}


def _bench_block(rows) -> str:
    """A 지시서에 넣을 벤치마크 블록.

    ⚠️ 이 블록은 **팩트가 아니다.** 남의 블로그 제목·태그다. A 지시서의 제1
       원칙이 "추출만 한다, 창작·보완 금지" 인데 여기 있는 문장을 근거로
       삼으면 그 원칙이 통째로 무너진다 — 남의 제목에 적힌 주장을 우리 글의
       사실로 옮겨 쓰게 된다.
       그래서 블록 자체에 "정렬 지시일 뿐"이라고 못 박아 넣는다.
       팩트시트에 들어갈 값은 [소스] 에서만 나온다.
    """
    if not rows:
        return "(없음 — 이 슬롯은 기존 방식이다. 소스만 보고 뽑아라.)"
    lines = []
    for i, r in enumerate(rows, 1):
        line = f"[{i}] ♥{r.get('likes')} {r.get('title', '')}"
        tags = [t for t in (r.get("tags") or [])][:8]
        if tags:
            line += f"\n    태그: {', '.join(tags)}"
        lines.append(line)
    return "\n".join(lines)


def stage_auto(celeb, force=False):
    """crawler 로 소스를 자동 구성해 세션을 연다 (하루 10편 운영의 기본 진입점).

    발언(따옴표)·직업 표기 기사를 앞세워 32건을 뽑는다 — 직업 확정과
    quotes 확보가 팩트시트 검증 1의 관문이기 때문이다.

    슬롯이 bench 면(config.SLOT_SOURCE_MODES) 벤치마크에서 지금 반응 있는
    앵글을 뽑아 크롤러 쿼리에 붙이고, 그 근거를 A 지시서에 함께 넣는다.
    벤치마크가 죽어도 그날 글은 나가야 하므로 실패하면 기존 방식으로 간다.
    """
    if celeb in recent_persons():
        print(f"⚠ {celeb} 은 최근 45일 안에 이미 썼습니다. 계속 진행은 하지만 확인할 것.")
    import crawler
    import store
    store.init()

    import config as CFG
    plan = slot_plan(next_slot())
    extra, bench_rows = [], []
    if plan["source_mode"] == "bench":
        print(f"■ {plan['slot']}번 슬롯 = 벤치마크 소재 (5+5 중 벤치마크 쪽)")
        try:
            import benchmark
            rows = benchmark.collect()
            angles = [w for w, _lk, _n in benchmark.angle_words(rows)]
            extra = [f"{celeb} {a}" for a in angles]
            # 그 인물이 벤치마크에 실제로 등장하면 그 글들을, 아니면 전체
            # 상위를 넣는다. 후자여도 값이 있다 — "지금 어떤 앵글이 먹히나"는
            # 인물과 무관하게 성립한다.
            hit = benchmark.for_celeb(celeb, rows)
            bench_rows = (hit or rows)[:CFG.BENCHMARK_PROMPT_ROWS]
            print(f"   앵글: {', '.join(angles) or '(없음)'}")
            print(f"   추가 쿼리 {len(extra)}개 / {celeb} 등장 {len(hit)}편")
        except Exception as e:
            # 벤치마크는 부가 신호다. 죽어도 그날 글은 나가야 한다.
            # 다만 조용히 넘기지 않는다 — 5+5 가 실제로는 10+0 이 돼 있는데
            # 아무도 모르는 상태가 가장 나쁘다.
            print(f"  [경고] 벤치마크 실패({type(e).__name__}: {e}) — "
                  f"이 슬롯은 기존 방식으로 진행합니다.")
    else:
        print(f"■ {plan['slot']}번 슬롯 = 기존 방식 소재 (5+5 중 기존 쪽)")

    items = crawler.collect(celeb, mode="google", extra_queries=extra)
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
    stage_source(tmp, force=force, bench_rows=bench_rows)


def stage_plan(n):
    """오늘 쓸 인물 N명 추천 — 소재 조달 경로를 5+5 로 나눠 보여준다.

    v13 사용 이력(state.json)과 일일 시스템 쿨다운(store.db)을 둘 다 거른다.
    (2026-08-07: 일일이 쓴 김유정을 v13이 다음날 또 쓴 교차 중복 실측 → 보강)

    2026-08-17 사용자 확정 — 5+5:
      pool  슬롯은 예전처럼 풀 순서대로.
      bench 슬롯은 벤치마크에서 지금 좋아요를 받는 인물부터.
    두 목록에서 같은 인물이 겹쳐 나오지 않게 bench 쪽을 먼저 확정한다.
    """
    import config as CFG
    import store
    store.init()
    used = recent_persons()
    avail = [c for c in CFG.CELEB_POOL
             if c not in used and not store.celeb_on_cooldown(c)]
    print(f"■ 후보 {len(avail)}명 (풀 {len(CFG.CELEB_POOL)} - 최근 사용 {len(used)})")

    # 오늘 남은 슬롯. 10번까지 다 찼으면 내일 몫(2번부터)을 보여준다 —
    # 없는 11·12번 슬롯을 만들어내면 5+5 배분이 통째로 pool 로 떨어진다
    # (SLOT_SOURCE_MODES 에 11번이 없어 기본값 pool 이 된다).
    slot = next_slot()
    todo = list(range(slot, 11))[:n]
    if not todo:
        print(f"   오늘 슬롯은 {slot - 1}번까지 다 찼습니다. 아래는 내일 몫입니다.")
        todo = list(range(2, 11))[:n]
    bench_slots = [s for s in todo
                   if CFG.SLOT_SOURCE_MODES.get(s, "pool") == "bench"]
    pool_slots = [s for s in todo if s not in bench_slots]

    ok = set(avail)
    bench_picks = []
    if bench_slots:
        try:
            import benchmark
            rows = benchmark.collect()
            angles = benchmark.angle_words(rows)
            print("\n── 지금 반응 있는 앵글 ──")
            print("   " + " / ".join(f"{w}(♥{lk})" for w, lk, _n in angles))
            bench_picks = [e for e in benchmark.hot_celebs(rows)
                           if e["celeb"] in ok][:len(bench_slots)]
            print(f"\n── 벤치마크 슬롯 {bench_slots} — 지금 반응 있는 인물 ──")
            if not bench_picks:
                print("   (없음 — 벤치마크 상위에 쓸 수 있는 풀 인물이 없다. "
                      "아래 기존 방식 후보로 채워라)")
            for e in bench_picks:
                t = e["posts"][0]["title"][:40] if e["posts"] else ""
                print(f"   ♥{e['likes']:>4}  {e['celeb']}   ← {t}")
        except Exception as e:
            print(f"\n  [경고] 벤치마크 실패({type(e).__name__}: {e}) — "
                  f"벤치마크 슬롯도 기존 방식 후보로 채웁니다.")
            pool_slots = todo

    taken = {e["celeb"] for e in bench_picks}
    rest = [c for c in avail if c not in taken]
    print(f"\n── 기존 방식 슬롯 {pool_slots} — 풀 순서 ──")
    print("   " + (", ".join(rest[:len(pool_slots)]) or "(없음)"))
    print("\n   시작: python pipeline.py --auto <인물명>")
    print("   슬롯 번호가 경로를 정한다 — 순서대로 돌리면 5+5 가 맞는다")


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
              .replace("{title_type}", plan["title_type"])
              .replace("{form_quota}", _form_quota_text()))
    _write(os.path.join(d, "B_지시서.md"), prompt)
    print(f"■ 팩트시트 검증 통과 — {plan['slot']}번 슬롯 / "
          f"포맷 {plan['format']} / 제목 문형 {plan['title_type']}")
    counts = today_title_forms()
    if counts:
        print("   오늘 확정된 문형: "
              + " / ".join(f"{f} {n}편" for f, n in sorted(counts.items(),
                                                           key=lambda x: -x[1])))
    for f, (n, cap) in sorted(blocked_forms().items()):
        print(f"   ⚠ {f} 은 오늘 {n}편으로 상한({cap}편) 소진 — 확정 후보에서 제외된다")
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
    """슬롯 번호 → 포맷·제목 문형·소재 조달 경로. 코드가 강제 주입하는 값이다."""
    import config as CFG
    return {
        "slot": slot,
        "format": CFG.SLOT_FORMATS.get(slot, "A"),
        "title_type": CFG.SLOT_TITLE_TYPES.get(slot, "수치충돌"),
        # pool = 기존 방식 / bench = 벤치마크 소재 (config.SLOT_SOURCE_MODES)
        "source_mode": CFG.SLOT_SOURCE_MODES.get(slot, "pool"),
    }


# ── 문형 하루 상한 ──────────────────────────────────────────────────────

def today_title_forms() -> dict:
    """오늘 이미 확정된 제목들의 문형 개수. {문형: 편수}

    문형은 state.json 에 적힌 값을 믿지 않고 제목에서 **매번 다시 판정**한다.
    상한을 넣기 전에 쌓인 글에는 title_form 키가 아예 없고, 판정 기준을
    고치면 옛 기록과 새 기록이 서로 다른 자로 재어진다.
    제목이 원본이고 문형은 파생값이다 — 파생값을 저장해두고 그걸 세면
    기준을 고친 날 상한이 조용히 어긋난다.

    ⚠️ 슬롯 1(일일 자동 포스팅)은 main.py 경로라 state.json 에 남지 않는다.
       즉 여기서 세는 건 v13 슬롯(2~10)뿐이다. 사용자가 "9편 중 6편"이라고
       센 것과 같은 범위다.
    """
    import titles as TS
    today = _kst_now().date().isoformat()
    out = {}
    for p in _state()["recent_posts"]:
        if p.get("date") != today:
            continue
        f = TS.form_of(p.get("title", ""))
        out[f] = out.get(f, 0) + 1
    return out


def blocked_forms() -> dict:
    """오늘 상한을 다 쓴 문형. {문형: (오늘 편수, 상한)}

    비어 있으면 제약이 없는 것이다. 이 딕셔너리에 든 문형은 그날 남은
    슬롯에서 확정 후보 자격을 잃는다 (stage_body).
    """
    import config as CFG
    counts = today_title_forms()
    caps = getattr(CFG, "TITLE_FORM_DAILY_MAX", {})
    return {f: (counts.get(f, 0), cap) for f, cap in caps.items()
            if counts.get(f, 0) >= cap}


def _form_quota_text() -> str:
    """B 지시서에 넣을 '오늘 남은 문형 자리' 안내.

    이건 **강제가 아니라 안내**다. 강제는 stage_body 가 한다. 남은 자리를
    알려주는 이유는 하나뿐이다 — 안 알려주면 모델이 상한에 걸린 문형으로
    10개를 다 채워오고, 그날 그 슬롯은 통째로 재생성이 된다(왕복 1회 손실).
    """
    import config as CFG
    counts = today_title_forms()
    caps = getattr(CFG, "TITLE_FORM_DAILY_MAX", {})
    if not caps:
        return "(상한 없음)"
    lines = []
    for f, cap in caps.items():
        n = counts.get(f, 0)
        left = cap - n
        if left <= 0:
            lines.append(f"- **{f}: 오늘 {n}편으로 상한({cap}편) 소진.** "
                         f"이 문형으로 쓴 제목은 확정 후보에서 코드가 제외한다 — "
                         f"10개 중 몇 개를 이 문형으로 채우면 그만큼 버려진다")
        else:
            lines.append(f"- {f}: 오늘 {n}편 / 상한 {cap}편 → 남은 자리 {left}편")
    return "\n".join(lines)


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

    # 브릿지 — 글 안에서 2개를 다르게 뽑는 것만으로는 부족하다.
    # 2026-08-16 실측: "문제는 그다음이었습니다" 가 같은 날 5편 중 4편에 깔렸다.
    # 최근 글들이 이미 쓴 것을 빼고 남은 것에서 고른다.
    used_recent = {b for p in _state()["recent_posts"][-6:]
                   for b in (p.get("bridges_used") or [])}
    avail = [b for b in CFG.BRIDGE_POOL if b not in used_recent]
    if len(avail) < CFG.BRIDGE_MIN:          # 풀을 다 돌았으면 처음부터
        avail = list(CFG.BRIDGE_POOL)
    bridges = random.sample(avail, CFG.BRIDGE_MIN)
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
              # 분량은 config 에서만 끌어온다. 프롬프트에 숫자를 박아두면
              # config 를 고쳐도 모델은 옛 숫자를 목표로 쓴다 (0816 실측 사고).
              .replace("{chars_min}", str(CFG.BODY_CHARS_MIN))
              .replace("{chars_max}", str(CFG.BODY_CHARS_MAX))
              .replace("{bridge_picks}", " / ".join(bridges))
              .replace("{connector_pick}", conn))
    _write(os.path.join(d, "C_지시서.md"), prompt)
    _jdump(os.path.join(d, "meta.json"),
           {"title": title, "slot": plan["slot"], "format": plan["format"],
            "source_mode": plan.get("source_mode", ""),
            # cta·closing 은 2026-08-16 사용자 확정으로 폐기됐다.
            # DB 컬럼 호환을 위해 키만 빈 값으로 남긴다.
            "cta": "", "closing": "", "bridges": bridges,
            "connector": conn, "marker": marker})
    print(f"■ 확정 제목: {title}")
    print(f"   슬롯 {plan['slot']} / 포맷 {plan['format']}({fmt['name']})")
    print(f"   주입: 브릿지 「{' / '.join(bridges)}」")
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
        ranked.append({**t, "score": s, "score_reasons": why,
                       "form": TS.form_of(t.get("title", ""))})
    ranked.sort(key=lambda t: (-t["score"], -t.get("heat", 0)))

    # ── 문형 하루 상한 (2026-08-17 사용자 확정) ──────────────────────────
    # score() 는 문형을 보지 않는다. 그래서 그날 가장 잘 먹는 한 문형이
    # 하루 열 편을 통째로 먹는다 (0817 인용형 8편 / 0816 모순형 9편).
    # 상한을 넘긴 문형은 여기서 확정 후보 자격을 잃는다. 점수는 그대로 두고
    # **후보에서 빼는** 방식이다 — 가중치를 건드리면 titles.py 의 실측
    # 캘리브레이션(KNOWN)이 통째로 무너진다.
    blocked = blocked_forms()
    dropped = [t for t in ranked if t["form"] in blocked]
    if blocked:
        print("■ 문형 하루 상한")
        for f, (n, cap) in sorted(blocked.items()):
            print(f"   · {f}: 오늘 이미 {n}편 (상한 {cap}편) → 확정 후보 제외")
        print(f"   후보 10개 중 {len(dropped)}개 제외 / {len(ranked) - len(dropped)}개 남음")
        for t in dropped[:4]:
            print(f"      (제외) {t['score']:>4}점 [{t['form']}] {t['title']}")
    ranked = [t for t in ranked if t["form"] not in blocked]

    if not ranked:
        _fail([f"10개 전부 상한에 걸린 문형이라 확정할 제목이 없다."]
              + [f"   · {f}: 오늘 {n}편 / 상한 {cap}편" for f, (n, cap)
                 in sorted(blocked.items())]
              + ["B_지시서.md 의 [오늘 남은 문형 자리] 를 다시 읽고 10개를 "
                 "다른 문형으로 다시 뽑아라. 상한에 걸린 문형은 한 개도 "
                 "확정될 수 없으니 그 문형으로 채운 자리는 전부 버려진다.",
                 "인용폭로가 막혔으면 큰따옴표를 아예 쓰지 마라 — 발언을 "
                 "따옴표 없이 서술로 풀거나(했다는·라고 한), 수치충돌·부정금지·"
                 "손해경고·제3자반응으로 가라."],
              "제목 문형 상한")

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
    # out_dir 을 먼저 잡는다 — 첫 줄에 출처(날짜·슬롯)를 박아야 하기 때문이다.
    # 2026-08-16: 예비제목.txt 는 홈판 안에 100개 넘게 전부 같은 이름이고 메모장은
    # 탭에 파일명만 보여준다. 발행 끝난 슬롯 폴더는 지워지는데 메모장은 세션 복원으로
    # 그 탭을 계속 들고 있어서, 이미 사라진 어제 파일이 오늘 것처럼 떠 있다.
    # 0번 본문.txt 에는 넣지 않는다 — 통째로 복사해 발행하는 파일이다.
    out_dir = _alloc_outdir(fs["person"]["name"])
    _p = os.path.abspath(out_dir).replace("\\", "/").split("/")
    _o = f"패션비버 {_p[-2]}-{_p[-1]}" if len(_p) >= 2 else "패션비버"
    lines = [f"[{_o} · 확정]", "", top["title"], "", "─── 예비 ───"]
    for t in ranked[1:]:
        lines += ["", t["title"]]
    # 상한에 걸려 빠진 제목도 보여준다. 안 보여주면 "왜 이게 확정됐지"를
    # 사람이 알 수 없고, 없는 셈 치면 예비 번호(--retitle N)와도 어긋난다.
    if dropped:
        lines += ["", f"─── 제외 · 문형 상한 ({', '.join(sorted(blocked))}) ───"]
        for t in dropped:
            lines += ["", f"[{t['form']}] {t['title']}"]
    _write(os.path.join(out_dir, "예비제목.txt"), "\n".join(lines) + "\n")
    # ranked.json 은 상한을 통과한 것만 담는다 — --retitle <번호> 가 이 순서를
    # 그대로 센다. 제외분은 따로 남겨 추적만 가능하게 둔다.
    _jdump(os.path.join(d, "ranked.json"), ranked)
    if dropped:
        _jdump(os.path.join(d, "ranked_dropped.json"), dropped)
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
        # 사람이 직접 쓴 제목은 상한으로 막지 않는다. 최종 발행은 사람이 하고,
        # 상한은 자동 확정이 한 문형으로 쏠리는 걸 막으려고 둔 것이다.
        # 다만 조용히 넘기지는 않는다 — 모르고 넘기면 상한이 무의미해진다.
        import titles as TS
        form = TS.form_of(title)
        cap = blocked_forms().get(form)
        if cap:
            print(f"⚠ 이 제목은 문형이 '{form}' 인데 오늘 이미 {cap[0]}편으로 "
                  f"상한({cap[1]}편)을 넘겼습니다. 사람이 직접 정한 제목이라 "
                  f"진행은 합니다.")
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
    # 오늘 이미 나온 다른 슬롯들의 도입 3줄 — §2 중복 검사용
    _today = _kst_now().date().isoformat()
    same_day_intros = [p.get("intro", "") for p in st["recent_posts"]
                       if p.get("date") == _today
                       and p.get("title") != meta["title"] and p.get("intro")]
    errs = V.validate_body(
        body, fs,
        {"recent_endings": [p.get("ending_used") for p in
                            st["recent_posts"][-3:] if p.get("ending_used")]},
        fmt=meta.get("format", ""),     # 슬롯이 정한 포맷 → 도입 문형 판정
        same_day_intros=same_day_intros)

    # 유사문서 검사 — 이미 발행한 글과 겹치면 네이버가 검색에서 걷어낸다.
    # (2026-08-16 사용자 확정: "중복유사문서로 판명나면 절대 안된다")
    import dedup
    errs += dedup.check(meta["title"], body, st["recent_posts"])

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
        "slot": meta.get("slot"),
        "format": meta.get("format", ""),
        # 도입 3줄을 남긴다 — 다음 슬롯이 이것과 겹치는지 대조한다 (§2)
        "intro": " ".join(V.intro_lines(body)),
        # 유사문서 지문 — 본문 전체가 아니라 서명·문장해시·제목골격만 남긴다
        "fp": __import__("dedup").fingerprint(title, body),
        # 브릿지 로테이션용. 예전엔 저장을 안 해서 슬롯끼리 겹쳤다
        # (2026-08-16 실측: "문제는 그다음이었습니다" 가 5편 중 4편에 깔렸다)
        "bridges_used": meta.get("bridges", []),
        # 문형 — 기록용이다. 하루 상한은 이 값을 세지 않고 제목에서 매번 다시
        # 판정한다 (today_title_forms 의 주석 참고). 여기 남기는 건 "그날 뭐가
        # 몇 편이었나"를 나중에 눈으로 확인하려고.
        "title_form": __import__("titles").form_of(title),
        # 소재를 어디서 골랐나 (pool=기존 방식 / bench=벤치마크). 5+5 배분이
        # 실제로 지켜졌는지 이 값으로 센다.
        "source_mode": meta.get("source_mode", ""),
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
