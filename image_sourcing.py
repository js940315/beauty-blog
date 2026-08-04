# -*- coding: utf-8 -*-
"""
save_money_119 카드뉴스 - 향상된 이미지 소싱 헬퍼 (2026-08-01)

기존 onepage_cardnews_template.download_best_image 를 확장한 모듈.
"소재로 인물 특정 -> 여러 매체에서 후보 다수 수집 -> 실제 픽셀 비교로 최고화질 채택 -> 육안 검수"
4단계 파이프라인에서, '후보 수집'과 '채택' 단계를 더 다양하고/고화질/신뢰도 높게 만들어 준다.

핵심 아이디어:
  1) 검색 썸네일이 아니라 '원본'을 받는다  -> naver_thumb_to_original(), article_hero_image()
  2) 라이선스가 깨끗한 고해상 소스를 추가한다 -> wikimedia_portrait_candidates()
  3) 후보를 5~8개로 늘려 실제 픽셀을 비교하고, 출처/해상도/용량을 리포트로 남긴다 -> pick_best_image()

주의:
  - 실존 인물 사진을 AI 업스케일링으로 보정하지 않는다 (이 모듈은 '고르기'만 하고 절대 확대·보정하지 않음).
  - 발행 전 반드시 육안 매칭 검수 게이트(Read로 이미지 직접 확인)를 통과시킨다. 이 모듈은 그 게이트를 대체하지 않는다.
  - urllib 직접 다운로드는 기존 download_best_image 와 동일한 방식(프로젝트에서 이미 검증된 경로)이다.
"""
import io
import json
import os
import re
import urllib.parse
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (compatible; savemoney119-cardnews/1.0)"}

# 프리미엄 스톡 API(Pexels 등)는 Cloudflare가 봇 UA(Python-urllib, 커스텀 문자열)를
# error 1010으로 막는다. API 호출·이미지 다운로드에는 실제 브라우저 UA를 써야 통과한다.
STOCK_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/124.0.0.0 Safari/537.36")}

# 후보를 모을 때의 '출처 우선순위' (숫자가 작을수록 신뢰/화질 우선). 리포트 정렬·가점에 사용.
SOURCE_TIERS = [
    # tier 1: 공식 1차 출처 (정부/기업 뉴스룸, 퍼블릭도메인 정부 사이트) - 원본 고해상, 워터마크 없음, 라이선스 안전
    (1, ["korea.kr", "president.go.kr", "moef.go.kr", "molit.go.kr", "krx.co.kr",
         "news.samsung.com", "samsung.com", "skhynix.com", "hyundai.com", "lge.co.kr",
         "nvidia.com", "tesla.com", "whitehouse.gov", "go.kr"]),
    # tier 2: 통신사 원본 (신뢰도 최상위, 원본 해상도 확보 가능)
    (2, ["yna.co.kr", "yonhapnews", "news1.kr", "newsis.com", "imgnews.naver.net"]),
    # tier 3: 라이선스가 명확한 아카이브 (CC/PD, 고해상)
    (3, ["upload.wikimedia.org", "commons.wikimedia.org", "kogl.or.kr", "gongu.copyright.or.kr"]),
    # tier 4: 종합 일간지/기타 매체 (그 외 전부)
]


def source_tier(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for tier, hosts in SOURCE_TIERS:
        if any(h in host or h in url for h in hosts):
            return tier
    return 4


def _get(url, timeout=20, tries=4):
    """API/HTML 조회. Commons API도 연속 호출 시 429를 내므로 백오프 재시도한다.
    (재시도가 없으면 호출부가 빈 리스트를 받고 '후보 없음'으로 잘못 판단한다)"""
    import time as _t
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            if "429" not in str(e) and "503" not in str(e):
                raise
            _t.sleep(1.5 + i * 2.0)
    raise last


def fetch_image(url, referer=None, timeout=25, tries=2):
    """이미지 바이너리를 견고하게 받아온다. 뉴스 CDN의 hotlink 차단(403) 대비로
    'UA만' -> 'UA+Referer(자동: 해당 호스트 루트)' 순으로 재시도한다.
    ※ 이미지/로고 같은 '바이너리 자산'은 반드시 이 방식(urllib)으로 받는다.
      WebFetch 계열 도구는 페이지 '텍스트'용이라 이미지 바이트를 못 준다 (예가 실패하던 흔한 원인)."""
    from urllib.parse import urlparse
    header_variants = [dict(UA)]
    if referer:
        header_variants.append({**UA, "Referer": referer})
    else:
        p = urlparse(url)
        header_variants.append({**UA, "Referer": f"{p.scheme}://{p.netloc}/"})
    last = None
    for h in header_variants:
        for _ in range(tries):
            try:
                req = urllib.request.Request(url, headers=h)
                return urllib.request.urlopen(req, timeout=timeout).read()
            except Exception as e:
                last = e
    raise last


# ---------- 1) 썸네일 -> 원본 정규화 ----------

def naver_thumb_to_original(url):
    """네이버 검색 이미지 결과는 리사이즈 프록시(search.pstatic.net)나 ?type=w800 축소본인 경우가 많다.
    실제 원본 URL로 되돌린다."""
    # search.pstatic.net/sunny?...&src=<real> 형태 -> 내부 src 추출
    if "pstatic.net" in url and "src=" in url:
        qs = urllib.parse.urlparse(url).query
        params = urllib.parse.parse_qs(qs)
        if "src" in params:
            url = urllib.parse.unquote(params["src"][0])
    # 뒤에 붙은 리사이즈 파라미터 제거 (?type=w800, ?type=nf220 등)
    url = re.sub(r"[?&]type=[^&]*$", "", url)
    return url


# ---------- 2) 기사 페이지에서 대표(원본) 이미지 뽑기 ----------

def article_hero_image(article_url, timeout=20):
    """뉴스 기사 URL에서 og:image / twitter:image(대개 원본 대표사진)를 추출한다.
    검색 썸네일보다 큰 원본을 얻는 가장 쉬운 방법."""
    try:
        html = _get(article_url, timeout).decode("utf-8", "ignore")
    except Exception:
        return None
    patterns = [
        r'property=["\']og:image(?::url)?["\']\s+content=["\']([^"\']+)["\']',
        r'content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
        r'name=["\']twitter:image["\']\s+content=["\']([^"\']+)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


# ---------- 3) 라이선스 안전 고해상 소스: Wikimedia Commons ----------

def wikimedia_portrait_candidates(query, limit=6, min_width=900, thumb_width=2000, timeout=20):
    """Wikimedia Commons에서 인물/주제 이미지를 검색해 원본(또는 지정폭 썸네일) URL 목록을 돌려준다.
    정치인/글로벌 CEO의 공식 초상이 CC/PD 라이선스로 고해상 등록돼 있는 경우가 많다.
    (재게시 라이선스가 상대적으로 안전 — 단 파일별 라이선스/저작자 표기 조건은 최종 확인 필요.)"""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": limit,
        "prop": "imageinfo",
        "iiprop": "url|size|extmetadata",
        "iiurlwidth": thumb_width,
    }
    url = api + "?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(_get(url, timeout).decode())
    except Exception:
        return []
    out = []
    pages = data.get("query", {}).get("pages", {})
    for _pid, page in pages.items():
        for ii in page.get("imageinfo", []):
            w = ii.get("width", 0)
            if w < min_width:
                continue
            meta = ii.get("extmetadata", {}) or {}
            license_short = (meta.get("LicenseShortName", {}) or {}).get("value", "")
            out.append({
                "url": ii.get("url"),                 # 원본
                "thumb": ii.get("thumburl"),          # 지정폭 썸네일(카드용으로 충분)
                "width": w, "height": ii.get("height", 0),
                "license": license_short,
                "title": page.get("title", ""),
            })
    # 큰 순으로
    out.sort(key=lambda r: r["width"] * r["height"], reverse=True)
    return out


# ---------- 4) 후보 다운로드 + 실제 픽셀 비교 + 리포트 ----------

def pick_best_image(urls, tmp_dir, min_side=1200, prefix="cand", normalize=True):
    """후보 URL들을 실제로 내려받아 픽셀 크기를 비교하고, 출처 tier와 함께 리포트를 남긴다.

    반환: (best, report)
      best   = {"path","url","w","h","min_side","bytes","host","tier","meets_min"}  (다운로드 실패 전부면 None)
      report = 후보별 결과 리스트 (성공/실패 모두 기록)

    선택 규칙:
      - 기본은 '실제 짧은 변이 min_side 이상'인 후보들 중 '면적이 가장 큰 것'.
      - min_side를 만족하는 후보가 하나도 없으면, 그 중 가장 큰 것을 쓰되 meets_min=False로 경고.
      - AI 업스케일링/보정은 하지 않는다 (원본을 그대로 채택).
    """
    os.makedirs(tmp_dir, exist_ok=True)
    report = []
    seen = set()
    downloaded = []
    for i, raw in enumerate(urls):
        u = naver_thumb_to_original(raw) if normalize else raw
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            data = _get(u, 25)
            im = Image.open(io.BytesIO(data))
            w, h = im.size
            ext = "png" if (im.format == "PNG") else "jpg"
            path = os.path.join(tmp_dir, f"{prefix}_{i}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            rec = {
                "path": path, "url": u, "w": w, "h": h, "min_side": min(w, h),
                "bytes": len(data), "host": urllib.parse.urlparse(u).netloc,
                "tier": source_tier(u),
            }
            downloaded.append(rec)
            report.append(rec)
        except Exception as e:
            report.append({"url": u, "error": str(e), "tier": source_tier(u)})

    if not downloaded:
        return None, report

    qualified = [r for r in downloaded if r["min_side"] >= min_side]
    pool = qualified if qualified else downloaded
    # 화질(면적) 우선, 동률이면 tier(작을수록 신뢰) 우선
    best = sorted(pool, key=lambda r: (r["w"] * r["h"], -r["tier"]), reverse=True)[0]
    best["meets_min"] = best["min_side"] >= min_side
    return best, report


# ---------- 5) 기업 로고 소싱 (삼성/SK/테슬라 등) ----------

def commons_file_candidates(query, width=1200, limit=8, timeout=20):
    """Wikimedia Commons 파일 검색. 각 파일의 원본 크기 + '지정폭 렌더 URL'(thumburl)을 준다.
    핵심: SVG 로고도 thumburl은 지정폭 PNG로 렌더돼 내려온다 → 별도 SVG 변환 불필요."""
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": query, "gsrnamespace": 6, "gsrlimit": limit,
        "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": width,
    }
    try:
        data = json.loads(_get(api + "?" + urllib.parse.urlencode(params), timeout).decode())
    except Exception:
        return []
    out = []
    for _pid, page in data.get("query", {}).get("pages", {}).items():
        for ii in page.get("imageinfo", []):
            meta = ii.get("extmetadata", {}) or {}
            out.append({
                "title": page.get("title", ""),
                "mime": ii.get("mime"),
                "src_w": ii.get("width"), "src_h": ii.get("height"),
                "render_url": ii.get("thumburl") or ii.get("url"),  # 지정폭 PNG 렌더
                "orig_url": ii.get("url"),
                "license": (meta.get("LicenseShortName", {}) or {}).get("value", ""),
            })
    return out


def logo_candidates(company, out_dir, width=1200, limit=8, prefix="logo"):
    """기업 로고 후보들을 실제로 렌더/다운로드해서 파일로 저장하고 리포트를 돌려준다.
    ※ Commons 검색은 자회사/변형 로고(예: 'SK magic', 'Galaxy A8')를 섞어주므로
      반드시 Read로 육안 확인 후 채택할 것. 이 함수는 '후보 모으기'까지만 한다.

    반환: report = [{"path","title","mime","w","h","render_url","license"} 또는 {"error",...}]
    (뉴스 사진과 달리 로고는 가로로 긴 워드마크가 많아 min_side 기준을 적용하지 않는다.)"""
    import time
    os.makedirs(out_dir, exist_ok=True)
    cands = commons_file_candidates(f"{company} logo", width=width, limit=limit)
    report = []
    for i, c in enumerate(cands):
        url = c["render_url"]
        if not url:
            continue
        try:
            time.sleep(0.6)  # upload.wikimedia.org 연속 요청 시 HTTP 429 방지 (throttle)
            data = fetch_image(url)
            ext = "png"
            path = os.path.join(out_dir, f"{prefix}_{i}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            w = h = None
            if Image is not None:
                try:
                    im = Image.open(io.BytesIO(data)); w, h = im.size
                except Exception:
                    pass
            report.append({"path": path, "title": c["title"], "mime": c["mime"],
                           "w": w, "h": h, "render_url": url, "license": c["license"]})
        except Exception as e:
            report.append({"error": str(e), "title": c["title"], "render_url": url})
    return report


# ---------- 6) 실사 사진 소싱 (재게시 가능한 라이선스만) ----------

# 상업적 재게시가 허용되는 라이선스만 통과시킨다.
# 뉴스 보도사진(연합/뉴시스 등)은 여기 없다 — 블로그 재게시 시 저작권 문제가 되므로
# 이 파이프라인에서는 아예 후보로 올리지 않는다.
COMMERCIAL_OK = {"cc0", "pdm", "by", "by-sa"}
ATTRIB_REQUIRED = {"by", "by-sa"}   # 저작자·라이선스 표기 의무가 있는 것


def openverse_photo_candidates(query, limit=12, min_width=1400, timeout=25):
    """Openverse(위키미디어·플리커·박물관 등 CC 이미지 통합 검색)에서 후보를 모은다.

    API 키가 필요 없고, 라이선스/저작자/출처 메타데이터를 함께 주기 때문에
    표기 의무가 있는 라이선스도 안전하게 쓸 수 있다.

    반환 항목: url(원본) / thumb / w / h / license / attribution / creator / landing
    """
    params = {
        "q": query,
        "license_type": "commercial,modification",  # 상업이용+변형 허용만
        "size": "large",
        "page_size": str(limit),
    }
    url = "https://api.openverse.org/v1/images/?" + urllib.parse.urlencode(params)
    try:
        data = json.loads(_get(url, timeout).decode())
    except Exception:
        return []

    out = []
    for r in data.get("results", []):
        lic = (r.get("license") or "").lower()
        if lic not in COMMERCIAL_OK:
            continue
        w, h = r.get("width") or 0, r.get("height") or 0
        if w < min_width:
            continue
        out.append({
            "url": r.get("url"),
            "thumb": r.get("thumbnail"),
            "w": w, "h": h,
            "license": lic,
            "license_version": r.get("license_version") or "",
            "license_url": r.get("license_url") or "",
            "creator": r.get("creator") or "",
            "title": (r.get("title") or "")[:80],
            "source": r.get("source") or r.get("provider") or "",
            "landing": r.get("foreign_landing_url") or "",
            "attribution": r.get("attribution") or "",
            "attrib_required": lic in ATTRIB_REQUIRED,
        })
    out.sort(key=lambda r: r["w"] * r["h"], reverse=True)
    return out


def short_credit(rec):
    """카드 하단에 넣을 짧은 크레딧 문자열.

    CC BY / BY-SA 는 저작자·라이선스 표기가 의무다. CC0 / PDM 은 없어도 되지만
    출처를 남기는 편이 신뢰도에 유리해서 동일하게 표기한다."""
    creator = (rec.get("creator") or "").strip()
    lic = (rec.get("license") or "").upper()
    ver = rec.get("license_version") or ""
    lic_txt = f"CC {lic} {ver}".strip() if lic not in ("CC0", "PDM") else lic
    src = rec.get("source") or ""
    bits = [b for b in [creator, lic_txt, src] if b]
    return "사진 : " + " / ".join(bits)


def wikimedia_photo_candidates(query, out_dir, limit=6, min_width=1600,
                               render_width=2200, prefix="wm"):
    """Wikimedia Commons에서 직접 사진을 받는다. 고해상이 보장되는 경로.

    Openverse는 편하지만 rawpixel 같은 제공자가 '6000px'이라고 보고해놓고
    실제로는 1024px 축소본만 내려주는 경우가 많다. Commons는 iiurlwidth로
    원하는 폭을 서버에서 렌더해 주기 때문에 요청한 크기가 그대로 온다.

    라이선스는 파일별로 다르므로 재게시 가능한 것만 통과시킨다.
    """
    import time
    os.makedirs(out_dir, exist_ok=True)
    cands = commons_file_candidates(query, width=render_width, limit=limit * 3)
    report, kept = [], 0
    for c in cands:
        if kept >= limit:
            break
        if not _portrait_license_ok(c.get("license")):
            report.append({"skipped": "라이선스 부적합", "license": c.get("license"),
                           "title": c.get("title", "")})
            continue
        if (c.get("src_w") or 0) < min_width:
            report.append({"skipped": f"원본 작음({c.get('src_w')}px)",
                           "title": c.get("title", "")})
            continue
        if (c.get("mime") or "").endswith("svg+xml"):
            continue                     # 사진이 아니라 도형
        url = c.get("render_url")
        try:
            time.sleep(1.0)
            data = fetch_image(url)
            rw, rh = _real_size(data)
            if rw and rw < min_width:
                report.append({"skipped": f"렌더 작음({rw}px)", "title": c["title"]})
                continue
            path = os.path.join(out_dir, f"{prefix}_{kept}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            title = (c["title"] or "").replace("File:", "").rsplit(".", 1)[0]
            report.append({
                "path": path, "w": rw, "h": rh,
                "license": c["license"],
                "credit": f"사진 : {title[:44]} / {c['license']} / Wikimedia Commons",
                "title": c["title"], "src_w": c.get("src_w"), "src_h": c.get("src_h"),
            })
            kept += 1
        except Exception as e:
            report.append({"error": str(e)[:70], "title": c.get("title", "")})
    return report


def wikimedia_thumb_url(url, width=2400):
    """upload.wikimedia.org 원본 URL을 '지정폭 렌더' URL로 바꾼다.

      .../commons/a/ab/Foo.jpg  ->  .../commons/thumb/a/ab/Foo.jpg/2400px-Foo.jpg

    원본이 8000px짜리면 그대로 받는 건 느리고 무거우므로, 필요한 폭만 렌더해서 받는다.
    (이미 thumb URL이면 그대로 돌려준다)"""
    if "upload.wikimedia.org" not in url or "/thumb/" in url:
        return url
    m = re.match(r"(https?://upload\.wikimedia\.org/wikipedia/[^/]+)/([0-9a-f])/([0-9a-f]{2})/(.+)$", url)
    if not m:
        return url
    base, d1, d2, fname = m.groups()
    return f"{base}/thumb/{d1}/{d2}/{fname}/{width}px-{fname}"


def _real_size(data):
    if Image is None:
        return (0, 0)
    try:
        return Image.open(io.BytesIO(data)).size
    except Exception:
        return (0, 0)


def photo_candidates(query, out_dir, limit=10, min_width=1400, prefix="photo"):
    """실사 사진 후보를 실제로 내려받아 파일로 저장하고 리포트를 돌려준다.

    logo_candidates 와 같은 구조다 — 자동 채택하지 않고 '후보 모으기'까지만 한다.
    검색 결과에는 주제와 무관한 사진이 반드시 섞이므로, Read 도구로 눈으로 확인한 뒤
    고르는 단계를 건너뛰면 안 된다.

    반환: [{"path","w","h","license","credit","attrib_required","title","landing"} | {"error",...}]
    """
    import time
    os.makedirs(out_dir, exist_ok=True)
    cands = openverse_photo_candidates(query, limit=limit * 2, min_width=min_width)

    # 원본을 실제로 주는 호스트를 앞에 세운다.
    # rawpixel 등 일부 제공자는 API가 6000px이라고 보고해놓고 1024px 축소본만 내려준다.
    cands.sort(key=lambda c: (0 if "wikimedia" in (c.get("url") or "") else 1,
                              -(c["w"] * c["h"])))

    report, kept = [], 0
    for i, c in enumerate(cands):
        if kept >= limit:
            break
        url = wikimedia_thumb_url(c["url"], width=2400)
        try:
            time.sleep(1.0)   # 429 방지
            try:
                data = fetch_image(url, referer=c.get("landing") or None)
            except Exception:
                # Wikimedia가 임의 폭 썸네일 렌더를 400으로 거부하는 파일이 있다
                # (허용된 사이즈 목록 정책). 이때는 원본을 그대로 받는다 —
                # 어차피 이후 파이프라인에서 1080px로 다시 리사이즈되므로 무겁지 않다.
                if url == c["url"]:
                    raise
                data = fetch_image(c["url"], referer=c.get("landing") or None)
            rw, rh = _real_size(data)
            # API 보고값이 아니라 '실제 픽셀'로 판정한다
            if rw and rw < min_width:
                report.append({"skipped": f"축소본({rw}x{rh}, 보고 {c['w']}x{c['h']})",
                               "title": c["title"]})
                continue
            ext = "png" if data[:8].startswith(b"\x89PNG") else "jpg"
            path = os.path.join(out_dir, f"{prefix}_{kept}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            report.append({
                "path": path, "w": rw or c["w"], "h": rh or c["h"],
                "license": c["license"], "credit": short_credit(c),
                "attrib_required": c["attrib_required"],
                "title": c["title"], "landing": c["landing"],
                "bytes": len(data),
            })
            kept += 1
        except Exception as e:
            report.append({"error": str(e)[:80], "title": c.get("title", ""),
                           "url": url})
    return report


# ---------- 6b) 프리미엄 스톡 API 소싱 (Pexels / Unsplash / Pixabay) ----------
#
# 키는 '환경변수에서만' 읽는다 — 코드/커밋에 값이 절대 들어가지 않는다.
#   PEXELS_API_KEY, UNSPLASH_ACCESS_KEY, PIXABAY_API_KEY
#
# 세 곳 모두 '워터마크 없음 + 무료 상업적 재게시 허용' 라이선스라 수익형 블로그에 안전하다:
#   - Pexels License / Unsplash License / Pixabay Content License
#     → 상업적 이용 OK, 출처표시 '의무 아님'(권장). 사진 원본을 그대로 되팔거나
#       재배포(사진 자체를 상품화)하는 것만 금지. 카드뉴스 배경으로 쓰는 건 허용.
# 그래도 신뢰도·추적을 위해 photographer/출처 URL/라이선스를 index.json에 남긴다.
#
# 각 *_candidates()는 '메타 후보 리스트'만 돌려준다(다운로드는 stock_photo_candidates에서).
# 공통 반환 키: url(다운로드용) / w / h / source_api / license / photographer /
#             landing(출처 페이지) / credit / id


def _stock_json(url, headers, timeout=25, tries=4):
    """스톡 API JSON 조회. 429/5xx는 백오프 재시도, 그 외 예외는 즉시 전파."""
    import time as _t
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())
        except Exception as e:
            last = e
            s = str(e)
            if "429" in s or "500" in s or "502" in s or "503" in s:
                _t.sleep(1.5 + i * 2.0)
                continue
            raise
    raise last


def pexels_candidates(query, per_page=15, min_width=1200, timeout=25):
    """Pexels 검색. 원본(src.original)을 준다 — 대개 3000px+ 고해상."""
    key = os.getenv("PEXELS_API_KEY")
    if not key:
        return []
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode(
        {"query": query, "per_page": per_page, "orientation": "landscape"})
    try:
        data = _stock_json(url, {**STOCK_UA, "Authorization": key}, timeout)
    except Exception:
        return []
    out = []
    for p in data.get("photos", []):
        w, h = p.get("width", 0), p.get("height", 0)
        if w < min_width:
            continue
        src = p.get("src", {}) or {}
        dl = src.get("original") or src.get("large2x") or src.get("large")
        if not dl:
            continue
        who = p.get("photographer", "")
        out.append({
            "url": dl, "w": w, "h": h, "source_api": "pexels",
            "license": "Pexels License", "photographer": who,
            "landing": p.get("url", ""),
            "credit": " / ".join(x for x in [who, "Pexels"] if x),
            "id": p.get("id"),
        })
    return out


def unsplash_candidates(query, per_page=15, min_width=1200, timeout=25):
    """Unsplash 검색. raw URL에 폭 파라미터를 붙여 1600px JPG로 받아 원본 과대용량을 피한다."""
    key = os.getenv("UNSPLASH_ACCESS_KEY")
    if not key:
        return []
    url = "https://api.unsplash.com/search/photos?" + urllib.parse.urlencode(
        {"query": query, "per_page": per_page, "orientation": "landscape",
         "content_filter": "high", "client_id": key})
    try:
        data = _stock_json(url, STOCK_UA, timeout)
    except Exception:
        return []
    out = []
    for p in data.get("results", []):
        w, h = p.get("width", 0), p.get("height", 0)
        if w < min_width:
            continue
        urls = p.get("urls", {}) or {}
        raw = urls.get("raw")
        # raw + 동적 리사이즈: 긴 변 2400px(→3:2면 짧은 변 1600px, 정사각 크롭에 충분),
        # JPG q85 (원본이 6000px여도 가볍게 받는다).
        dl = (raw + "&w=2400&q=85&fm=jpg&fit=max") if raw else (urls.get("full") or urls.get("regular"))
        if not dl:
            continue
        who = (p.get("user", {}) or {}).get("name", "")
        out.append({
            "url": dl, "w": w, "h": h, "source_api": "unsplash",
            "license": "Unsplash License", "photographer": who,
            "landing": (p.get("links", {}) or {}).get("html", ""),
            "credit": " / ".join(x for x in [who, "Unsplash"] if x),
            "id": p.get("id"),
        })
    return out


def pixabay_candidates(query, per_page=20, min_width=1200, timeout=25):
    """Pixabay 검색. largeImageURL(긴 변 1280px)을 준다 — 가로 사진은 정사각 크롭 시
    짧은 변이 부족할 수 있어, 실제 다운로드 후 min_side로 한 번 더 거른다."""
    key = os.getenv("PIXABAY_API_KEY")
    if not key:
        return []
    url = "https://pixabay.com/api/?" + urllib.parse.urlencode(
        {"key": key, "q": query, "image_type": "photo", "per_page": per_page,
         "safesearch": "true", "min_width": min_width, "order": "popular"})
    try:
        data = _stock_json(url, STOCK_UA, timeout)
    except Exception:
        return []
    out = []
    for p in data.get("hits", []):
        w, h = p.get("imageWidth", 0), p.get("imageHeight", 0)   # 원본 해상도(대형)
        # 큰 렌더부터: imageURL(원본, 권한 필요)→fullHDURL(1920)→largeImageURL(1280).
        # 무료 키는 대개 largeImageURL(긴 변 1280)만 와서 가로 사진은 정사각 크롭에 짧다.
        dl = (p.get("imageURL") or p.get("fullHDURL")
              or p.get("largeImageURL") or p.get("webformatURL"))
        if not dl:
            continue
        who = p.get("user", "")
        out.append({
            "url": dl, "w": w, "h": h, "source_api": "pixabay",
            "license": "Pixabay Content License", "photographer": who,
            "landing": p.get("pageURL", ""),
            "credit": " / ".join(x for x in [who, "Pixabay"] if x),
            "id": p.get("id"),
        })
    return out


def _stock_download(url, referer=None, timeout=35, tries=3):
    """스톡 CDN 이미지 다운로드. 브라우저 UA(+필요시 Referer)로 hotlink/Cloudflare 회피."""
    header_variants = [dict(STOCK_UA)]
    if referer:
        header_variants.append({**STOCK_UA, "Referer": referer})
    last = None
    for h in header_variants:
        for _ in range(tries):
            try:
                req = urllib.request.Request(url, headers=h)
                return urllib.request.urlopen(req, timeout=timeout).read()
            except Exception as e:
                last = e
    raise last


STOCK_GETTERS = {
    "pexels": pexels_candidates,
    "unsplash": unsplash_candidates,
    "pixabay": pixabay_candidates,
}


def stock_photo_candidates(query, out_dir, keep=6, min_side=1200, prefix="stock",
                           apis=("pexels", "unsplash", "pixabay"),
                           square=True, size=1400):
    """3개 프리미엄 API에서 후보를 모아 실제로 내려받아 리사이즈본을 저장하고 리포트를 준다.

    - 소스 다양성: api별로 면적순 정렬 후 '라운드로빈'으로 골라, 한 소스가 독식하지 않게 한다.
    - 화질 보장: '실제 픽셀' min(w,h) >= min_side 인 것만 채택(1080px+ 요건 충족).
    - 저장: square=True면 prepare_photo로 정사각 size(기본 1400px) + 시리즈 톤/비네트까지
      입혀 기존 라이브러리와 톤을 맞춘다. '리사이즈본'만 남기므로 대용량 원본은 저장되지 않는다.

    반환 리포트 항목:
      {"path","w","h","src_w","src_h","source_api","license","credit",
       "photographer","landing","attrib_required","query"}  또는  {"error"/"skipped",...}
    """
    import time
    os.makedirs(out_dir, exist_ok=True)

    per_api = {}
    for api in apis:
        try:
            cands = STOCK_GETTERS[api](query, per_page=max(keep * 3, 15), min_width=min_side)
        except Exception:
            cands = []
        cands.sort(key=lambda c: -(c["w"] * c["h"]))
        per_api[api] = cands

    # 라운드로빈 병합
    ordered, i = [], 0
    while any(i < len(per_api[a]) for a in apis):
        for a in apis:
            if i < len(per_api[a]):
                ordered.append(per_api[a][i])
        i += 1

    report, kept, seen = [], 0, set()
    for c in ordered:
        if kept >= keep:
            break
        u = c["url"]
        if not u or u in seen:
            continue
        seen.add(u)
        try:
            time.sleep(0.3)
            data = _stock_download(u, referer=c.get("landing") or None)
            rw, rh = _real_size(data)
            if min(rw, rh) < min_side:
                report.append({"skipped": f"짧은변 부족({rw}x{rh})",
                               "api": c["source_api"], "id": c.get("id")})
                continue
            path = os.path.join(out_dir, f"{prefix}_{c['source_api']}_{kept}.jpg")
            with open(path, "wb") as f:
                f.write(data)
            if square:
                try:
                    prepare_photo(path, path, size=size)
                    pw = ph = size
                except Exception as e:
                    report.append({"error": f"가공실패 {str(e)[:50]}",
                                   "api": c["source_api"]})
                    if os.path.exists(path):
                        os.remove(path)
                    continue
            else:
                pw, ph = rw, rh
            report.append({
                "path": path, "w": pw, "h": ph, "src_w": rw, "src_h": rh,
                "source_api": c["source_api"], "license": c["license"],
                "credit": c["credit"], "photographer": c["photographer"],
                "landing": c["landing"], "attrib_required": False,
                "query": query,
            })
            kept += 1
        except Exception as e:
            report.append({"error": str(e)[:80], "api": c.get("source_api"), "url": u})
    return report


# ---------- 7) 인물 사진 소싱 (공인의 공적 활동, 라이선스 열린 소스만) ----------

# 재게시 가능한 라이선스 패턴. 언론사 보도사진은 여기 없다.
#   KOGL 제1유형 = 공공누리 1유형: 출처표시 조건, 상업적 이용·변형 허용
PORTRAIT_OK_PATTERNS = [
    "public domain", "pdm", "cc0",
    "cc by", "cc by-sa", "kogl", "공공누리",
]


def _portrait_license_ok(license_short):
    s = (license_short or "").lower()
    if not s:
        return False
    if "nc" in s.split() or "-nc" in s or "noncommercial" in s:
        return False          # 비상업 전용은 블로그(수익형)에 부적합
    if "nd" in s.split() or "-nd" in s:
        return False          # 변형 금지는 카드 합성에 부적합
    return any(pat in s for pat in PORTRAIT_OK_PATTERNS)


def portrait_credit(rec):
    """인물 사진 크레딧. KOGL·CC BY 계열은 출처표시가 조건이므로 반드시 넣는다."""
    lic = rec.get("license", "")
    title = (rec.get("title", "") or "").replace("File:", "").rsplit(".", 1)[0]
    return f"사진 : {title[:46]} / {lic} / Wikimedia Commons"


def portrait_candidates(query, out_dir, limit=6, min_width=1200, prefix="portrait"):
    """공인의 인물 사진 후보를 라이선스 열린 소스에서만 받아온다.

    판단 기준은 '실존 인물이냐'가 아니라 '이 사진을 재게시할 권리가 있느냐'다.
    공인을 그의 공적 활동(실적·정책 발표 등)과 함께 보도·정보전달 맥락에서 다루는 것은
    문제되지 않는다. 문제가 되는 것은 언론사 사진의 무단 전재다.

    주의:
      - Commons 검색은 느슨해서 동명이인·단체사진·엉뚱한 인물이 섞인다.
        반드시 Read 도구로 얼굴을 눈으로 확인한 뒤 채택할 것.
      - 상업적 보증처럼 보이게 쓰지 않는다 (특정 상품 추천에 인물 얼굴을 붙이는 행위).
      - 공적 역할과 무관한 사적인 사진은 쓰지 않는다.
    """
    import time
    os.makedirs(out_dir, exist_ok=True)
    cands = wikimedia_portrait_candidates(query, limit=limit * 2, min_width=min_width)
    report, n = [], 0
    for c in cands:
        if n >= limit:
            break
        if not _portrait_license_ok(c.get("license")):
            report.append({"skipped": "라이선스 부적합", "license": c.get("license"),
                           "title": c.get("title", "")})
            continue
        url = c.get("thumb") or c.get("url")   # thumb = 지정폭 렌더 (2000px)
        try:
            # upload.wikimedia.org 는 연속 요청에 429를 잘 낸다. 0.5초로는 부족해서 1.2초.
            time.sleep(1.2)
            data = fetch_image(url)
            ext = "png" if data[:8].startswith(b"\x89PNG") else "jpg"
            path = os.path.join(out_dir, f"{prefix}_{n}.{ext}")
            with open(path, "wb") as f:
                f.write(data)
            w = h = None
            if Image is not None:
                try:
                    im = Image.open(io.BytesIO(data)); w, h = im.size
                except Exception:
                    pass
            report.append({
                "path": path, "w": w or c["width"], "h": h or c["height"],
                "license": c["license"], "credit": portrait_credit(c),
                "title": c["title"], "src_w": c["width"], "src_h": c["height"],
            })
            n += 1
        except Exception as e:
            report.append({"error": str(e)[:70], "title": c.get("title", "")})
    return report


# ---------- 8) 후처리: 매일 다른 사진이 와도 같은 톤으로 보이게 ----------

def prepare_photo(src_path, dst_path, size=1080, focus="center",
                  grade=True, vignette=True):
    """사진을 카드 규격에 맞게 정사각으로 자르고, 시리즈 톤으로 보정한다.

    스톡 사진을 그대로 쓰면 매일 색감이 제각각이라 블로그가 '긁어온 티'가 난다.
    아래 3가지를 걸면 어떤 사진이 와도 같은 매체가 만든 것처럼 보인다:
      1) 정사각 크롭 (비율 왜곡 없이 꽉 채움)
      2) 컬러 그레이딩 — 채도를 살짝 낮추고 그림자에 청색을 더해 카드 남색과 붙인다
      3) 비네트 — 가장자리를 눌러 가운데 피사체로 시선을 모으고 카피 가독성을 높인다

    focus: 'center' | 'top' | 'bottom'  (인물은 대개 'top'이 얼굴을 살린다)
    """
    if Image is None:
        raise RuntimeError("Pillow가 필요합니다")
    from PIL import ImageEnhance, ImageDraw, ImageFilter

    im = Image.open(src_path).convert("RGB")
    w, h = im.size

    # 1) 정사각 크롭
    side = min(w, h)
    if w >= h:
        left = (w - side) // 2
        top = 0
    else:
        left = 0
        if focus == "top":
            top = 0
        elif focus == "bottom":
            top = h - side
        else:
            top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    im = im.resize((size, size), Image.LANCZOS)

    if grade:
        # 채도 -12%, 대비 +8% : 과하지 않게. 사진을 '망치지 않는' 선이 중요하다
        im = ImageEnhance.Color(im).enhance(0.88)
        im = ImageEnhance.Contrast(im).enhance(1.08)
        # 그림자에만 청색을 살짝 — 카드 배경(남색)과 자연스럽게 이어진다
        tint = Image.new("RGB", im.size, (14, 30, 58))
        im = Image.blend(im, tint, 0.10)

    if vignette:
        # 가장자리를 어둡게 (원형 마스크를 블러해서 자연스럽게)
        mask = Image.new("L", (size, size), 0)
        d = ImageDraw.Draw(mask)
        pad = int(size * 0.06)
        d.ellipse([-pad, -pad, size + pad, size + pad], fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(size * 0.18))
        dark = Image.new("RGB", im.size, (6, 12, 24))
        im = Image.composite(im, dark, mask)

    im.save(dst_path, quality=92, subsampling=1)
    return dst_path


# 후보를 못 찾을 때의 최후 폴백 URL 빌더 (참고용 — 화질/색상 한계 있음)
def logo_fallback_urls(domain, brand_slug=None):
    """Commons에서 못 구했을 때 최후 폴백. 순서대로 시도.
      1) Simple Icons: 단색 브랜드 SVG (색이 단색이라 카드용으로는 제한적)
      2) Google favicon: 최대 256px PNG (작음, 정말 최후일 때만)
    ※ Clearbit(logo.clearbit.com)는 이 샌드박스에서 DNS 차단이라 제외했다."""
    urls = []
    if brand_slug:
        urls.append(f"https://cdn.simpleicons.org/{brand_slug}")           # 단색 svg
    urls.append(f"https://www.google.com/s2/favicons?domain={domain}&sz=256")  # 작은 png
    return urls


try:
    from PIL import Image  # noqa: E402  (하단 import: 픽셀 비교에만 필요)
except Exception:  # pragma: no cover
    Image = None


if __name__ == "__main__":
    # 간단 self-test (네트워크 필요)
    print("[test] Wikimedia: Jensen Huang")
    for c in wikimedia_portrait_candidates("Jensen Huang", limit=4)[:4]:
        print("  ", c["width"], "x", c["height"], c["license"], c["url"])
    print("[test] naver thumb normalize")
    t = "https://search.pstatic.net/common/?type=b150&src=http%3A%2F%2Fimgnews.naver.net%2Fimage%2F001%2Fx.jpg"
    print("  ->", naver_thumb_to_original(t))
