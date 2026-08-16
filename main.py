"""매일 한 편. 크롤링 → 팩트시트 → 제목 확정 → 본문 → output/날짜/

산출물 폴더는 아침에 열었을 때 복붙할 것만 보이게 둔다.
  output/날짜/완성본.txt   ← 이것만 복사하면 된다
  output/날짜/1번 사진.jpg …
  output/날짜/_작업/          ← 팩트시트·제목점수·소스 등 중간 산출물

경로는 두 개다.

A) API 경로 — ANTHROPIC_API_KEY 가 있을 때. 한 번에 끝난다.
     python main.py --dry-run        제목까지만 (첫 실행 권장)
     python main.py                  풀 생성
     python main.py --celeb 김희선   인물 지정
     python main.py --sort date      최신 기사 위주

B) 에이전트 경로 — 키가 없을 때. 클로드 코드가 LLM 단계를 맡는다.
     python main.py --stage crawl    수집 → 1_팩트시트_지시서.md
       (에이전트가 factsheet.json + title_candidates.json 을 쓴다)
     python main.py --stage title    제목 확정 → 2_본문_지시서.md
       (에이전트가 body.txt 를 쓴다)
     python main.py --stage finish   검증 → 완성본.txt + 사진

어느 쪽이든 수집·제목 점수·형식 검증은 파이썬이 한다. LLM은 글만 쓴다.
"""

import argparse
import json
import os
import sys
import traceback

import brief
import config as C
import crawler
import store
import titles as T
import validator


def _utf8_stdout():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def pick_celeb(explicit=None):
    if explicit:
        if store.celeb_on_cooldown(explicit):
            print(f"[주의] {explicit}은(는) 쿨다운 중입니다 "
                  f"({C.CELEB_COOLDOWN_DAYS}일). 지정했으니 그대로 진행합니다.")
        return explicit
    pool = store.available_celebs()
    if not pool:
        raise SystemExit(
            f"사용 가능한 인물이 없습니다. 전원 {C.CELEB_COOLDOWN_DAYS}일 쿨다운 중입니다.\n"
            "config.py의 CELEB_POOL을 늘리거나 CELEB_COOLDOWN_DAYS를 줄이세요."
        )
    return pool[0]


def outdir(date_str):
    """복붙할 것만 놓는 폴더. 본문 1개 + 사진 몇 장.

    2026-08-06 사용자 확정: 모든 블로그 공통 output/MMDD/번호/ 구조.
    일일 자동 포스팅은 항상 1번 방이다 (v13은 2번부터 — pipeline.py 참조).
    """
    mmdd = date_str.replace("-", "")[4:]
    d = os.path.join(C.OUTPUT_DIR, mmdd, "1")
    os.makedirs(d, exist_ok=True)
    return d


def wpath(d, name):
    """중간 산출물 경로. output 폴더에는 본문과 사진만 남긴다."""
    return os.path.join(brief.workdir(d), name)


def _dump(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)


def _load(path, what):
    if not os.path.exists(path):
        raise SystemExit(f"{what}가 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_text(path, what):
    if not os.path.exists(path):
        raise SystemExit(f"{what}가 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()




def _finalize(d, body, richness, meta, chosen, polish_ok):
    """검증 → 완성본.txt → 사진 → report.json. 두 경로가 공유한다."""
    cta = meta.get("cta")
    celeb = meta.get("celeb")
    problems = validator.validate(body, richness, cta, celeb, chosen["title"])
    polished = False
    if problems and polish_ok:
        body = validator.polish(body)
        polished = True
        problems = validator.validate(body, richness, cta, celeb, chosen["title"])

    # 제휴 표시·건강 면책은 polish를 안 거쳐도 반드시 붙어야 한다
    body = validator.ensure_notices(body)

    post = chosen["title"] + "\n" + C.BLANK_LINE + "\n" + body
    _dump(os.path.join(d, C.POST_FILENAME), post)

    made = []
    # IMAGE_COUNT=0 이면 사진 단계 자체를 건너뛴다 (2026-08-07 사용자 확정: 스탠바이 무이미지)
    if not meta.get("no_images") and C.IMAGE_COUNT > 0:
        print("■ 사진 준비...")
        try:
            import images
            fs = _load(wpath(d, "factsheet.json"), "팩트시트")
            made = images.render(chosen["title"], fs,
                                 meta["date"].replace("-", "")[4:], d)
        except Exception as e:
            print(f"   [경고] 사진 준비 실패 — 본문은 정상입니다: {str(e)[:80]}")

    # 예비제목.txt — v13과 동일하게 일일 편에도 제목 후보를 남긴다
    # (2026-08-10 사용자 지적: 1번 방만 예비제목이 없었음. 형식은 제목만, 빈 줄 간격)
    ranked_path = wpath(d, "titles.json")
    if os.path.exists(ranked_path):
        with open(ranked_path, encoding="utf-8") as f:
            _ranked = json.load(f)
        # 첫 줄에 출처(블로그·날짜·슬롯)를 박는다. 2026-08-16: 예비제목.txt 는
        # 홈판 안에 100개 넘게 전부 같은 이름이고 메모장은 탭에 파일명만 보여준다.
        # 발행 끝난 슬롯 폴더는 지워지는데 메모장은 세션 복원으로 그 탭을 계속
        # 들고 있어서, 이미 사라진 어제 파일이 오늘 것처럼 떠 있다.
        # 0번 본문.txt 에는 넣지 않는다 — 통째로 복사해 발행하는 파일이다.
        _p = os.path.abspath(d).replace("\\", "/").split("/")
        _o = f"패션비버 {_p[-2]}-{_p[-1]}" if len(_p) >= 2 else "패션비버"
        lines = [f"[{_o} · 확정]", "", chosen["title"], "", "─── 예비 ───"]
        for t in _ranked:
            if t["title"] != chosen["title"] and t.get("score", 0) > -900:
                lines += ["", t["title"]]
        with open(os.path.join(d, "예비제목.txt"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")

    cl = len(validator.content_lines(validator.split_sections(body)[0]))
    return post, problems, polished, made, cl


# ── A) API 경로 ─────────────────────────────────────────────────────────

def run_api(args):
    import writer

    store.init()
    date_str = store.today_str()
    d = outdir(date_str)

    if already_done(d, args.force):
        return

    celeb = pick_celeb(args.celeb)
    print(f"■ 인물: {celeb}   (날짜 {date_str} KST)")

    print("■ 수집 중...")
    items = crawler.collect(celeb, sort=args.sort, mode=args.source)
    if not items:
        raise SystemExit("소스를 하나도 못 모았습니다. 인물을 바꾸거나 --sort date로 시도하세요.")
    richness = crawler.assess_richness(items)
    print(f"   {len(items)}건 수집 / richness={richness}")

    print("■ 팩트시트 추출...")
    fs = writer.build_factsheet(celeb, crawler.summarize(items))
    fs_text = writer.factsheet_text(fs)
    _dump(wpath(d, "factsheet.json"), fs)
    print(f"   아이템 {len(fs.get('foods') or [])}개 / 루틴 {len(fs.get('exercises') or [])}개 "
          f"/ 발언 {len(fs.get('quotes') or [])}개")

    print("■ 제목 후보 생성...")
    candidates = writer.generate_titles(fs_text)
    ranked = T.pick(candidates)
    _dump(wpath(d, "titles.json"), ranked)

    print(f"■ 제목 점수 ({len(ranked)}개)")
    for r in ranked:
        print(f"   {r['score']:>5}  {r['title']}")
    if not ranked or ranked[0]["score"] <= 0:
        raise SystemExit("쓸 만한 제목이 없습니다. 후킹 근거가 부족하거나 전부 탈락했습니다.")

    chosen = ranked[0]
    print(f"■ 확정: {chosen['title']}  ({chosen['score']}점)")
    for why in chosen["reasons"]:
        print(f"      - {why}")

    if args.dry_run:
        print("\n(--dry-run 이므로 본문은 만들지 않습니다)")
        _dump(wpath(d, "report.json"), {
            "date": date_str, "celeb": celeb, "richness": richness,
            "sources": len(items), "title": chosen, "dry_run": True,
        })
        return

    # cta·closing 은 2026-08-16 사용자 확정으로 폐기됐다.
    # DB 컬럼 호환을 위해 키만 빈 값으로 남긴다 (pipeline.py와 동일 패턴).
    cta = ""
    print("■ 본문 생성...")
    heart = C.CLOSING_POOL[int(date_str.replace("-", "")) % len(C.CLOSING_POOL)]
    body, history, polished = writer.generate_body(
        chosen["title"], fs_text, richness, cta,
        bool(fs.get("has_exercise_detail")), celeb, heart,
    )

    meta = {"date": date_str, "celeb": celeb, "cta": cta, "no_images": args.no_images}
    post, problems, extra_polish, made, cl = _finalize(
        d, body, richness, meta, chosen, polish_ok=False)

    _dump(wpath(d, "report.json"), {
        "date": date_str, "celeb": celeb, "richness": richness,
        "sources": len(items), "title": chosen, "cta": cta,
        "content_lines": cl, "polished": polished or extra_polish,
        "repair_history": history, "final_problems": problems,
        "images": made,
    })

    store.mark_celeb(celeb)
    store.mark_articles(items)
    store.record_post(date_str, celeb, chosen["title"], cta)

    print(f"■ 완료 → {os.path.join(d, C.POST_FILENAME)}")
    print(f"   내용 줄 {cl}개 / 사진 {len(made)}장 / 남은 위반 {len(problems)}건"
          + ("  (polish 강제복구 적용)" if polished else ""))
    for p in problems:
        print(f"      · {p}")


# ── B) 에이전트 경로 ────────────────────────────────────────────────────

def already_done(d, force):
    """그 날짜에 완성된 글이 이미 있으면 건드리지 않는다.

    루틴이 매일 같은 날짜 폴더에 쓰는데, 사람이 미리 만들어둔 글이 있으면
    말없이 덮어쓴다. 손으로 다듬은 원고가 새벽에 사라지는 사고가 난다.
    """
    post = os.path.join(d, C.POST_FILENAME)
    if force or not os.path.exists(post):
        return False
    print(f"■ 이미 오늘자 글이 있습니다 → {post}")
    print("   덮어쓰지 않고 종료합니다. 다시 만들려면 --force 를 붙이세요.")
    return True


def stage_crawl(args):
    store.init()
    date_str = args.date or store.today_str()
    d = outdir(date_str)
    if already_done(d, args.force):
        return

    celeb = pick_celeb(args.celeb)
    print(f"■ 인물: {celeb}   (날짜 {date_str} KST)")

    print("■ 수집 중...")
    items = crawler.collect(celeb, sort=args.sort, mode=args.source)
    if not items:
        raise SystemExit("소스를 하나도 못 모았습니다. 인물을 바꾸거나 --sort date로 시도하세요.")
    richness = crawler.assess_richness(items)
    # cta·closing 은 2026-08-16 사용자 확정으로 폐기됐다.
    # DB 컬럼 호환을 위해 키만 빈 값으로 남긴다 (pipeline.py와 동일 패턴).
    cta = ""
    print(f"   {len(items)}건 수집 / richness={richness}")

    _dump(wpath(d, "sources.json"), items)
    heart = C.CLOSING_POOL[int(date_str.replace("-", "")) % len(C.CLOSING_POOL)]
    _dump(wpath(d, "meta.json"), {
        "date": date_str, "celeb": celeb, "richness": richness,
        "sources": len(items), "cta": cta, "heart": heart,
        "no_images": args.no_images,
    })
    path = brief.factsheet_brief(d, celeb, richness,
                                 crawler.summarize(items), len(items))

    print(f"■ 1단계 지시서 → {path}")
    print("   에이전트가 할 일: factsheet.json + title_candidates.json 작성")
    print("   그 다음: python main.py --stage title")


def stage_title(args):
    store.init()
    date_str = args.date or store.today_str()
    d = outdir(date_str)

    meta = _load(wpath(d, "meta.json"), "meta.json (--stage crawl 먼저)")
    fs = _load(wpath(d, "factsheet.json"), "factsheet.json (에이전트가 써야 합니다)")
    cand = _load(wpath(d, "title_candidates.json"), "title_candidates.json")
    candidates = cand.get("titles") if isinstance(cand, dict) else cand

    ranked = T.pick(candidates or [])
    _dump(wpath(d, "titles.json"), ranked)

    print(f"■ 제목 점수 ({len(ranked)}개)")
    for r in ranked:
        print(f"   {r['score']:>5}  {r['title']}")
    if not ranked or ranked[0]["score"] <= 0:
        raise SystemExit("쓸 만한 제목이 없습니다. 후킹 근거가 부족하거나 전부 탈락했습니다.")

    chosen = ranked[0]
    print(f"■ 확정: {chosen['title']}  ({chosen['score']}점)")
    for why in chosen["reasons"]:
        print(f"      - {why}")

    heart = meta.get("heart") or C.CLOSING_POOL[
        int(date_str.replace("-", "")) % len(C.CLOSING_POOL)]
    path = brief.body_brief(d, chosen["title"], brief.factsheet_text(fs),
                            meta["richness"], meta["cta"],
                            bool(fs.get("has_exercise_detail")),
                            meta["celeb"], heart)
    print(f"■ 2단계 지시서 → {path}")
    print("   에이전트가 할 일: body.txt 작성")
    print("   그 다음: python main.py --stage finish")


def stage_finish(args):
    store.init()
    date_str = args.date or store.today_str()
    d = outdir(date_str)

    meta = _load(wpath(d, "meta.json"), "meta.json (--stage crawl 먼저)")
    ranked = _load(wpath(d, "titles.json"), "titles.json (--stage title 먼저)")
    body = _load_text(wpath(d, "body.txt"), "body.txt (에이전트가 써야 합니다)")
    chosen = ranked[0]
    if args.no_images:
        meta["no_images"] = True

    post, problems, polished, made, cl = _finalize(
        d, body, meta["richness"], meta, chosen, polish_ok=args.polish)

    _dump(wpath(d, "report.json"), {
        "date": meta["date"], "celeb": meta["celeb"], "richness": meta["richness"],
        "sources": meta["sources"], "title": chosen, "cta": meta["cta"],
        "content_lines": cl, "polished": polished,
        "route": "agent", "final_problems": problems, "images": made,
    })

    if problems:
        note = brief.repair_note(d, problems)
        print(f"■ 형식 위반 {len(problems)}건 — 본문 파일은 만들었지만 아직 복붙하면 안 됩니다.")
        for p in problems[:12]:
            print(f"      · {p}")
        print(f"   위반 목록 → {note}")
        print("   body.txt 를 고쳐서 --stage finish 를 다시 실행하세요.")
        sys.exit(4)

    # 통과했으면 지난 회차의 위반 목록을 남겨두지 않는다.
    # 고쳐서 통과했는데 파일이 그대로 있으면 아침에 아직 문제가 있는 줄 안다.
    stale = wpath(d, "위반목록.md")
    if os.path.exists(stale):
        os.remove(stale)

    items = []
    if os.path.exists(wpath(d, "sources.json")):
        items = _load(wpath(d, "sources.json"), "sources.json")
    store.mark_celeb(meta["celeb"])
    store.mark_articles(items)
    store.record_post(meta["date"], meta["celeb"], chosen["title"], meta["cta"])

    print(f"■ 완료 → {os.path.join(d, C.POST_FILENAME)}")
    print(f"   내용 줄 {cl}개 / 사진 {len(made)}장 / 위반 0건"
          + ("  (polish 강제복구 적용)" if polished else ""))


def main():
    _utf8_stdout()
    ap = argparse.ArgumentParser(description="네이버 뷰티·패션 블로그 자동 생성")
    ap.add_argument("--stage", choices=("crawl", "title", "finish"),
                    help="에이전트 경로. 생략하면 API 경로(키 필요)")
    ap.add_argument("--dry-run", action="store_true", help="제목까지만 생성 (API 경로)")
    ap.add_argument("--celeb", help="인물 지정")
    ap.add_argument("--sort", choices=("sim", "date"), default=C.DEFAULT_SORT,
                    help="sim=정확도(기본) / date=최신. 네이버 경로에서만 의미 있음")
    ap.add_argument("--source", choices=("auto", "naver", "google"), default=C.SOURCE_MODE,
                    help="수집 경로. auto=네이버 키 없으면 구글 뉴스")
    ap.add_argument("--no-images", action="store_true", help="본문만 생성 (사진 생략)")
    ap.add_argument("--polish", action="store_true",
                    help="finish에서 형식 위반을 기계적으로 강제 복구 (최후 수단)")
    ap.add_argument("--date", help="작업 폴더 날짜 지정 (YYYY-MM-DD). 기본은 오늘")
    ap.add_argument("--force", action="store_true",
                    help="그 날짜에 완성된 본문이 있어도 새로 만든다")
    args = ap.parse_args()

    try:
        if args.stage == "crawl":
            stage_crawl(args)
        elif args.stage == "title":
            stage_title(args)
        elif args.stage == "finish":
            stage_finish(args)
        else:
            run_api(args)
    except crawler.NaverKeyMissing as e:
        print(f"[중단] {e}")
        sys.exit(2)
    except SystemExit:
        raise
    except Exception as e:
        if type(e).__name__ == "RefusalError":
            print(f"[중단] {e}\n소재를 바꾸거나 다른 인물로 시도하세요.")
            sys.exit(3)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
