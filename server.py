
"""
Web 控制台：多账号并发跑评教。
标准库实现（http.server），不引额外框架，方便 PyInstaller 打包。

启动：python server.py [--port 8765] [--no-browser]
"""

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from utils import PROFILES

def _prepare_browser():
    """
    浏览器目录必须在 playwright 被导入之前定好。
    - 打包成 exe 后 Playwright 默认去 _internal/playwright/driver/package/.local-browsers
      找，那里是空的，所以要指到用户目录
    - 内置在 exe 里的浏览器首次运行时释放到该目录，之后直接复用
    """
    la = os.environ.get("LOCALAPPDATA") or str(pathlib.Path.home() / "AppData" / "Local")
    target = pathlib.Path(la) / "njit-pj-browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(target)

    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    src = pathlib.Path(base) / "browsers"
    if not src.exists():
        return
    try:
        dirs = [d for d in src.iterdir() if d.is_dir()]
    except Exception:
        return
    missing = [d for d in dirs if not (target / d.name).exists()]
    if not missing:
        return

    print("首次运行，正在释放内置浏览器（%d 个组件，约 280MB）..." % len(missing), flush=True)
    target.mkdir(parents=True, exist_ok=True)
    for d in missing:
        try:
            shutil.copytree(d, target / d.name, dirs_exist_ok=True)
        except Exception as e:
            print("释放 %s 失败：%s" % (d.name, e), flush=True)
    print("浏览器就绪：%s" % target, flush=True)

_prepare_browser()

ROOT = pathlib.Path(__file__).resolve().parent

def app_dir():
    """exe 打包后取 exe 所在目录，源码运行时取脚本目录。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return ROOT

def res_path(rel):
    """只读资源（web/）在打包后位于 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return pathlib.Path(base) / rel
    return ROOT / rel

APP = app_dir()
DATA = APP / "data"
DATA.mkdir(exist_ok=True)
ACCOUNTS_FILE = DATA / "accounts.json"
SETTINGS_FILE = DATA / "settings.json"

DEFAULT_SETTINGS = {
    "concurrency": 3,
    "auto_submit": True,
    "skip_submitted": True,
    "dry_run": False,
    "gc_enabled": True,
    "grade_mode": False,
    "grade_default": "A",
    "grade_map": "",
    "link_mode": "auto",
    "cdp_port": 0,
    "cdp_mode": False,
    "delay_min": 700,
    "delay_max": 1600,
    "comment_enabled": True,
    "max_comment_len": 200,
}

class Bus:
    def __init__(self):
        self.events = []
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self.seq = 0

    def push(self, ev):
        with self.cond:
            self.seq += 1
            ev["seq"] = self.seq
            ev["ts"] = time.strftime("%H:%M:%S")
            self.events.append(ev)
            if len(self.events) > 8000:
                del self.events[:2000]
            self.cond.notify_all()

    def wait_since(self, idx, timeout=15.0):
        with self.cond:
            if self.seq <= idx:
                self.cond.wait(timeout)
            return self.events[idx:] if idx < len(self.events) else []

BUS = Bus()

TABS = {}
TABS_LOCK = threading.Lock()
TABS_SEEN = {"any": False}

def tab_touch(tab):
    if not tab:
        return
    with TABS_LOCK:
        TABS[tab] = time.time()
        TABS_SEEN["any"] = True

def tab_leave(tab):
    with TABS_LOCK:
        TABS.pop(tab, None)

def tabs_alive(ttl=12.0):
    now = time.time()
    with TABS_LOCK:
        for k in [k for k, v in TABS.items() if now - v > ttl]:
            TABS.pop(k, None)
        return len(TABS)

def watchdog(grace=4.0):
    """页面全关后等 grace 秒（容忍刷新），仍然没有页面就退出进程。"""
    idle_since = None
    while True:
        time.sleep(1.0)
        if not TABS_SEEN["any"]:
            continue
        if tabs_alive() > 0:
            idle_since = None
            continue
        if idle_since is None:
            idle_since = time.time()
        elif time.time() - idle_since > grace:
            print("页面已全部关闭，程序退出", flush=True)
            try:
                STOP_EVENT.set()
            except Exception:
                pass
            os._exit(0)

def open_ui(url):
    """优先用 Edge/Chrome 的 app 模式开独立窗口——关掉窗口就等于关程序。"""
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for exe in cands:
        if not os.path.exists(exe):
            continue
        try:
            subprocess.Popen(
                [exe, "--app=%s" % url, "--window-size=1280,880",
                 "--no-first-run", "--no-default-browser-check"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return exe
        except Exception:
            continue
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return None

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, obj):
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

ACCOUNTS = load_json(ACCOUNTS_FILE, [])
SETTINGS = dict(DEFAULT_SETTINGS)
SETTINGS.update(load_json(SETTINGS_FILE, {}))

STATE = {}
STOP_EVENT = threading.Event()
RUNNING = {}
SEM = None
STATE_LOCK = threading.Lock()

def init_state():
    for a in ACCOUNTS:
        STATE.setdefault(a["id"], {"state": "idle", "done": 0, "total": 0, "message": ""})

def parse_grade_map(text):
    """把「教师名:A」这样的文本解析成字典，一行一条。"""
    m = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for sep in (":", "：", "=", ",", "，"):
            if sep in line:
                k, v = line.split(sep, 1)
                k, v = k.strip(), v.strip().upper()[:1]
                if k and v:
                    m[k] = v
                break
    return m

def base_cfg():
    """从 config.json 读站点配置，再叠加界面上的设置。"""
    from utils import config_file
    cfg = load_json(config_file(), {})
    cfg.update({
        "auto_submit": SETTINGS["auto_submit"],
        "skip_submitted": SETTINGS["skip_submitted"],
        "dry_run": SETTINGS["dry_run"],
        "gc_enabled": SETTINGS["gc_enabled"],
        "grade_mode": SETTINGS["grade_mode"],
        "grade_default": SETTINGS["grade_default"],
        "grade_map_parsed": parse_grade_map(SETTINGS.get("grade_map", "")),
        "link_mode": SETTINGS.get("link_mode", "auto"),
        "cdp_port": 9333 if SETTINGS.get("cdp_mode") else 0,
        "delay_min": SETTINGS["delay_min"],
        "delay_max": SETTINGS["delay_max"],
        "comment_enabled": SETTINGS["comment_enabled"],
        "max_comment_len": SETTINGS["max_comment_len"],
        "headless": True,
        "login_mode": "auto",
        "captcha_retry": 6,
        "form_wait_ms": 25000,
    })
    return cfg

def api_teachers(account_id):
    """教师列表来自上次运行缓存的 data/teachers_<id>.json，不用重新登录。"""
    acc = None
    for a in ACCOUNTS:
        if a["id"] == account_id:
            acc = a
            break
    if acc is None and ACCOUNTS:
        acc = ACCOUNTS[0]
    if acc is None:
        return {"ok": False, "msg": "还没有添加账号"}

    cache = DATA / ("teachers_%s.json" % acc["id"])
    if not cache.exists():
        return {"ok": False,
                "msg": "还没有教师列表，先点一次「开始」跑一遍（勾上试跑更快），回来再读"}
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return {"ok": True, "teachers": data, "cached": True,
                "account": acc.get("name") or acc["username"]}
    except Exception as e:
        return {"ok": False, "msg": "缓存读取失败：%s" % str(e)[:80]}

def api_outer_login(account_id):
    """打开可见浏览器，让用户手动过滑块登录一次；profile 存下来，以后自动复用。"""
    import time as _t
    import pj
    from playwright.sync_api import sync_playwright

    acc = None
    for a in ACCOUNTS:
        if a["id"] == account_id:
            acc = a
            break
    if acc is None and ACCOUNTS:
        acc = ACCOUNTS[0]
    if acc is None:
        return {"ok": False, "msg": "还没有添加账号"}

    cfg = base_cfg()
    url = cfg.get("outer_gc_url")
    if not url:
        return {"ok": False, "msg": "config.json 里没有 outer_gc_url"}

    PROFILES.mkdir(parents=True, exist_ok=True)
    prof = PROFILES / acc["id"]
    try:
        with sync_playwright() as p:

            ctx = None
            last_err = None
            for ch in ("msedge", "chrome", None):
                try:
                    kw = dict(user_data_dir=str(prof), headless=False,
                              no_viewport=True, args=["--start-maximized"])
                    if ch:
                        kw["channel"] = ch
                    ctx = p.chromium.launch_persistent_context(**kw)
                    break
                except Exception as e:
                    last_err = e
            if ctx is None:
                return {"ok": False, "msg": "起不来浏览器：%s" % str(last_err)[:100]}
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                for _ in range(360):
                    _t.sleep(1)
                    for pg in ctx.pages:
                        try:
                            u = pg.url
                        except Exception:
                            continue
                        if "/jwglxt" in u and "enlink" not in u and "authserver" not in u:
                            _t.sleep(2000)
                            return {"ok": True, "msg": "外链登录成功，已保存，以后不用再登"}
                return {"ok": False, "msg": "等待超时（6 分钟），窗口已关闭"}
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
    except Exception as e:
        return {"ok": False, "msg": str(e)[:150]}

CDP_PORT = 9333

def find_browser_exe():
    cands = [
        r"C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe",
        r"C://Program Files//Microsoft//Edge//Application//msedge.exe",
        r"C://Program Files//Google//Chrome//Application//chrome.exe",
        r"C://Program Files (x86)//Google//Chrome//Application//chrome.exe",
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    return None

def _browser_running(names=("msedge.exe", "chrome.exe")):
    for n in names:
        try:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq " + n],
                                 capture_output=True, text=True,
                                 encoding="gbk", errors="ignore").stdout
            if n.lower() in (out or "").lower():
                return n
        except Exception:
            pass
    return None

def api_cdp_launch(system_profile=False):
    """开一个带调试端口的浏览器，用户在里面手动走到过程评价页，程序再接管。
    system_profile=True 时用本机 Edge/Chrome 的默认用户目录，能带上你平时的登录态。"""
    exe = find_browser_exe()
    if not exe:
        return {"ok": False, "msg": "没找到 Edge 或 Chrome"}

    if system_profile:
        run = _browser_running()
        if run:
            return {"ok": False,
                    "msg": "请先把所有 %s 窗口关掉（含托盘里的），再点一次。\n"
                           "带调试端口启动时浏览器必须是完全关闭状态。" % run}
        la = os.environ.get("LOCALAPPDATA") or ""
        prof = pathlib.Path(la) / "Microsoft" / "Edge" / "User Data"
        if not prof.exists():
            prof = pathlib.Path(la) / "Google" / "Chrome" / "User Data"
        if not prof.exists():
            return {"ok": False, "msg": "没找到系统浏览器的用户目录"}
    else:
        prof = DATA / "cdp_profile"
        prof.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen(
            [exe, "--remote-debugging-port=%d" % CDP_PORT,
             "--user-data-dir=%s" % str(prof),
             "--profile-directory=Default",
             "--no-first-run", "--no-default-browser-check",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"ok": False, "msg": str(e)[:120]}

    return {"ok": True, "port": CDP_PORT, "system_profile": bool(system_profile),
            "msg": "浏览器已打开：在里面登录外链、一路点到「过程评价」页面，然后回来点「开始」"}

def api_cdp_check():
    """检查调试端口是否活着、有几个页面。"""
    try:
        import urllib.request as _u
        with _u.urlopen("http://127.0.0.1:%d/json/version" % CDP_PORT, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        with _u.urlopen("http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=3) as r:
            tabs = json.loads(r.read().decode("utf-8"))
        pages = [t for t in tabs if t.get("type") == "page"]
        return {"ok": True, "browser": data.get("Browser", ""),
                "pages": [{"title": t.get("title", "")[:40], "url": t.get("url", "")[:100]}
                          for t in pages]}
    except Exception as e:
        return {"ok": False, "msg": str(e)[:100]}

def emit(ev):
    BUS.push(ev)

def _worker(acc, cfg):
    from runner import AccountRunner

    aid = acc["id"]
    try:
        with SEM:
            if STOP_EVENT.is_set():
                with STATE_LOCK:
                    STATE[aid] = {"state": "stopped", "done": 0, "total": 0, "message": "未开始"}
                emit({"type": "status", "id": aid, "state": "stopped"})
                return

            def on_event(ev):
                t = ev.get("type")
                if t == "log":
                    BUS.push(ev)
                elif t == "status":
                    with STATE_LOCK:
                        cur = STATE.get(aid, {})
                        cur["state"] = ev.get("state", cur.get("state", ""))
                        if "done" in ev:
                            cur["done"] = ev["done"]
                        if "total" in ev:
                            cur["total"] = ev["total"]
                        if "message" in ev:
                            cur["message"] = ev["message"]
                        STATE[aid] = cur
                    BUS.push(ev)
                elif t == "progress":
                    with STATE_LOCK:
                        cur = STATE.get(aid, {})
                        cur["done"] = ev.get("done", 0)
                        cur["total"] = ev.get("total", 0)
                        STATE[aid] = cur
                    BUS.push(ev)

            AccountRunner(acc, cfg, on_event, STOP_EVENT).run()
    except Exception as e:
        BUS.push({"type": "log", "id": aid, "text": "线程异常：%s" % e})
        BUS.push({"type": "log", "id": aid, "text": traceback.format_exc()[-600:]})
        with STATE_LOCK:
            STATE[aid] = {"state": "failed", "done": 0, "total": 0, "message": str(e)[:150]}
        BUS.push({"type": "status", "id": aid, "state": "failed"})
    finally:
        RUNNING.pop(aid, None)

def start_run(ids=None):
    global SEM
    if RUNNING:
        return {"ok": False, "msg": "已有任务在跑，先停止"}
    targets = [a for a in ACCOUNTS if (not ids or a["id"] in ids)]
    if not targets:
        return {"ok": False, "msg": "没有可运行的账号"}

    STOP_EVENT.clear()
    SEM = threading.Semaphore(max(1, int(SETTINGS["concurrency"])))
    cfg = base_cfg()

    for a in targets:
        with STATE_LOCK:
            STATE[a["id"]] = {"state": "queued", "done": 0, "total": 0, "message": "排队中"}
        BUS.push({"type": "status", "id": a["id"], "state": "queued"})
        t = threading.Thread(target=_worker, args=(a, cfg), daemon=True, name="acc-%s" % a["id"])
        RUNNING[a["id"]] = t
        t.start()

    BUS.push({"type": "sys", "text": "启动 %d 个账号，并发 %d" % (len(targets), SETTINGS["concurrency"])})
    return {"ok": True, "count": len(targets)}

def stop_run():
    STOP_EVENT.set()
    BUS.push({"type": "sys", "text": "已发送停止信号"})
    return {"ok": True}

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "njitpj/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)

        if p in ("/", "/index.html"):
            return self._static("web/index.html", "text/html; charset=utf-8")
        if p.startswith("/web/") or p.startswith("/assets/"):
            rel = p.lstrip("/")
            if rel.startswith("assets/"):
                rel = "web/" + rel
            ctype = "text/css" if rel.endswith(".css") else (
                "application/javascript" if rel.endswith(".js") else "application/octet-stream")
            return self._static(rel, ctype)

        if p == "/api/accounts":
            return self._send(200, {"accounts": ACCOUNTS})
        if p == "/api/settings":
            return self._send(200, SETTINGS)
        if p == "/api/state":
            with STATE_LOCK:
                return self._send(200, {"state": STATE, "running": bool(RUNNING)})
        if p == "/api/events":
            since = int((q.get("since") or ["0"])[0])
            tab_touch((q.get("tab") or [""])[0])
            return self._sse(since)
        if p == "/api/ping":
            tab_touch((q.get("tab") or [""])[0])
            return self._send(200, {"ok": True})
        if p == "/api/leave":
            tab_leave((q.get("tab") or [""])[0])
            return self._send(200, {"ok": True})
        if p == "/api/quit":
            BUS.push({"type": "sys", "text": "收到退出指令"})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return self._send(200, {"ok": True})
        return self._send(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        body = self._json_body()

        if p == "/api/ping":
            tab_touch((q.get("tab") or [""])[0])
            return self._send(200, {"ok": True})
        if p == "/api/leave":
            tab_leave((q.get("tab") or [""])[0])
            return self._send(200, {"ok": True})

        if p == "/api/accounts":
            name = (body.get("name") or "").strip()
            user = (body.get("username") or "").strip()
            pwd = body.get("password") or ""
            if not user or not pwd:
                return self._send(400, {"ok": False, "msg": "账号和密码都要填"})
            acc = {"id": uuid.uuid4().hex[:10], "name": name or user,
                   "username": user, "password": pwd}
            ACCOUNTS.append(acc)
            save_json(ACCOUNTS_FILE, ACCOUNTS)
            with STATE_LOCK:
                STATE[acc["id"]] = {"state": "idle", "done": 0, "total": 0, "message": ""}
            return self._send(200, {"ok": True, "account": acc})

        if p == "/api/accounts/delete":
            aid = body.get("id")
            ACCOUNTS[:] = [a for a in ACCOUNTS if a["id"] != aid]
            save_json(ACCOUNTS_FILE, ACCOUNTS)
            with STATE_LOCK:
                STATE.pop(aid, None)
            return self._send(200, {"ok": True})

        if p == "/api/settings":
            for k in DEFAULT_SETTINGS:
                if k in body:
                    SETTINGS[k] = body[k]
            save_json(SETTINGS_FILE, SETTINGS)
            return self._send(200, {"ok": True, "settings": SETTINGS})

        if p == "/api/cdp-launch":
            return self._send(200, api_cdp_launch(bool(body.get("system"))))

        if p == "/api/outer-login":
            return self._send(200, api_outer_login(body.get("id") or ""))

        if p == "/api/teachers":
            return self._send(200, api_teachers(body.get("id") or ""))

        if p == "/api/run":
            return self._send(200, start_run(body.get("ids") or None))

        if p == "/api/stop":
            return self._send(200, stop_run())

        return self._send(404, {"ok": False, "msg": "not found"})

    def _static(self, rel, ctype):
        f = res_path(rel)
        if not f.exists():
            return self._send(404, "missing: %s" % rel, "text/plain; charset=utf-8")
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _sse(self, since):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        idx = since
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                evs = BUS.wait_since(idx, timeout=15.0)
                if not evs:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                for ev in evs:
                    idx = ev["seq"]
                    payload = json.dumps(ev, ensure_ascii=False)
                    self.wfile.write(("data: %s\n\n" % payload).encode("utf-8"))
                self.wfile.flush()
        except Exception:
            return

def chromium_ok():
    """完整 chromium 或 headless_shell 有一个能用就算就绪。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            pth = p.chromium.executable_path
            if pth and os.path.exists(pth):
                return True, pth
    except Exception as e:
        pass

    base = pathlib.Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "") or ".")
    if base.exists():
        shells = sorted(base.glob("chromium_headless_shell-*"))
        if shells:
            return True, str(shells[-1])
        chromes = sorted(base.glob("chromium-*"))
        if chromes:
            return True, str(chromes[-1])
    return False, "未找到 Chromium"

def install_chromium():
    """调用 Playwright 自带的驱动下载 Chromium（打包后同样可用）。"""
    try:
        from playwright._impl._driver import compute_driver_executable
        node, cli = compute_driver_executable()
        subprocess.run([str(node), str(cli), "install", "chromium"], check=False)
        return True
    except Exception as e:
        print("安装 Chromium 失败：%s" % e, flush=True)
        return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    ok, info = chromium_ok()
    if not ok:
        print("未检测到 Chromium，开始下载（首次约 150MB，只需一次）...", flush=True)
        install_chromium()
        ok, info = chromium_ok()
        if not ok:
            print("仍然不可用：%s" % info, flush=True)
        else:
            print("Chromium 就绪", flush=True)

    init_state()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 56, flush=True)
    print("评教控制台已启动： %s" % url, flush=True)
    print("数据目录：%s" % DATA, flush=True)
    print("按 Ctrl+C 退出", flush=True)
    print("=" * 56, flush=True)

    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()

    if not args.no_browser:
        threading.Timer(0.8, lambda: open_ui(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()

if __name__ == "__main__":
    main()
