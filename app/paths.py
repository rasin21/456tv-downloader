"""경로·설정 공용 모듈."""
import json
import os
import threading
from pathlib import Path

APP_NAME = "456tv 올인원 다운로더"
ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"
STATIC_DIR = APP_DIR / "static"
DATA_DIR = ROOT / "data"
PROFILE_DIR = DATA_DIR / "chrome-profile"      # 전용 브라우저(로그인) 프로필
COOKIES_TXT = DATA_DIR / "cookies.txt"          # yt-dlp / gallery-dl 용
COOKIES_JSON = DATA_DIR / "cookies.json"        # 브라우저 캡처용
SETTINGS_FILE = DATA_DIR / "settings.json"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = DATA_DIR / "app.log"
VENV_SCRIPTS = ROOT / ".venv" / "Scripts"

PORT = 18456          # 다운로더 화면
CDP_PORT = 18457      # 전용 브라우저 원격 제어(로컬 전용)

DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "save_dir": str(Path(os.path.expanduser("~")) / "Downloads" / APP_NAME),
    "quality": "best",        # best | 1080 | 720 | 480
    "mode": "video",          # video | mp3 | both
    "subs": False,
    "concurrent": 2,
}

_lock = threading.Lock()


def load_settings() -> dict:
    with _lock:
        s = dict(DEFAULT_SETTINGS)
        try:
            s.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
        return s


def save_settings(new: dict) -> dict:
    s = load_settings()
    for k in DEFAULT_SETTINGS:
        if k in new:
            s[k] = new[k]
    try:
        s["concurrent"] = max(1, min(4, int(s["concurrent"])))
    except Exception:
        s["concurrent"] = 2
    with _lock:
        SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return s


def find_chrome() -> str | None:
    cands = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for c in cands:
        if os.path.isfile(c):
            return c
    return None
