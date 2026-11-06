
"""
单账号执行器：把 pj.py 的流程包成可并发、可中断、可上报进度的对象。
每个实例在自己的线程里跑，Playwright sync API 各自独立。

三种浏览器来源：
  1. CDP 接管：用户手动开好带 --remote-debugging-port 的浏览器，一路点到目标页，程序接管
  2. 内置 headless shell（跑批默认）
  3. 系统 Edge/Chrome（内置缺失时回退）
"""

import json
import pathlib
import re
import threading
import time
import traceback

from playwright.sync_api import sync_playwright

import pj
from captcha import CaptchaSolver
from comments import CommentPool, GcPool
from utils import DATA, PROFILES

ROOT = pathlib.Path(__file__).resolve().parent
PROFILE_ROOT = PROFILES

def enter_jwglxt_webvpn(page, ctx, cfg, solver):
    """校外全自动：门户登录 → appShow → 教务系统。返回 (page, base)。"""
    prefix = pj.webvpn_login(page, cfg, solver)
    if not prefix:
        return page, None

    app_id = cfg.get("ehall_app_id") or "5904538791462728"
    try:
        page.goto(prefix + "/appShow?appId=" + app_id,
                  wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        pj.log("进教务系统失败：%s" % str(e).splitlines()[0][:80])
        return page, None

    for _ in range(12):
        page.wait_for_timeout(2500)
        u = page.url
        if "/jwglxt" in u and "login" not in u:
            base = u.split("/jwglxt")[0]
            pj.log("外链教务系统就绪")
            return page, base
        if "/jwglxt/xtgl/login_slogin" in u:
            try:
                page.locator("button.commonBtn.sso").first.click(force=True, timeout=4000)
            except Exception:
                pass
    pj.log("外链进教务系统超时：%s" % page.url[:80])
    return page, None

class AccountRunner:
    def __init__(self, account, base_cfg, emit, stop_event):
        self.acc = account
        self.cfg = dict(base_cfg)
        self.cfg["username"] = account["username"]
        self.cfg["password"] = account["password"]
        self.cfg["user_data_dir"] = str(PROFILE_ROOT / account["id"])
        self.cfg["headless"] = True
        self.cfg["login_mode"] = "auto"
        self.emit = emit
        self.stop = stop_event
        self.aid = account["id"]
        self.base = self.cfg.get("base", "")
        self.ctx = None
        self.solver = None

    def log(self, line):
        self.emit({"type": "log", "id": self.aid, "text": line})

    def status(self, state, **kw):
        ev = {"type": "status", "id": self.aid, "state": state}
        ev.update(kw)
        self.emit(ev)

    def progress(self, done, total, label=""):
        self.emit({"type": "progress", "id": self.aid, "done": done,
                   "total": total, "label": label})

    def _launch(self, p):
        """内置 headless shell 优先，失败回退系统 Edge/Chrome 无头。"""
        args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage", "--no-sandbox",

            "--disable-features=HttpsFirstModeV2,HttpsUpgrades,HttpsFirstBalancedModeAutoEnable",
            "--unsafely-treat-insecure-origin-as-secure="
            "http://authserver.njit.edu.cn,http://ehall.njit.edu.cn,http://jwjxnew.njit.edu.cn",
        ]

        last = None
        for ch in ("msedge", "chrome"):
            try:
                return p.chromium.launch_persistent_context(
                    user_data_dir=self.cfg["user_data_dir"],
                    headless=True, slow_mo=self.cfg.get("slow_mo", 0),
                    no_viewport=True, channel=ch, args=args,
                )
            except Exception as e:
                last = e
        self.log("系统浏览器起不来，回退内置（校内能用，校外可能卡住）")
        try:
            return p.chromium.launch_persistent_context(
                user_data_dir=self.cfg["user_data_dir"],
                headless=True, slow_mo=self.cfg.get("slow_mo", 0),
                no_viewport=True, args=args,
            )
        except Exception as e:
            raise RuntimeError("没有可用的浏览器：%s / %s" % (str(last)[:60], str(e)[:60]))

    def _attach_cdp(self, p, port):
        """接管用户手动开好的浏览器，直接在他已点开的过程评价页上干活。"""
        try:
            browser = p.chromium.connect_over_cdp("http://127.0.0.1:%d" % port)
        except Exception as e:
            self.log("接管失败：%s" % str(e).splitlines()[0][:80])
            return None, None, None

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for pg in ctx.pages:
            try:
                if "/jwglxt" in pg.url:
                    page = pg
                    break
            except Exception:
                continue
        if page is None:
            page = ctx.pages[-1] if ctx.pages else ctx.new_page()

        try:
            url = page.url
        except Exception:
            url = ""
        if "/jwglxt" not in url:
            self.log("当前页面不在教务系统：%s" % url[:90])
            self.log("请在那个浏览器里一路点到「过程评价」页面，再回来点开始")
            return ctx, None, None

        base = url.split("/jwglxt")[0]
        self.log("已接管浏览器：%s" % base[:80])
        return ctx, page, base

    def run(self):
        pj.set_logger(self.log)
        PROFILE_ROOT.mkdir(exist_ok=True)
        self.status("running", message="")
        self.log("账号 %s 启动" % self.cfg["username"])

        try:
            with sync_playwright() as p:
                cdp_port = int(self.cfg.get("cdp_port") or 0)
                if cdp_port:
                    ctx, page, base = self._attach_cdp(p, cdp_port)
                    if page is None:
                        self.status("failed", message="没有可接管的教务系统页面")
                        return
                    self.ctx = ctx
                    self.base = base
                    self.solver = CaptchaSolver(self.cfg)
                    self._run_pages(page)
                    return

                ctx = self._launch(p)
                self.ctx = ctx
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.set_default_timeout(30000)
                    solver = CaptchaSolver(self.cfg)
                    self.solver = solver

                    link = (self.cfg.get("link_mode") or "auto").lower()
                    self.base = self.cfg["base"]

                    if link == "outer":
                        page = None
                    elif link in ("inner", "auto"):
                        if link == "auto" and not pj.base_reachable(self.base):
                            self.log("校内教务系统连不上（多半在校外），直接走外链")
                            page = None
                        else:
                            page = pj.ensure_login(page, ctx, self.cfg, solver)

                    if page is None and link in ("outer", "auto"):
                        self.log("切换校外链路 ...")
                        if page is None:
                            page = ctx.pages[0] if ctx.pages else ctx.new_page()
                            page.set_default_timeout(30000)
                        page, base2 = enter_jwglxt_webvpn(page, ctx, self.cfg, solver)
                        if base2:
                            self.base = base2

                    if page is None:
                        self.status("failed", message="登录失败")
                        return

                    self._run_pages(page)
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
        except Exception as e:
            self.log("运行异常：%s" % e)
            self.log(traceback.format_exc()[-700:])
            self.status("failed", message=str(e)[:180])
        finally:
            pj.set_logger(None)

    def _probe_done(self, page):
        """快速看两个评价是不是都评完了。学生评价看 tjzt 全 1，过程评价看未评列表是否为空。"""
        st_done = gc_done = False

        try:
            page.goto(self.base + self.cfg["pj_path"],
                      wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
            has_grid = page.locator("#tempGrid").count() > 0
            rows = pj.get_rows(page) if has_grid else []
            if not rows:
                st_done = has_grid
            else:
                st_done = True
                for r in rows:
                    if str(r.get("tjzt")) != "1":
                        st_done = False
                        break
        except Exception:
            pass

        if not self.cfg.get("gc_enabled"):
            gc_done = True
        else:
            try:
                page.goto(self.base + self.cfg.get("gc_pj_path", ""),
                          wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(4000)

                tab = page.evaluate(
                    "() => { const e = document.querySelector('#wpj_yq');"
                    " return e ? (e.innerText || '') : ''; }")
                if tab:
                    gc_done = ("0" in tab.replace(" ", ""))
                else:
                    page.evaluate("() => { const t=document.querySelector('#wpj_yq'); if(t) t.click(); }")
                    page.wait_for_timeout(2500)
                    gc_done = len(page.evaluate(pj.GC_LIST_JS)) == 0
            except Exception:
                pass

        return st_done, gc_done

    def _save_history(self, st_done, gc_done, ok_n, skip_n, fail_n):
        try:
            (DATA / ("history_%s.json" % self.aid)).write_text(json.dumps({
                "student_done": st_done, "gc_done": gc_done,
                "ok": ok_n, "skip": skip_n, "fail": fail_n,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def _run_pages(self, page):
        ok_n = fail_n = skip_n = dry_n = 0

        self.log("检查完成状态 ...")
        st_done, gc_done = self._probe_done(page)
        self.log("  学生评价：%s" % ("已完成" if st_done else "待评"))
        self.log("  过程评价：%s" % ("已完成" if gc_done else "待评"))

        if st_done and gc_done:
            self.log("两项都已全部评完，本次跳过")
            self._save_history(True, True, 0, 0, 0)
            self.status("done", done=0, total=0, message="已全部完成，跳过")
            return

        pages = []
        if not st_done:
            pages.append(("学生评价", self.cfg["pj_path"]))
        if not gc_done and self.cfg.get("gc_enabled"):
            pages.append(("过程评价", self.cfg.get("gc_pj_path", "")))

        for label, path in pages:
            if self.stop.is_set():
                break
            if label == "过程评价":
                s = self.run_gc_page(page, path, label)
            else:
                s = self.run_page(page, path, label)

            if (s is None and (self.cfg.get("link_mode") or "auto") == "auto"
                    and "casb.njit.edu.cn" not in self.base
                    and not self.cfg.get("cdp_port")):
                self.log("%s 在校内链路打不开，切外链重试 ..." % label)
                try:
                    page2, base2 = enter_jwglxt_webvpn(page, self.ctx, self.cfg, self.solver)
                except Exception as e:
                    page2, base2 = None, None
                    self.log("外链异常：%s" % str(e)[:80])
                if base2:
                    self.base = base2
                    if label == "过程评价":
                        s = self.run_gc_page(page2, path, label, retry_outer=False)
                    else:
                        s = self.run_page(page2, path, label)

            if s:
                ok_n += s["ok"]
                skip_n += s["skip"]
                fail_n += s["fail"]
                dry_n += s["dry"]

        try:
            st2, gc2 = self._probe_done(page)
            self._save_history(st2, gc2, ok_n, skip_n, fail_n)
        except Exception:
            pass

        if self.stop.is_set():
            self.status("stopped", done=ok_n + dry_n, total=ok_n + dry_n + skip_n + fail_n)
        else:
            if self.cfg.get("dry_run"):
                msg = "试跑 %d / 跳过 %d / 失败 %d" % (dry_n, skip_n, fail_n)
            else:
                msg = "成功 %d / 跳过 %d / 失败 %d" % (ok_n, skip_n, fail_n)
            self.status("done", done=ok_n + dry_n,
                        total=ok_n + dry_n + skip_n + fail_n, message=msg)

    def run_page(self, page, path, label):
        self.log("======== %s ========" % label)
        try:
            page.goto(self.base + path, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            self.log("%s 打开失败：%s" % (label, str(e).splitlines()[0].lstrip(": ")))
            return None
        page.wait_for_timeout(3000)

        try:
            title = page.title()
            body = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            title, body = "", ""
        if "系统维护" in title or "系统升级维护中" in body:
            self.log("%s 当前不可用：页面提示「系统升级维护中」" % label)
            self.emit({"type": "sys", "text": "%s 当前未开放（系统维护中）" % label})
            return None
        if "login_slogin" in page.url:
            self.log("%s 会话失效" % label)
            return None

        rows = pj.get_rows(page)
        total = len(rows)
        self._save_teachers(rows)
        self.log("%s 列表共 %d 条" % (label, total))
        if not rows:
            self.log("%s 没有取到教学班列表" % label)
            return {"ok": 0, "skip": 0, "fail": 0, "dry": 0}

        ok_n = fail_n = skip_n = dry_n = 0
        self.progress(0, total, label)
        for i, r in enumerate(rows):
            if self.stop.is_set():
                self.log("收到停止信号，中断")
                break
            lab = pj.row_label(r)
            if self.cfg.get("skip_submitted") and str(r.get("tjzt")) == "1":
                skip_n += 1
                self.log("已提交，跳过：%s" % lab)
            else:
                self.log("—— %s（tjzt=%s）" % (lab, r.get("tjzt")))
                try:
                    res = pj.eval_one(page, r, self.cfg, self.pool,
                                      dry=bool(self.cfg.get("dry_run")))
                except Exception as e:
                    self.log("异常：%s" % e)
                    res = False
                if res is True:
                    ok_n += 1
                elif res == "skip":
                    skip_n += 1
                elif res == "dry":
                    dry_n += 1
                else:
                    fail_n += 1
            self.progress(i + 1, total, label)
            pj.human_pause(self.cfg)

        if self.cfg.get("dry_run"):
            self.log("%s 试跑完成：%d 条已填充未保存，跳过 %d，失败 %d" % (label, dry_n, skip_n, fail_n))
        else:
            self.log("%s 保存完成：%d 条，跳过 %d，失败 %d" % (label, ok_n, skip_n, fail_n))

        if self.cfg.get("auto_submit") and ok_n > 0 and not self.stop.is_set():
            self.progress(total, total, label)
            pj.submit_all(page, self.cfg)

        return {"ok": ok_n, "skip": skip_n, "fail": fail_n, "dry": dry_n}

    def run_gc_page(self, page, path, label, retry_outer=True):
        """左列表逐个点开 → 肯定/改进/总评/评语 → 提交。"""
        self.log("======== %s ========" % label)
        try:
            page.goto(self.base + path, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            self.log("%s 打开失败：%s" % (label, str(e).splitlines()[0].lstrip(": ")))
            return None
        page.wait_for_timeout(3000)

        try:
            title = page.title()
            body = page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            title, body = "", ""
        if "系统维护" in title or "系统升级维护中" in body:
            self.log("%s 校内不可用：页面提示「系统升级维护中」" % label)
            if (retry_outer and self.cfg.get("gc_outer_retry")
                    and (self.cfg.get("link_mode") or "auto") == "auto"
                    and "casb.njit.edu.cn" not in self.base):
                self.log("改走校外链路再试 ...")
                try:
                    page2, base2 = enter_jwglxt_webvpn(page, self.ctx, self.cfg, self.solver)
                except Exception as e:
                    page2, base2 = None, None
                    self.log("外链异常：%s" % str(e)[:80])
                if base2:
                    old = self.base
                    self.base = base2
                    try:
                        return self.run_gc_page(page2, path, label + "(校外)", retry_outer=False)
                    finally:
                        self.base = old
            self.emit({"type": "sys", "text": "%s 当前未开放（系统维护中）" % label})
            return None
        if "login_slogin" in page.url:
            self.log("%s 会话失效" % label)
            return None

        try:
            page.evaluate("() => { const t = document.querySelector('#wpj_yq'); if (t) t.click(); }")
            page.wait_for_timeout(2000)
        except Exception:
            pass

        ok_n = fail_n = dry_n = 0
        done = set()
        for rnd in range(50):
            if self.stop.is_set():
                break
            try:
                items = page.evaluate(pj.GC_LIST_JS)
            except Exception as e:
                self.log("列表提取失败：%s" % e)
                break
            pending = [it for it in items if it["teacher"] not in done]
            if not pending:
                if rnd == 0:
                    self.log("%s 没提取到课程卡片（未评列表空或选择器需校准）" % label)
                break
            it = pending[0]
            teacher = it["teacher"]
            self.log("—— %s | %s" % (teacher, it["text"][:44]))

            hit = page.evaluate(pj.GC_PICK_JS, {"teacher": teacher, "i": it.get("i")})
            if not hit:
                self.log("  点击卡片失败，跳过")
                done.add(teacher)
                fail_n += 1
                continue
            page.wait_for_timeout(2800)

            grade = self.grade_for_teacher(teacher)
            overall = pj.gc_overall_for(grade, self.cfg)
            affirm = self.gc_pool.next_affirm()
            improve = self.gc_pool.next_improve()
            comment = self.pool.next()
            self.log("  等级 %s → 总评「%s」" % (grade or "默认", overall))
            self.log("  肯定：%s" % affirm[:26])
            self.log("  改进：%s" % improve[:26])
            self.log("  评语：%s" % comment[:26])

            res = page.evaluate(pj.GC_FILL_JS, {
                "affirm": affirm, "improve": improve,
                "comment": comment, "overall": overall,
            })
            self.log("  填写：textarea %d 个，总评候选 %d 个，选中 %s"
                     % (res.get("tas", -1), res.get("overallCount", -1),
                        res.get("overall") or "未命中"))
            if res.get("overall") is None:
                self.log("  !! 总评单选没命中「%s」" % overall)

            if self.cfg.get("dry_run"):
                self.log("  [dry] 不提交")
                dry_n += 1
                done.add(teacher)
                continue

            btn = page.evaluate(pj.GC_SUBMIT_JS)
            if not btn:
                self.log("  !! 没找到提交按钮")
                done.add(teacher)
                fail_n += 1
                continue
            page.wait_for_timeout(1300)
            pj.dismiss_dialog(page, 1800)
            toast = pj.get_toast(page)
            if toast:
                self.log("  返回：%s" % toast)
            pj.dismiss_dialog(page, 1000)
            page.wait_for_timeout(1200)
            ok_n += 1
            self.log("  已提交：%s" % teacher)
            done.add(teacher)
            pj.human_pause(self.cfg)

        self.gc_pool.flush()
        self.log("%s 完成：提交 %d，试跑 %d，失败 %d" % (label, ok_n, dry_n, fail_n))
        return {"ok": ok_n, "skip": 0, "fail": fail_n, "dry": dry_n}

    def _save_teachers(self, rows):
        try:
            out, seen = [], set()
            for r in rows:
                t = (r.get("jzgmc") or "").strip()
                if not t or t in seen:
                    continue
                seen.add(t)
                out.append({"teacher": t, "course": r.get("kcmc", ""),
                            "jxb": r.get("jxbmc", "")})
            (DATA / ("teachers_%s.json" % self.aid)).write_text(
                json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def grade_for_teacher(self, teacher):
        return pj.resolve_grade({"jzgmc": teacher}, self.cfg)

    _gc_pool = None

    @property
    def gc_pool(self):
        if AccountRunner._gc_pool is None:
            AccountRunner._gc_pool = GcPool(store_path=DATA / "gc_used.json")
        return AccountRunner._gc_pool

    @property
    def pool(self):
        return CommentPool.shared(store_path=DATA / "comments_used.json",
                                  seed=self.cfg.get("random_seed", 20261105))

def run_in_thread(account, base_cfg, emit, stop_event):
    AccountRunner(account, base_cfg, emit, stop_event).run()
