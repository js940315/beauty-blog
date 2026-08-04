"""팩트시트 → 실물 사진 N장.

카드뉴스(텍스트 얹은 이미지)는 쓰지 않는다. 실물 사진 그대로 간다.
하는 일은 세 가지뿐이다: 어느 버킷을 쓸지 고르고 → 순환 선택하고 → 정사각으로 자른다.

⚠️ 연예인 인물 사진은 쓰지 않는다. 초상권은 사진가의 저작권과 별개이고,
   이 시스템은 실명 자체를 안 쓰는 설계다. 사진은 음식·운동·공간 소재만.
"""

import os

import config as C
import photo_library as PL
from image_sourcing import prepare_photo

# 팩트시트의 한국어 뷰티·패션 표현 → 사진 버킷
# 이름은 FOOD_BUCKETS 그대로 둔다 — 내용만 뷰티 버킷으로 갈았다.
FOOD_BUCKETS = {
    "립스틱": ("립스틱", "립", "틴트", "립글로스"),
    "화장품": ("화장품", "쿠션", "파운데이션", "메이크업", "아이섀도", "화장"),
    "스킨케어": ("스킨케어", "크림", "세럼", "앰플", "토너", "선크림", "클렌징", "피부"),
    "향수": ("향수", "퍼퓸",),
    "화장대": ("화장대", "브러시",),
    "가방": ("가방", "핸드백", "명품", "백"),
    "구두": ("구두", "힐", "하이힐", "슈즈", "신발"),
    "주얼리": ("주얼리", "귀걸이", "목걸이", "반지", "팔찌", "시계"),
    "옷장": ("옷장", "코디", "스타일링",),
    "드레스": ("드레스", "원피스", "시상식", "가운"),
}

def _match_buckets(texts, table):
    """팩트시트에 언급된 아이템과 맞는 버킷을 언급 순서대로."""
    joined = " ".join(t for t in texts if t)
    return [b for b, keys in table.items() if any(k in joined for k in keys)]


def choose_buckets(fs: dict, date_tag: str = ""):
    """슬롯별 photo_category. 1번이 대표 사진(홈판 썸네일로 쓰인다).

    ⚠️ 인물 없는 소재 버킷만 쓴다. 사람이 크게 나오는 컷은 글과 따로 놀고,
       초상 문제 소지도 있다.
       팩트시트에 아이템 언급이 없으면 기본 버킷을 날짜에 따라 돌려 쓴다.
    """
    picked = [b for b in _match_buckets(fs.get("foods") or [], FOOD_BUCKETS)
              if b in C.FOOD_BUCKETS_ONLY]

    # 남는 슬롯은 기본 소재 버킷에서 채운다. 날짜로 시작점을 밀어
    # 매일 같은 조합이 나오지 않게 한다.
    pool = list(C.FOOD_BUCKETS_ONLY)
    if pool:
        offset = sum(ord(c) for c in date_tag) % len(pool)
        pool = pool[offset:] + pool[:offset]
    for b in pool:
        if len(picked) >= C.IMAGE_COUNT:
            break
        if b not in picked:
            picked.append(b)
    return picked[: C.IMAGE_COUNT]


def _focus_from(align: str) -> str:
    """variation_seed의 9방향 정렬을 PIL 크롭 기준으로 옮긴다."""
    if "YMin" in align:
        return "top"
    if "YMax" in align:
        return "bottom"
    return "center"


def render(title, fs, date_tag, outdir):
    """실물 사진 세트를 만든다. 반환: [{slot, category, photo, file}]"""
    buckets = choose_buckets(fs, date_tag)
    used, made = set(), []

    for seq, category in enumerate(buckets, 1):
        fname = PL.pick_photo(category, date_tag, seq, exclude=used)
        if not fname:
            print(f"  [건너뜀] {seq}번 — '{category}' 버킷에 사진이 없습니다.")
            continue
        src = os.path.join(PL.PHOTO_DIR, fname)
        if not os.path.exists(src):
            print(f"  [건너뜀] {seq}번 — 파일 없음: {fname}")
            continue

        used.add(fname)
        var = PL.variation_seed(fname, date_tag, seq)
        dst = os.path.join(outdir, f"{seq}번 사진.jpg")
        try:
            prepare_photo(src, dst, size=C.IMAGE_SIZE,
                          focus=_focus_from(var["align"]),
                          grade=C.PHOTO_GRADE, vignette=C.PHOTO_VIGNETTE)
        except Exception as e:
            print(f"  [실패] {seq}번 가공 실패: {str(e)[:60]}")
            continue

        PL.record_usage(category, fname, date_tag, seq)
        made.append({"slot": seq, "category": category,
                     "photo": fname, "file": os.path.basename(dst)})
        print(f"  {seq}번 사진.jpg  [{category}] {fname}")

    if len(made) < C.IMAGE_COUNT:
        print(f"  [알림] 소재 사진 {len(made)}장 (목표 {C.IMAGE_COUNT}장). "
              "모자라면 모자란 대로 나갑니다.")
    if not made:
        print("         버킷을 채우려면: python build_photo_library.py --per 3")
    return made


if __name__ == "__main__":
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("아이템 언급 있음:", choose_buckets(
        {"foods": ["립스틱", "명품 가방"], "exercises": ["요가"]}, "0804"))
    print("아이템 언급 없음:", choose_buckets({"foods": [], "exercises": []}, "0804"))
    print("다른 날짜    :", choose_buckets({"foods": [], "exercises": []}, "0806"))
    print("\n버킷 외 항목이 섞이지 않는지 확인 — 위 결과가 전부 FOOD_BUCKETS_ONLY 안이어야 한다")
