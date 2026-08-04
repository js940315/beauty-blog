# -*- coding: utf-8 -*-
"""카테고리별 사진 라이브러리를 미리 채워두는 도구.

매일 아침 기사마다 사진을 새로 검색하면 (1) 느리고 (2) 429에 걸리고
(3) 무관한 사진이 걸려도 아무도 못 잡는다. 그래서 미리 카테고리별로 쌓아두고
매일은 '고르기만' 하는 구조로 간다.

    python build_photo_library.py            # 전체 카테고리
    python build_photo_library.py 수출무역    # 특정 카테고리만

받은 뒤 Read 도구로 눈으로 확인하고, 주제에 안 맞는 파일은 지운다.
지우면 index.json 도 같이 정리해야 하므로 --prune 으로 정리한다.

소스는 Wikimedia Commons 직접 경로만 쓴다:
  - Openverse는 rawpixel 등이 축소본(1024px)만 줘서 카드(1080px)에 못 쓴다
  - korea.kr(정책브리핑)은 720px 상한 + 항목별 공공누리 부착 여부 확인 필요라 제외
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from image_sourcing import (prepare_photo, wikimedia_photo_candidates,
                            photo_candidates, stock_photo_candidates)

DIR = os.path.join("assets", "photos")
INDEX = os.path.join(DIR, "index.json")

# 카테고리 -> 검색어. 검색어가 전부 영문인 이유는 Commons/Openverse/스톡 3사의
# 인덱스가 영문 기반이라 한글 쿼리는 적중률이 급락하기 때문이다(car-blog 실측 교훈).
# Openverse는 1~2단어 짧은 쿼리에서 결과가 잘 나온다 — 3단어 넘어가면 0건이 잦다.
#
# ⚠️ 인물 사진은 이 라이브러리에 넣지 않는다. 연예인 초상은 소싱 대상이 아니다.
#    (초상권은 사진가의 저작권과 별개다. 실명을 안 쓰는 이 시스템의 설계와도 일치.)
CATEGORIES = {
    # --- 소재 버킷 10종 — 인물 없는 소재컷만 (사용자 확정) -------------------
    # 사람이 크게 나오는 컷은 쓰지 않으므로 쿼리에 woman/model 류를 넣지 않는다.
    # 단일 피사체라 스톡 적중률이 가장 높다. config.FOOD_BUCKETS_ONLY 와 1:1.
    "화장품": ["cosmetics makeup products flatlay", "makeup products table"],
    "립스틱": ["lipstick collection closeup", "red lipstick product"],
    "스킨케어": ["skincare cream jar product", "serum bottle skincare"],
    "향수": ["perfume bottle elegant", "fragrance bottle glass"],
    "화장대": ["vanity table makeup mirror", "dressing table cosmetics"],
    "가방": ["luxury handbag leather", "designer handbag display"],
    "구두": ["high heels shoes elegant", "women heels product"],
    "주얼리": ["jewelry necklace earrings", "gold jewelry closeup"],
    "옷장": ["wardrobe clothes rack", "closet fashion clothing"],
    "드레스": ["evening gown dress hanger", "elegant dress boutique"],
}


def load_index():
    # 손상(빈/NUL) JSON을 만나도 크래시 대신 {}로 복구(불안정 환경·블루스크린 대비)
    if os.path.exists(INDEX):
        try:
            with open(INDEX, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            print("  [경고] index.json 손상 — 빈 인덱스로 복구")
            return {}
    return {}


def save_index(idx):
    # 원자적 저장: 임시파일에 다 쓴 뒤 os.replace로 교체 → 쓰는 중 크래시해도 원본 안 깨짐
    os.makedirs(os.path.dirname(INDEX) or ".", exist_ok=True)
    tmp = INDEX + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, INDEX)


def prune(idx):
    """파일이 지워진 항목을 index에서 정리한다."""
    gone = [k for k in idx if not k.startswith("_") and
            not os.path.exists(os.path.join(DIR, k))]
    for k in gone:
        idx.pop(k)
    return gone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("category", nargs="?", help="특정 카테고리만 (생략 시 전체)")
    ap.add_argument("--per", type=int, default=2, help="검색어당 채택 수")
    ap.add_argument("--min-width", type=int, default=1600)
    ap.add_argument("--sources", default="wiki,openverse,stock",
                    help="쉼표구분: wiki,openverse,stock — 다양한 소스에서 모은다. "
                         "stock(Pexels/Unsplash/Pixabay)은 환경변수 API키가 있어야 동작.")
    ap.add_argument("--workers", type=int, default=8,
                    help="동시 다운로드/가공 스레드 수 (병목 제거용, 기본 8)")
    ap.add_argument("--prune", action="store_true", help="index 정리만 하고 종료")
    ap.add_argument("--register", action="store_true",
                    help="수동으로 넣은 사진을 index에 등록(카테고리=파일명 '_' 앞부분). "
                         "속보용 프레스 사진을 직접 넣을 때 사용.")
    args = ap.parse_args()

    os.makedirs(DIR, exist_ok=True)
    idx = load_index()

    if args.prune:
        gone = prune(idx)
        save_index(idx)
        print(f"index에서 {len(gone)}건 정리: {gone}")
        return 0

    if args.register:
        # 사용자가 assets/photos/ 에 직접 넣은(속보용 프레스 등) 사진을 index에 등록한다.
        # 카테고리는 파일명의 첫 '_' 앞부분에서 뽑는다. 예: GV90_press_0.jpg -> 카테고리 "GV90".
        # 정사각 1400px로 맞춰 다른 사진과 규격을 통일한다.
        added = 0
        for fn in sorted(os.listdir(DIR)):
            if fn == "index.json" or not fn.lower().endswith((".jpg", ".png")):
                continue
            if fn in idx:
                continue
            cat = os.path.splitext(fn)[0].split("_")[0]  # 확장자 먼저 떼고(GV90.jpg→GV90)
            path = os.path.join(DIR, fn)
            try:
                prepare_photo(path, path, size=1400)
            except Exception as e:
                print(f"   가공 실패 {fn}: {str(e)[:40]}")
            idx[fn] = {
                "카테고리": cat,
                "license": "수동추가(사용자 책임)",
                "credit": "",
                "attribution_required": False,
                "처리": "수동 등록 + 정사각 1400px",
                "검색어": "manual",
                "검수": "수동추가 — 사용자 확인함",
            }
            print(f"   등록  {fn:<26} -> 카테고리 '{cat}'")
            added += 1
        save_index(idx)
        print(f"\n{added}장 등록 완료. (git add -f 로 커밋하세요)")
        return 0

    targets = ({args.category: CATEGORIES[args.category]}
               if args.category else CATEGORIES)
    if args.category and args.category not in CATEGORIES:
        print("없는 카테고리입니다. 가능한 값:", ", ".join(CATEGORIES))
        return 1

    src_names = [s.strip() for s in args.sources.split(",") if s.strip()]
    total_ok = total_skip = 0

    # 병목 제거: (카테고리 × 검색어 × 소스)를 전부 독립 작업으로 만들어
    # 네트워크 다운로드를 스레드풀로 동시에 돌린다. 예전엔 버킷마다 프로세스를
    # 새로 띄우고, 소스도 순차, 다운로드마다 1초씩 쉬느라 느렸다.
    jobs = [(cat, qi, q, sname)
            for cat, queries in targets.items()
            for qi, q in enumerate(queries)
            for sname in src_names]

    def source_one(job):
        cat, qi, q, sname = job
        sprefix = f"{cat}_{qi}{sname[0]}"
        try:
            if sname == "wiki":
                rep = wikimedia_photo_candidates(
                    q, DIR, limit=args.per, min_width=args.min_width, prefix=sprefix)
            elif sname == "openverse":
                rep = photo_candidates(
                    q, DIR, limit=args.per, min_width=args.min_width, prefix=sprefix)
            elif sname == "stock":
                rep = stock_photo_candidates(
                    q, DIR, keep=args.per, min_side=args.min_width, prefix=sprefix)
            else:
                print(f"   [{sname}] 알 수 없는 소스 — 건너뜀")
                rep = []
        except Exception as e:
            print(f"   [{sname}/{cat}] 소싱 실패: {str(e)[:60]}")
            rep = []
        return cat, q, rep

    # 1단계: 다운로드(네트워크) 병렬
    downloaded = []   # (cat, q, r)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for cat, q, rep in ex.map(source_one, jobs):
            for r in rep:
                if "path" in r:
                    downloaded.append((cat, q, r))
                elif "skipped" in r:
                    total_skip += 1

    # 2단계: 카드 규격(정사각 1400px) 가공도 병렬 (CPU/IO)
    def prep_one(item):
        _, _, r = item
        try:
            prepare_photo(r["path"], r["path"], size=1400)
        except Exception as e:
            print(f"   가공 실패 {os.path.basename(r['path'])}: {str(e)[:40]}")
        return item

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        prepared = list(ex.map(prep_one, downloaded))

    # 3단계: 인덱스 기록(직렬 — 공유 dict 안전)
    for cat, q, r in prepared:
        fname = os.path.basename(r["path"])
        idx[fname] = {
            "카테고리": cat,
            "license": r["license"],
            "credit": r["credit"],
            "attribution_required": True,
            "원본해상도": f"{r.get('src_w') or r.get('w')}x{r.get('src_h') or r.get('h')}",
            "처리": "정사각 1400px + 시리즈 톤 + 비네트",
            "검색어": q,
            "검수": "미확인 — Read로 눈 확인 필요",
        }
        print(f"   OK   {fname:<26} [{r['license']}]")
        total_ok += 1

    save_index(idx)
    print(f"\n채택 {total_ok}장 / 폐기 {total_skip}건 -> {DIR}/")
    print("이제 Read 도구로 각 파일을 열어 주제에 맞는지 확인하세요.")
    print("안 맞는 건 파일 삭제 후: python build_photo_library.py --prune")
    return 0


if __name__ == "__main__":
    sys.exit(main())
