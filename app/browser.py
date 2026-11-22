"""전용 브라우저: 로그인 창 열기, 쿠키 내보내기, 페이지에서 미디어 주소 찾기."""
import json
import logging
import re
import subprocess
import threading
import time
import urllib.request

from . import paths

log = logging.getLogger("browser")
_pw_lock = threading.Lock()   # Playwright 동시 실행 방지
_proc: subprocess.Popen | None = None

# 사이트별 로그인 여부를 판단할 쿠키
LOGIN_COOKIES = {
    "instagram": (".instagram.com", "sessionid"),
    "threads": (".threads.com", "sessionid"),
    "tiktok": (".tiktok.com", "sessionid"),
    "douyin": (".douyin.com", "sessionid"),
    "xiaohongshu": (".xiaohongshu.com", "web_session"),
    "naver": (".naver.com", "NID_AUT"),
    "youtube": (".youtube.com", "SID"),
}


# ---------------------------------------------------------------- 전용 브라우저
def cdp_alive() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{paths.CDP_PORT}/json/version", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def open_browser(url: str) -> None:
    """전용 브라우저(별도 프로필)를 열거나, 이미 열려 있으면 새 탭으로 연다."""
    global _proc
    chrome = paths.find_chrome()
    if not chrome:
        raise RuntimeError("Chrome 또는 Edge를 찾지 못했습니다.")
    paths.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        chrome,
        f"--user-data-dir={paths.PROFILE_DIR}",
        f"--remote-debugging-port={paths.CDP_PORT}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    _proc = subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ---------------------------------------------------------------- 피드 화면 → 지금 보는 영상
_CURRENT_TIKTOK_JS = r"""
() => {
  const vw = innerWidth, vh = innerHeight;
  let best = null, bestArea = 0;
  for (const v of document.querySelectorAll('video')) {
    const r = v.getBoundingClientRect();
    const w = Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0));
    const h = Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    if (w * h > bestArea) { bestArea = w * h; best = v; }
  }
  if (!best) return null;
  let id = null, user = null, el = best;
  for (let i = 0; i < 20 && el; i++, el = el.parentElement) {
    if (!id && el.id) { const m = el.id.match(/(\d{15,})/); if (m) id = m[1]; }
    if (!user && el.querySelector) {
      const a = el.querySelector('a[href*="/@"]');
      if (a) { const m = (a.getAttribute('href') || '').match(/\/@([\w.\-]+)/); if (m) user = m[1]; }
    }
    if (el.tagName === 'ARTICLE') break;
  }
  return id ? {id, user} : null;
}
"""


def current_tiktok_video(feed_url: str) -> str | None:
    """전용 브라우저에 열린 틱톡 추천·팔로잉 화면에서 지금 보이는 영상 주소를 찾는다."""
    if not cdp_alive():
        return None
    from playwright.sync_api import sync_playwright
    with _pw_lock, sync_playwright() as p:
        b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{paths.CDP_PORT}")
        try:
            pages = [pg for ctx in b.contexts for pg in ctx.pages if "tiktok.com" in pg.url]

            def rank(pg):
                try:
                    vis = pg.evaluate("document.visibilityState") == "visible"
                except Exception:
                    vis = False
                return (pg.url.split("#")[0] == feed_url.split("#")[0], vis)

            for pg in sorted(pages, key=rank, reverse=True):
                try:
                    r = pg.evaluate(_CURRENT_TIKTOK_JS)
                except Exception:
                    r = None
                if r and r.get("id"):
                    return f"https://www.tiktok.com/@{r.get('user') or ''}/video/{r['id']}"
        finally:
            b.close()   # 연결만 끊음
    return None


# ---------------------------------------------------------------- 쿠키
def _write_cookies(cookies: list[dict]) -> None:
    paths.COOKIES_JSON.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = c.get("domain", "")
        if not domain:
            continue
        exp = c.get("expires", -1)
        exp = int(exp) if exp and exp > 0 else 0
        name, value = c.get("name", ""), c.get("value", "")
        if "\t" in value or "\n" in value:
            continue
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([
            prefix + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(exp), name, value,
        ]))
    paths.COOKIES_TXT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_cookies() -> bool:
    """전용 브라우저에서 쿠키를 가져와 파일로 저장. 성공하면 True."""
    from playwright.sync_api import sync_playwright
    with _pw_lock:
        try:
            with sync_playwright() as p:
                if cdp_alive():
                    b = p.chromium.connect_over_cdp(f"http://127.0.0.1:{paths.CDP_PORT}")
                    cookies = b.contexts[0].cookies() if b.contexts else []
                    b.close()  # 연결만 끊음(브라우저는 계속 열려 있음)
                elif (paths.PROFILE_DIR / "Default").exists():
                    ctx = p.chromium.launch_persistent_context(
                        str(paths.PROFILE_DIR), channel=_channel(), headless=True)
                    cookies = ctx.cookies()
                    ctx.close()
                else:
                    return False
            _write_cookies(cookies)
            return True
        except Exception as e:
            log.warning("쿠키 갱신 실패: %s", e)
            return False


def login_status() -> dict:
    try:
        cookies = json.loads(paths.COOKIES_JSON.read_text(encoding="utf-8"))
    except Exception:
        cookies = []
    names = {(c.get("domain", ""), c.get("name", "")) for c in cookies}
    out = {}
    for site, (dom, name) in LOGIN_COOKIES.items():
        out[site] = any(d.endswith(dom.lstrip(".")) and n == name for d, n in names)
    return out


def _channel() -> str:
    c = paths.find_chrome() or ""
    return "msedge" if "msedge" in c.lower() else "chrome"


# ---------------------------------------------------------------- 미디어 찾기
_DOM_JS = r"""
() => {
  const out = {videos: [], metas: [], title: document.title, state: null};
  document.querySelectorAll('video').forEach(v => {
    const srcs = [v.currentSrc, v.src, ...[...v.querySelectorAll('source')].map(s => s.src)];
    srcs.forEach(s => { if (s && !s.startsWith('blob:')) out.videos.push(s); });
  });
  document.querySelectorAll('meta').forEach(m => {
    const k = (m.getAttribute('property') || m.getAttribute('name') || '').toLowerCase();
    const v = m.getAttribute('content');
    if (v && /^(og:image|og:video|og:video:url|og:video:secure_url|og:title|og:description|twitter:image)$/.test(k)) out.metas.push([k, v]);
  });
  try {
    const s = window.__INITIAL_STATE__;
    if (s && s.note && s.note.noteDetailMap) {
      const notes = [];
      for (const k in s.note.noteDetailMap) {
        const n = s.note.noteDetailMap[k] && s.note.noteDetailMap[k].note;
        if (!n) continue;
        notes.push({
          id: n.noteId, title: n.title || n.desc || '', user: (n.user && n.user.nickname) || '',
          images: (n.imageList || []).map(i => i.urlDefault || i.url || ''),
          video: (n.video && n.video.media && n.video.media.stream) ? n.video.media.stream : null
        });
      }
      out.state = JSON.parse(JSON.stringify(notes));
    }
  } catch (e) {}
  return out;
}
"""


_STATIC_RE = re.compile(r"douyinstatic\.com|/obj/douyin-pc-web/|/static/|\.webm\?|loading|placeholder", re.I)


def _post_id(url: str) -> str:
    """주소 속 게시물 ID(숫자 15자리 이상 또는 /p/·/post/·/explore/ 뒤 코드)."""
    m = re.search(r"/(?:post|p|reel|reels|explore|discovery/item|video|photo|note)/([\w-]{6,})", url)
    if m:
        return m.group(1)
    m = re.search(r"(\d{15,})", url)
    return m.group(1) if m else ""


def _threads_media(html: str, code: str) -> dict | None:
    """스레드 페이지의 JSON에서 해당 게시물(code)의 영상/사진을 찾는다."""
    found = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("code") == code and ("video_versions" in o or "image_versions2" in o or "carousel_media" in o):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    for m in re.finditer(r'<script type="application/json"[^>]*>(.*?)</script>', html, re.S):
        txt = m.group(1)
        if code not in txt:
            continue
        try:
            walk(json.loads(txt))
        except Exception:
            continue
    if not found:
        return None
    post = found[0]

    def one(item):
        vv = item.get("video_versions") or []
        if vv:
            return {"type": "video", "url": vv[0]["url"]}
        cands = (item.get("image_versions2") or {}).get("candidates") or []
        if cands:
            best = max(cands, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
            return {"type": "image", "url": best["url"]}
        return None

    items = []
    for it in (post.get("carousel_media") or [post]):
        x = one(it)
        if x:
            items.append(x)
    user = ((post.get("user") or {}).get("username")) or ""
    cap = ((post.get("caption") or {}).get("text")) or ""
    return {"items": items, "title": cap[:60], "uploader": user, "id": code}


def _naver_clip(d, seed: str | None, max_height: int) -> dict | None:
    """클립 뷰어 카드 JSON에서 해당 클립의 mp4(소리 포함)를 고른다."""
    contents = []

    def walk(o):
        if isinstance(o, dict):
            c = o.get("content")
            if isinstance(c, dict) and c.get("mediaId"):
                contents.append(c)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(d)
    if not contents:
        return None
    pick = next((c for c in contents if seed and c["mediaId"].upper() == seed.upper()), None)
    if pick is None:
        if seed:
            return None
        pick = contents[0]
    reps = []

    def walk2(o):
        if isinstance(o, dict):
            if "BaseURL" in o and "@height" in o:
                for u in o.get("BaseURL") or []:
                    if isinstance(u, str) and "/pd/" in u and ".mp4" in u:
                        reps.append((min(int(o.get("@height") or 0), int(o.get("@width") or 99999)), u))
            for v in o.values():
                walk2(v)
        elif isinstance(o, list):
            for v in o:
                walk2(v)

    walk2(pick)
    if not reps:
        return None
    # 화질 제한은 짧은 변 기준(세로 1080x1920 = 1080p)
    ok = [r for r in reps if not max_height or r[0] <= max_height]
    best = max(ok or reps)[1]
    return {"items": [{"type": "video", "url": best}],
            "title": (pick.get("title") or pick.get("description") or "").split("\n")[0][:60],
            "uploader": (pick.get("profile") or {}).get("nickname", ""), "id": pick["mediaId"][:12]}


def capture(url: str, wait_s: float = 9.0, max_height: int = 0) -> dict:
    """페이지를 열어 영상·사진 주소를 찾는다.
    반환: {items:[{type,url}], title, uploader, id, referer}"""
    from playwright.sync_api import sync_playwright
    net: list[tuple[str, str, int]] = []
    api_resps = []   # 더우인 상세 정보 응답

    def on_response(r):
        try:
            ct = (r.headers.get("content-type") or "").lower()
            u = r.url
            if "aweme/v1/web/aweme/detail" in u or "clipviewer/card" in u:
                api_resps.append(r)
                return
            if _STATIC_RE.search(u):
                return
            if "video/" in ct or "mpegurl" in ct or re.search(r"\.(mp4|m3u8)(\?|$)", u):
                net.append((u, ct, int(r.headers.get("content-length") or 0)))
        except Exception:
            pass

    with _pw_lock, sync_playwright() as p:
        use_cdp = cdp_alive()
        if use_cdp:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{paths.CDP_PORT}")
            ctx = browser.contexts[0]
        else:
            browser = p.chromium.launch(channel=_channel(), headless=True, args=["--mute-audio"])
            ctx = browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"),
                locale="ko-KR", viewport={"width": 1280, "height": 900})
            try:
                ctx.add_cookies([
                    {k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if k in c}
                    for c in json.loads(paths.COOKIES_JSON.read_text(encoding="utf-8"))
                ])
            except Exception:
                pass
        page = ctx.new_page()
        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            final_url = page.url
            deadline = time.time() + wait_s
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                try:
                    page.evaluate("document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})})")
                except Exception:
                    pass
            final_url = page.url
            dom = page.evaluate(_DOM_JS)
            html = page.content()
            api_json = []
            for r in api_resps:
                try:
                    api_json.append(r.json())
                except Exception:
                    pass
        finally:
            page.close()
            if use_cdp:
                browser.close()   # 연결만 끊음
            else:
                ctx.close()
                browser.close()

    result = {"items": [], "title": "", "uploader": "", "id": "", "referer": final_url}

    # 0) 원래 주소의 게시물 ID가 최종 주소에서 사라졌으면(삭제·비공개로 홈 이동) 실패 처리
    orig_id = _post_id(url)
    if orig_id and orig_id not in final_url and orig_id not in html[:2_000_000]:
        raise RuntimeError("게시물이 삭제됐거나 비공개예요. (다른 화면으로 이동됨)")

    # 1-a) 네이버 클립: 클립 뷰어 카드 정보(화질별 mp4)
    seed = (re.search(r"(?:seedMediaId|mediaId)=([0-9A-F]{20,})", url + " " + final_url, re.I) or [None, None])[1]
    for d in api_json:
        nc = _naver_clip(d, seed, max_height)
        if nc:
            return nc | {"referer": final_url}

    # 1-b) 더우인: 페이지가 받아온 상세 정보
    for d in api_json:
        aw = (d or {}).get("aweme_detail") or {}
        if not aw:
            continue
        v = aw.get("video") or {}
        imgs = aw.get("images") or []
        uploader = (aw.get("author") or {}).get("nickname", "")
        base = {"title": (aw.get("desc") or "")[:60], "uploader": uploader,
                "id": str(aw.get("aweme_id", "")), "referer": final_url}
        if imgs:
            urls = [(i.get("url_list") or [None])[-1] for i in imgs]
            return base | {"items": [{"type": "image", "url": u} for u in urls if u]}
        def short_side(b):
            pa = b.get("play_addr") or {}
            return min(pa.get("height") or 99999, pa.get("width") or 99999)
        rates = sorted(v.get("bit_rate") or [], key=lambda b: -(b.get("bit_rate") or 0))
        if max_height:
            rates = [b for b in rates if short_side(b) <= max_height] or rates[-1:]
        cand = []
        for b in rates:   # H.264 우선
            if not b.get("is_h265") and (b.get("play_addr") or {}).get("url_list"):
                cand.append(b["play_addr"]["url_list"][0])
                break
        if not cand and (v.get("play_addr") or {}).get("url_list"):
            cand.append(v["play_addr"]["url_list"][0])
        if cand:
            return base | {"items": [{"type": "video", "url": cand[0]}]}

    # 2) 스레드: 게시물 JSON
    m = re.search(r"threads\.(?:net|com)/@([^/]+)/post/([\w-]+)", final_url)
    if m:
        t = _threads_media(html, m.group(2))
        if t is not None:
            if not t["items"]:
                raise RuntimeError("이 스레드 게시물에는 영상·사진이 없어요.")
            t["uploader"] = t.get("uploader") or m.group(1)
            t["referer"] = final_url
            return t
        result["uploader"], result["id"] = m.group(1), m.group(2)

    # 2) 샤오홍슈: 페이지 상태값
    if dom.get("state"):
        n = dom["state"][0]
        items = []
        stream = n.get("video") or {}
        for codec in ("h264", "h265", "av1"):
            for s in stream.get(codec) or []:
                u = s.get("masterUrl") or (s.get("backupUrls") or [None])[0]
                if u:
                    items.append({"type": "video", "url": u})
                    break
            if items:
                break
        if not items:
            items = [{"type": "image", "url": u} for u in n.get("images") or [] if u]
        if items:
            return {"items": items, "title": n.get("title", "")[:60], "uploader": n.get("user", ""),
                    "id": n.get("id", ""), "referer": final_url}

    metas = dom.get("metas") or []
    title = next((v for k, v in metas if k == "og:title"), "") or dom.get("title", "")
    result["title"] = title[:60]

    # 3) <video> 태그의 직접 주소
    vids = [v for v in dom.get("videos") or [] if v.startswith("http")]
    # 4) 네트워크로 받은 영상 (크기 큰 순)
    netv = [u for u, ct, ln in sorted(net, key=lambda x: -x[2])]
    hls = [u for u in netv if "m3u8" in u or "mpegurl" in u]
    mp4 = [u for u in netv if u not in hls]
    ogv = [v for k, v in metas if k.startswith("og:video")]
    for u in vids + ogv + hls + mp4:
        u = re.sub(r"[&?]bytestart=\d+&byteend=\d+", "", u)
        result["items"].append({"type": "video", "url": u})
        break
    if not result["items"]:
        imgs = list(dict.fromkeys(v for k, v in metas if k in ("og:image", "twitter:image")))
        result["items"] = [{"type": "image", "url": u} for u in imgs]
    return result
