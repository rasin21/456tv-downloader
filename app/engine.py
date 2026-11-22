"""다운로드 엔진: yt-dlp → gallery-dl → 브라우저 캡처 순으로 시도."""
import glob
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yt_dlp

from . import browser, paths

log = logging.getLogger("engine")

PLATFORM_NAMES = {
    "youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram", "threads": "Threads",
    "douyin": "Douyin", "xiaohongshu": "Xiaohongshu", "naver": "Naver", "other": "기타",
}
COOKIE_PLATFORMS = {"tiktok", "instagram", "threads", "douyin", "xiaohongshu", "naver"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")


def detect_platform(url: str) -> str:
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    if "youtube.com" in h or h == "youtu.be":
        return "youtube"
    if "tiktok" in h:
        return "tiktok"
    if "instagram" in h:
        return "instagram"
    if "threads.net" in h or "threads.com" in h:
        return "threads"
    if "douyin" in h:
        return "douyin"
    if "xiaohongshu" in h or "xhslink" in h or "rednote" in h:
        return "xiaohongshu"
    if "naver" in h:
        return "naver"
    return "other"


def resolve_short(url: str) -> str:
    """naver.me, xhslink, v.douyin 등 짧은 주소를 실제 주소로."""
    h = (urllib.parse.urlparse(url).hostname or "").lower()
    if not any(s in h for s in ("naver.me", "xhslink", "v.douyin", "vt.tiktok", "vm.tiktok")):
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.geturl()
    except Exception:
        return url


def safe_name(s: str, n: int = 80) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s[:n].rstrip(" .") or "untitled"


# ---------------------------------------------------------------- 작업
class Job:
    def __init__(self, url: str, mode: str, quality: str, subs: bool):
        self.id = uuid.uuid4().hex[:10]
        self.url = url.strip()
        self.mode, self.quality, self.subs = mode, quality, subs
        self.platform = detect_platform(self.url)
        self.state = "queued"          # queued | running | done | error
        self.progress = 0.0
        self.message = "대기 중"
        self.title = ""
        self.files: list[str] = []
        self.folder = ""
        self.created = time.time()

    def to_dict(self):
        return {k: getattr(self, k) for k in (
            "id", "url", "mode", "quality", "subs", "platform", "state", "progress",
            "message", "title", "files", "folder", "created")} | {
            "platform_name": PLATFORM_NAMES.get(self.platform, "기타")}


class Manager:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=paths.load_settings()["concurrent"])

    def set_workers(self, n: int):
        self.pool._max_workers = n   # 다음 작업부터 반영

    def add(self, urls: list[str], mode: str, quality: str, subs: bool) -> list[Job]:
        out = []
        for u in urls:
            u = u.strip()
            if not re.match(r"https?://", u):
                m = re.search(r"https?://\S+", u)   # 공유 문구 속 링크 추출
                if not m:
                    continue
                u = m.group(0)
            j = Job(u, mode, quality, subs)
            with self.lock:
                self.jobs[j.id] = j
            self.pool.submit(self._run, j)
            out.append(j)
        return out

    def retry(self, jid: str):
        j = self.jobs.get(jid)
        if j and j.state == "error":
            j.state, j.progress, j.message, j.files = "queued", 0, "대기 중", []
            self.pool.submit(self._run, j)

    def clear_finished(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j.state in ("done", "error")]:
                del self.jobs[k]

    def active(self) -> bool:
        return any(j.state in ("queued", "running") for j in self.jobs.values())

    def snapshot(self):
        with self.lock:
            return [j.to_dict() for j in sorted(self.jobs.values(), key=lambda j: -j.created)]

    # ------------------------------------------------------------ 실행
    def _run(self, j: Job):
        j.state, j.message = "running", "준비 중"
        try:
            j.url = resolve_short(j.url)
            j.platform = detect_platform(j.url)
            # 틱톡 추천·팔로잉 화면 주소 → 전용 브라우저에서 지금 보는 영상으로 바꾼다
            if j.platform == "tiktok" and not re.search(r"/(video|photo)/\d+", j.url):
                j.message = "전용 브라우저에서 지금 보는 영상 찾는 중"
                real = browser.current_tiktok_video(j.url)
                if not real:
                    raise RuntimeError(
                        "FEED|틱톡 추천 화면 주소에는 어떤 영상인지 정보가 없어요. 전용 브라우저에서 그 영상을 띄워 둔 채로 "
                        "[다시 시도]를 누르거나, 영상의 [공유 → 링크 복사] 주소를 넣어주세요.")
                j.url = real
            s = paths.load_settings()
            outdir = Path(s["save_dir"]) / PLATFORM_NAMES.get(j.platform, "기타")
            outdir.mkdir(parents=True, exist_ok=True)
            j.folder = str(outdir)
            if j.platform in COOKIE_PLATFORMS:
                j.message = "로그인 정보 확인 중"
                browser.refresh_cookies()

            errors = []
            ok = False
            # 스레드·네이버 클립은 yt-dlp 미지원 → 바로 브라우저 캡처
            direct_capture = j.platform == "threads" or (
                j.platform == "naver" and not re.search(r"tv(cast)?\.naver\.com/(v|embed)/\d+", j.url))
            if not direct_capture:
                try:
                    ok = self._ytdlp(j, j.url, outdir)
                except Exception as e:
                    errors.append(f"yt-dlp: {_short_err(e)}")
            if not ok and j.platform in ("instagram", "tiktok"):
                try:
                    ok = self._gallery(j, outdir)
                except Exception as e:
                    errors.append(f"gallery-dl: {_short_err(e)}")
            # 유튜브·틱톡은 페이지 분석을 하지 않는다(엉뚱한 추천 영상을 받을 수 있음)
            if not ok and j.platform not in ("youtube", "tiktok"):
                try:
                    ok = self._capture(j, outdir)
                except Exception as e:
                    errors.append(f"브라우저: {_short_err(e)}")
            if not ok:
                raise RuntimeError(" / ".join(errors) or "영상·사진을 찾지 못했습니다.")

            if j.subs:
                self._make_transcripts(j)
            j.state, j.progress = "done", 100
            j.message = f"완료 · 파일 {len(j.files)}개"
            if j.subs and not any(f.lower().endswith(".srt") for f in j.files):
                j.message += " · 이 영상엔 자막이 없어요"
        except Exception as e:
            log.exception("job failed %s", j.url)
            j.state = "error"
            j.message = friendly_error(str(e), j.platform)

    # ------------------------------------------------------------ yt-dlp
    def _ytdlp(self, j: Job, url: str, outdir: Path, title_hint: str = "", headers: dict | None = None,
               subs: bool | None = None) -> bool:
        subs = j.subs if subs is None else subs
        before = set(glob.glob(str(outdir / "*")))
        q = j.quality
        fmt_sort = ["vcodec:h264", "acodec:m4a"]
        if q in ("1080", "720", "480"):
            fmt_sort.insert(0, f"res:{q}")
        opts = {
            "outtmpl": {"default": str(outdir / (
                (safe_name(title_hint, 140).replace("%", "%%") + ".%(ext)s") if title_hint else
                "%(uploader,channel,creator|unknown).40B - %(title,fulltitle|untitled).80B [%(id)s].%(ext)s"))},
            "windowsfilenames": True,
            "noplaylist": True,
            "quiet": True, "no_warnings": True, "noprogress": True,
            "progress_hooks": [lambda d: self._hook(j, d)],
            "postprocessor_hooks": [lambda d: self._pphook(j, d)],
            "merge_output_format": "mp4",
            "format_sort": fmt_sort,
            "js_runtimes": {"node": {}},
            "retries": 5, "fragment_retries": 5,
            "concurrent_fragment_downloads": 4,
            "overwrites": False,
            "logger": _YLog(),
        }
        if headers:
            opts["http_headers"] = headers
        if j.platform in COOKIE_PLATFORMS and paths.COOKIES_TXT.exists():
            opts["cookiefile"] = str(paths.COOKIES_TXT)
        pps = []
        if j.mode == "mp3":
            opts["format"] = "ba/b"
            pps.append({"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"})
        else:
            opts["format"] = "bv*+ba/b"
        if subs:
            opts.update({"writesubtitles": True, "writeautomaticsub": True, "subtitlesformat": "srt/vtt/best"})
            pps.append({"key": "FFmpegSubtitlesConvertor", "format": "srt"})
        opts["postprocessors"] = pps

        with yt_dlp.YoutubeDL(opts) as ydl:
            j.message = "정보 가져오는 중"
            ie = ydl.extract_info(url, download=False, process=False)
            if ie.get("_type") == "playlist":
                raise RuntimeError("재생목록·채널 주소입니다. 영상 하나의 주소를 넣어주세요.")
            j.title = j.title or ie.get("title") or ""
            if subs:
                ydl.params["subtitleslangs"] = _pick_sub_langs(ie)
            info = ydl.process_ie_result(ie, download=True)
            vid = "" if title_hint else info.get("id", "")
        after = set(glob.glob(str(outdir / "*")))
        new = sorted(after - before)
        if vid:
            pat = str(outdir / f"*{glob.escape('[' + vid + ']')}*")
            new = sorted(set(new) | set(glob.glob(pat)))
        new = [f for f in new if not f.endswith((".part", ".ytdl", ".temp"))
               and not any(re.fullmatch(r"\.f\d+(-\d+)?", sfx) for sfx in Path(f).suffixes)]
        # 영상+MP3: 받은 영상에서 MP3를 따로 만든다
        if j.mode == "both":
            for f in list(new):
                if f.lower().endswith((".mp4", ".mkv", ".webm", ".mov")):
                    mp3 = str(Path(f).with_suffix(".mp3"))
                    if not os.path.exists(mp3):
                        j.message = "MP3 만드는 중"
                        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", f, "-vn", "-c:a", "libmp3lame",
                                        "-b:a", "192k", mp3], capture_output=True,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.path.exists(mp3):
                        new.append(mp3)
        j.files = sorted(set(j.files) | set(new))
        has_media = any(f.lower().endswith((".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".mov")) for f in j.files)
        if not has_media:
            raise RuntimeError("영상 파일이 만들어지지 않았습니다.")
        return True

    def _hook(self, j: Job, d: dict):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                j.progress = round(min(99, done * 100 / total), 1)
            sp = d.get("speed") or 0
            j.message = f"받는 중 {j.progress:.0f}%" + (f" · {sp / 1048576:.1f}MB/s" if sp else "")
        elif d.get("status") == "finished":
            j.message = "받기 완료, 정리 중"

    def _pphook(self, j: Job, d: dict):
        if d.get("status") == "started":
            name = d.get("postprocessor", "")
            j.message = {"Merger": "영상·소리 합치는 중", "ExtractAudio": "MP3 만드는 중",
                         "FFmpegSubtitlesConvertor": "자막 변환 중"}.get(name, "정리 중")

    # ------------------------------------------------------------ gallery-dl (사진)
    def _gallery(self, j: Job, outdir: Path) -> bool:
        j.message = "사진 게시물 받는 중"
        exe = paths.VENV_SCRIPTS / "gallery-dl.exe"
        cmd = [str(exe), "-D", str(outdir), "-f", "{category}_{id|post_id|media_id}_{num:>02}.{extension}",
               "--no-mtime"]
        if paths.COOKIES_TXT.exists():
            cmd += ["--cookies", str(paths.COOKIES_TXT)]
        cmd.append(j.url)
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        files = []
        for line in r.stdout.splitlines():
            p = line.strip().lstrip("# ").strip()
            if p and os.path.isfile(p):
                files.append(p)
        if not files:
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "받은 파일 없음")
        j.files = sorted(set(j.files) | set(files))
        return True

    # ------------------------------------------------------------ 브라우저 캡처
    def _capture(self, j: Job, outdir: Path) -> bool:
        j.message = "브라우저로 페이지 분석 중"
        mh = int(j.quality) if j.quality.isdigit() else 0
        res = browser.capture(j.url, wait_s=12 if j.platform == "douyin" else 9, max_height=mh)
        items = res.get("items") or []
        if not items:
            raise RuntimeError("페이지에서 영상·사진을 찾지 못했습니다.")
        j.title = j.title or res.get("title", "")
        base = safe_name(f"{res.get('uploader') or PLATFORM_NAMES[j.platform]} - {res.get('title') or 'untitled'}", 100)
        pid = res.get("id") or uuid.uuid4().hex[:8]
        headers = {"User-Agent": UA, "Referer": res.get("referer") or j.url}
        videos = [i for i in items if i["type"] == "video"]
        images = [i for i in items if i["type"] == "image"]
        for n, it in enumerate(videos, 1):
            hint = f"{base} [{pid}]" + (f"_{n:02d}" if len(videos) > 1 else "")
            self._ytdlp(j, it["url"], outdir, title_hint=hint, headers=headers, subs=False)
        if images and j.mode != "mp3":
            for n, it in enumerate(images, 1):
                j.message = f"사진 받는 중 {n}/{len(images)}"
                ext = _img_ext(it["url"])
                dest = outdir / f"{base} [{pid}]_{n:02d}{ext}"
                _download(it["url"], dest, headers)
                j.files.append(str(dest))
                j.progress = n * 100 / len(images)
        return bool(j.files)

    # ------------------------------------------------------------ 대본
    def _make_transcripts(self, j: Job):
        for f in list(j.files):
            if f.lower().endswith(".srt"):
                txt = Path(f).with_suffix(".txt")
                lines, last = [], None
                for line in Path(f).read_text(encoding="utf-8", errors="replace").splitlines():
                    s = line.strip()
                    if not s or s.isdigit() or "-->" in s:
                        continue
                    s = re.sub(r"<[^>]+>", "", s)
                    if s != last:
                        lines.append(s)
                        last = s
                txt.write_text("\n".join(lines), encoding="utf-8")
                j.files.append(str(txt))


# ---------------------------------------------------------------- 도우미
class _YLog:
    def debug(self, m):
        pass

    def info(self, m):
        pass

    def warning(self, m):
        log.info("yt-dlp warn: %s", m)

    def error(self, m):
        log.warning("yt-dlp error: %s", m)


def _pick_sub_langs(ie: dict) -> list[str]:
    want = ("ko", "en", "ja", "zh")
    manual = list((ie.get("subtitles") or {}).keys())
    langs = [k for k in manual if k.split("-")[0].lower() in want and k != "live_chat"]
    if langs:
        return langs[:4]
    auto = list((ie.get("automatic_captions") or {}).keys())
    langs = [k for k in auto if k.endswith("-orig")][:1]
    if "ko" in auto:
        langs.append("ko")
    return langs or ["ko", "en", "ja", "zh.*"]


def _img_ext(url: str) -> str:
    m = re.search(r"\.(jpe?g|png|webp|heic|gif)(?:[?#]|$)", url, re.I)
    return "." + (m.group(1).lower() if m else "jpg")


def _download(url: str, dest: Path, headers: dict):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fp:
        while chunk := r.read(1 << 16):
            fp.write(chunk)


def _short_err(e: Exception) -> str:
    s = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")
    return s.splitlines()[0][:200] if s else type(e).__name__


def friendly_error(msg: str, platform: str) -> str:
    low = msg.lower()
    if msg.startswith("FEED|"):
        return msg[5:]
    if "영상·사진이 없어요" in msg or "삭제됐거나 비공개" in msg:
        return re.sub(r"^.*?브라우저: ", "", msg)
    if any(k in low for k in ("ip address is blocked", "not available in your country", "geo", "video unavailable",
                              "has been removed", "no longer available", "no results for")):
        return "삭제됐거나, 비공개이거나, 한국에서 볼 수 없는 영상이에요."
    if any(k in low for k in ("login", "log in", "cookies", "private", "rate-limit", "authentication", "sign in")):
        return "로그인이 필요해요. [전용 브라우저]에서 로그인한 뒤 다시 시도해 주세요. (" + msg[:120] + ")"
    if "재생목록" in msg:
        return msg
    if "unsupported url" in low:
        return "지원하지 않는 주소예요. 영상 하나의 주소인지 확인해 주세요."
    return "실패: " + msg[:220]


def engine_versions() -> dict:
    import importlib.metadata as md
    out = {}
    for p in ("yt-dlp", "gallery-dl", "playwright"):
        try:
            out[p] = md.version(p)
        except Exception:
            out[p] = "?"
    return out
