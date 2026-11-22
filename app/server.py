"""로컬 전용 웹 서버(127.0.0.1) — 다운로더 화면과 API."""
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import browser, engine, paths, translate

log = logging.getLogger("server")
manager = engine.Manager()
_last_seen = time.time()
_update_state = {"running": False, "message": ""}
_cookie_refresh = threading.Lock()
ALLOWED_HOSTS = {f"127.0.0.1:{paths.PORT}", f"localhost:{paths.PORT}"}


def _state():
    return {
        "jobs": manager.snapshot(),
        "settings": paths.load_settings(),
        "login": browser.login_status(),
        "browser_open": browser.cdp_alive(),
        "versions": engine.engine_versions(),
        "update": _update_state,
    }


def _inside_save_dir(p: str) -> bool:
    try:
        root = Path(paths.load_settings()["save_dir"]).resolve()
        return Path(p).resolve().is_relative_to(root)
    except Exception:
        return False


def _update_engine():
    _update_state.update(running=True, message="업데이트 중…")
    try:
        r = subprocess.run([str(paths.VENV_SCRIPTS / "python.exe"), "-m", "pip", "install", "-U", "-q",
                            "yt-dlp[default,curl-cffi]", "gallery-dl"],
                           capture_output=True, text=True, timeout=900,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _update_state["message"] = "업데이트 완료 · 프로그램을 다시 켜면 적용돼요" if r.returncode == 0 else "업데이트 실패: " + r.stderr[-200:]
    except Exception as e:
        _update_state["message"] = f"업데이트 실패: {e}"
    finally:
        _update_state["running"] = False


class Handler(BaseHTTPRequestHandler):
    server_version = "456tv"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def do_GET(self):
        global _last_seen
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._send(200, (paths.STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/login":
            return self._send(200, (paths.STATIC_DIR / "login.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/icon.png":
            return self._send(200, (paths.STATIC_DIR / "icon.png").read_bytes(), "image/png")
        if path == "/api/state":
            _last_seen = time.time()
            return self._send(200, _state())
        if path == "/api/login-status":
            if _cookie_refresh.acquire(blocking=False):
                def run():
                    try:
                        browser.refresh_cookies()
                    finally:
                        _cookie_refresh.release()
                threading.Thread(target=run, daemon=True).start()
            return self._send(200, {"login": browser.login_status()})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        # 다른 웹사이트가 몰래 요청하지 못하도록 Host·전용 헤더 확인
        if not self._host_ok() or self.headers.get("X-456tv") != "1":
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        path = self.path.split("?")[0]
        try:
            if path == "/api/jobs":
                urls = [u for u in (body.get("urls") or []) if u.strip()]
                s = paths.save_settings({k: body[k] for k in ("mode", "quality", "subs") if k in body})
                jobs = manager.add(urls, s["mode"], s["quality"], bool(s["subs"]))
                return self._send(200, {"ok": True, "added": len(jobs)})
            if path == "/api/jobs/retry":
                manager.retry(body.get("id", ""))
                return self._send(200, {"ok": True})
            if path == "/api/jobs/clear":
                manager.clear_finished()
                return self._send(200, {"ok": True})
            if path == "/api/open":
                p = body.get("path") or paths.load_settings()["save_dir"]
                if not _inside_save_dir(p):
                    return self._send(400, {"error": "저장 폴더 밖의 경로입니다."})
                Path(p).mkdir(parents=True, exist_ok=True) if not Path(p).suffix else None
                if body.get("select") and os.path.isfile(p):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
                else:
                    os.startfile(os.path.normpath(p))
                return self._send(200, {"ok": True})
            if path == "/api/login/open":
                browser.open_browser(f"http://127.0.0.1:{paths.PORT}/login")
                return self._send(200, {"ok": True})
            if path == "/api/browser/open":
                url = body.get("url", "")
                if not url.startswith(("https://", "http://")):
                    return self._send(400, {"error": "잘못된 주소"})
                browser.open_browser(url)
                return self._send(200, {"ok": True})
            if path == "/api/translate":
                kw = (body.get("keyword") or "").strip()
                if not kw:
                    return self._send(400, {"error": "키워드를 입력하세요."})
                return self._send(200, {"ok": True, "result": translate.translate_all(kw)})
            if path == "/api/search":
                site, kw = body.get("site", ""), (body.get("keyword") or "").strip()
                if site not in translate.SEARCH or not kw:
                    return self._send(400, {"error": "검색어를 확인하세요."})
                browser.open_browser(translate.search_url(site, kw))
                return self._send(200, {"ok": True})
            if path == "/api/settings":
                s = paths.save_settings(body)
                manager.set_workers(s["concurrent"])
                return self._send(200, {"ok": True, "settings": s})
            if path == "/api/pick-folder":
                return self._send(200, {"ok": True, "path": _pick_folder()})
            if path == "/api/update-engine":
                if not _update_state["running"]:
                    threading.Thread(target=_update_engine, daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/quit":
                self._send(200, {"ok": True})
                threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0)), daemon=True).start()
                return
        except Exception as e:
            log.exception("api error")
            return self._send(500, {"error": str(e)})
        self._send(404, {"error": "not found"})


def _pick_folder() -> str:
    """Windows 폴더 선택 창."""
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$f=New-Object System.Windows.Forms.FolderBrowserDialog;"
          "$f.Description='저장할 폴더를 고르세요';"
          "if($f.ShowDialog() -eq 'OK'){[Console]::OutputEncoding=[Text.Encoding]::UTF8;$f.SelectedPath}")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True,
                       text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.stdout.strip()


def _idle_watch():
    """화면이 닫히고(3분간 신호 없음) 진행 중인 다운로드도 없으면 스스로 종료."""
    while True:
        time.sleep(30)
        if time.time() - _last_seen > 180 and not manager.active():
            log.info("idle exit")
            os._exit(0)


def serve():
    httpd = ThreadingHTTPServer(("127.0.0.1", paths.PORT), Handler)
    threading.Thread(target=_idle_watch, daemon=True).start()
    log.info("serving on %s", paths.PORT)
    httpd.serve_forever()
