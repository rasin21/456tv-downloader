"""456tv 올인원 다운로더 실행 파일 (창 없이 서버를 켜고 앱 창을 연다)."""
import logging
import os
import socket
import subprocess
import sys
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import paths  # noqa: E402

URL = f"http://127.0.0.1:{paths.PORT}/"


def port_in_use() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", paths.PORT)) == 0


def open_window():
    chrome = paths.find_chrome()
    if chrome:
        app_profile = paths.DATA_DIR / "app-window"
        subprocess.Popen([chrome, f"--app={URL}", f"--user-data-dir={app_profile}", "--no-first-run",
                          "--no-default-browser-check", "--window-size=1040,860"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        webbrowser.open(URL)


def main():
    if port_in_use():          # 이미 켜져 있으면 창만 다시 연다
        open_window()
        return
    logging.basicConfig(filename=paths.LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    from app import server
    import threading
    threading.Thread(target=lambda: (time.sleep(0.8), open_window()), daemon=True).start()
    server.serve()


if __name__ == "__main__":
    main()
