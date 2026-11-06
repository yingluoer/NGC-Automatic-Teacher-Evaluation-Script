
"""
正方教务 学生评教 批量自动化（NJIT）
=================================================
登录链：authserver 统一身份认证（字符验证码） -> ehall 信息门户 -> 教务系统
评教链：xspj_cxXspjIndex.html（左列表 + 右问卷）
        逐条 setSelection 加载问卷 -> 每题选「非常满意」
        -> 指定题选「比较满意」-> 填随机评语 -> 保存（不提交）
        全部保存完毕 -> 点一次提交（服务端返回「整体提交成功」）

用法：
  python pj.py                 # 全流程
  python pj.py --dry           # 只填不保存
  python pj.py --limit 2       # 只处理前 2 条
  python pj.py --no-submit     # 保存完不提交
  python pj.py --inspect       # 导出列表与问卷结构
"""

import argparse
import json
import pathlib
import random
import re
import sys
import threading
import time
import urllib.parse

from playwright.sync_api import sync_playwright

from captcha import CaptchaSolver
from comments import CommentPool
from utils import APP, DATA, DUMP

ROOT = APP
LOG_FILE = DATA / "run.log"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

_local = threading.local()

def set_logger(fn):
    """给当前线程装一个日志回调；传 None 恢复默认的 stdout + run.log。"""
    _local.hook = fn

def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    hook = getattr(_local, "hook", None)
    if hook is not None:
        try:
            hook(line)
            return
        except Exception:
            pass
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def base_reachable(base, timeout=6):
    """探测教务系统能不能连上。校外网络访问不了内网域名，会直接超时。"""
    try:
        import requests
        requests.get(base, timeout=timeout, allow_redirects=False)
        return True
    except Exception:
        return False

def load_cfg(path):
    p = pathlib.Path(path)
    if not p.exists():
        from utils import config_file
        p = config_file()
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)

def human_pause(cfg):
    time.sleep(random.uniform(cfg["delay_min"], cfg["delay_max"]) / 1000.0)

def build_auth_url(cfg):
    ehall = cfg["ehall_index"]
    service = cfg["ehall_login"] + "?service=" + urllib.parse.quote(ehall, safe="")
    return cfg["auth_base"].rstrip("/") + "/authserver/login?service=" + urllib.parse.quote(service, safe="")

def _fill_cas_form(page, cfg, solver, attempt, tries):
    """往当前 authserver 登录表单里填账号密码验证码并提交，返回是否通过。"""
    user = cfg.get("username") or ""
    pwd = cfg.get("password") or ""

    log("认证尝试 %d/%d" % (attempt, tries))
    page.fill("#username", user)
    page.locator("#password:visible").first.fill(pwd)

    code = ""
    img = page.locator("#captchaImg:visible")
    if img.count() > 0:
        shot = DUMP / "cas_captcha.png"
        DUMP.mkdir(parents=True, exist_ok=True)
        img.first.screenshot(path=str(shot))
        code = solver.solve(shot.read_bytes())
        log("  验证码：%s" % (code or "识别失败"))
        box = page.locator("#captchaResponse:visible")
        if box.count() > 0 and code:
            box.first.fill(code)

    try:
        page.click("button.auth_login_btn", timeout=8000)
    except Exception:
        page.keyboard.press("Enter")

    page.wait_for_timeout(4500)
    if "#username" not in page.content() and "authserver/login" not in page.url.lower():
        log("认证通过：%s" % page.url[:80])
        return True
    return False

def sso_fill_current(page, cfg, solver, tries=None):
    """当前页面已经是 authserver 登录表单（可能是 WebVPN 域的），直接填，不 goto。
    给 enlink 的 SSO 链路用。"""
    tries = int(tries or cfg.get("captcha_retry", 6))
    DUMP.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, tries + 1):
        if page.locator("#username").count() == 0:
            log("认证通过：%s" % page.url[:80])
            return True
        if _fill_cas_form(page, cfg, solver, attempt, tries):
            return True
        try:
            page.evaluate("() => { const e = document.querySelector('#captchaImg'); if (e) e.click(); }")
        except Exception:
            pass
        page.wait_for_timeout(900)
    return False

def cas_login(page, cfg, solver):
    """在 authserver 页面填账号密码 + 验证码，通过返回 True。"""
    url = build_auth_url(cfg)
    log("打开统一身份认证 ...")
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2200)

    tries = int(cfg.get("captcha_retry", 6))
    DUMP.mkdir(parents=True, exist_ok=True)

    for attempt in range(1, tries + 1):
        if "authserver" not in page.url.lower():
            return True
        if page.locator("#username").count() == 0:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)

        if _fill_cas_form(page, cfg, solver, attempt, tries):
            return True

        tip = ""
        try:
            tip = page.evaluate(
                "() => { const e = document.querySelector('#msg, .auth_error, #errorMsg');"
                " return e ? (e.innerText || '').trim() : ''; }"
            )
        except Exception:
            pass
        if tip:
            log("  提示：%s" % tip)
        log("  未通过，换一张验证码重试")
        try:
            page.evaluate("() => { const e = document.querySelector('#captchaImg'); if (e) e.click(); }")
        except Exception:
            pass
        page.wait_for_timeout(900)
    return False

def enter_jwglxt(page, cfg):
    """从 ehall 应用入口跳进教务系统。多试几轮，学校这跳转偶尔很慢。"""
    app_url = cfg["ehall_app"]
    log("从信息门户进入教务系统 ...")

    for attempt in range(3):
        try:
            page.goto(app_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass

        for _ in range(10):
            page.wait_for_timeout(1500)
            for pg in page.context.pages:
                try:
                    if "jwglxt" in pg.url:
                        log("已进入教务系统")
                        return pg
                except Exception:
                    continue
            if "jwglxt" in page.url:
                log("已进入教务系统")
                return page
        log("  第 %d 次进教务系统没成功，重试" % (attempt + 1))

    try:
        page.goto(cfg["base"] + cfg["index_path"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
    except Exception:
        pass
    if "jwglxt" in page.url and "login" not in page.url:
        return page
    return None

def ensure_login(page, ctx, cfg, solver):

    try:
        page.goto(cfg["base"] + cfg["pj_path"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(2500)
        if "login_slogin" not in page.url and page.locator("#tempGrid").count() > 0:
            log("已有有效会话")
            return page
    except Exception:
        pass

    if cfg.get("login_mode") == "manual":
        log("=" * 56)
        log("手动模式：请在浏览器里登录信息门户并点开「教务系统新」")
        log("程序会自动检测，登录完成后继续")
        log("=" * 56)
        work = page
    else:
        if not cas_login(page, cfg, solver):
            log("统一认证未通过")
            return None
        work = enter_jwglxt(page, cfg)
        if work is None:
            log("未能进入教务系统")
            return None

    for _ in range(60):
        try:
            work.goto(cfg["base"] + cfg["pj_path"], wait_until="domcontentloaded", timeout=45000)
            work.wait_for_timeout(2200)
            if work.locator("#tempGrid").count() > 0 and "login_slogin" not in work.url:
                log("已进入学生评价页")
                return work
        except Exception:
            pass
        time.sleep(2)
    return None

ROWS_JS = r"""
(ids) => ids.map(function (id) {
  const r = jQuery('#tempGrid').getRowData(id);
  r._id = id;
  return r;
})
"""

def get_rows(page, expand=True):

    if expand:
        try:
            need = page.evaluate(
                """() => {
                    const g = jQuery('#tempGrid');
                    const rec = parseInt(g.getGridParam('records') || 0, 10);
                    const num = parseInt(g.getGridParam('rowNum') || 0, 10);
                    if (rec > num) {
                        g.setGridParam({rowNum: Math.max(200, rec)});
                        g.trigger('reloadGrid');
                        return rec;
                    }
                    return 0;
                }"""
            )
            if need:
                log("列表共 %d 条（分页），已全部拉取" % need)
                page.wait_for_timeout(2500)
        except Exception:
            pass

    try:
        ids = page.evaluate("() => jQuery('#tempGrid').getDataIDs()")
    except Exception:
        return []
    if not ids:
        return []
    try:
        return page.evaluate(ROWS_JS, ids)
    except Exception:
        return []

def row_label(r):
    return "%s / %s / %s" % (r.get("jzgmc", ""), r.get("kcmc", ""), r.get("jxbmc", ""))

FORM_INFO_JS = r"""
() => {
  // 注意：本方页面重写过 Array.from，这里全部用原生 for 循环，别用 Array.from/map/filter
  const trs = document.querySelectorAll('#panel_content tr.tr-xspj');
  const out = [];
  for (let i = 0; i < trs.length; i++) {
    const tr = trs[i];
    const td = tr.querySelector('td');
    const q = td ? (td.innerText || '').replace(/\s+/g, ' ').trim() : '';
    const cells = tr.querySelectorAll('div.input-xspj');
    const opts = [];
    for (let j = 0; j < cells.length; j++) {
      const c = cells[j];
      const s = c.querySelector('span');
      const inp = c.querySelector('input.radio-pjf');
      const txt = s ? (s.innerText || s.textContent || '').replace(/\s+/g, '') : '';
      if (!txt) continue;
      opts.push({text: txt, val: inp ? inp.getAttribute('data-dyf') : null});
    }
    out.push({i: i, q: q, opts: opts});
  }
  return out;
}
"""

FILL_JS = r"""
(args) => {
  const trs = document.querySelectorAll('#panel_content tr.tr-xspj');
  const n = trs.length;
  const res = {total: n, filled: 0, special: [], fail: [], autoSpecial: 0, grade: args.normal};
  const wants = new Array(n);

  for (let i = 0; i < n; i++) {
    if (args.gradeMode) {
      // 等级模式：最后一题固定不动，其余按等级走
      wants[i] = (i === n - 1) ? args.lastFixed : args.normal;
    } else {
      const td = trs[i].querySelector('td');
      const q = td ? (td.innerText || '').replace(/\s+/g, ' ').trim() : '';
      let isSpecial = false;
      for (let k = 0; k < args.kw.length; k++) {
        if (q.indexOf(args.kw[k]) >= 0) { isSpecial = true; break; }
      }
      if (args.spIndex && (i + 1) === args.spIndex) isSpecial = true;
      if (isSpecial) res.special.push(i + 1);
      wants[i] = isSpecial ? args.special : args.normal;
    }
  }

  // 全同会撞「同一评分类型不可全部选择同一评分项」。
  // 等级模式下最后一题要保住，就降倒数第二题；默认模式仍降最后一题。
  let allSame = true;
  for (let i = 1; i < n; i++) {
    if (wants[i] !== wants[0]) { allSame = false; break; }
  }
  if (allSame && n > 1 && args.autoBreak) {
    const idx = args.gradeMode ? (n - 2) : (n - 1);
    if (idx >= 0) {
      wants[idx] = args.special;
      res.autoSpecial = idx + 1;
      if (res.special.indexOf(idx + 1) < 0) res.special.push(idx + 1);
    }
  }

  for (let i = 0; i < n; i++) {
    const tr = trs[i];
    const cells = tr.querySelectorAll('div.input-xspj');
    let hit = null;
    let fallback = null;
    for (let j = 0; j < cells.length; j++) {
      const c = cells[j];
      const s = c.querySelector('span');
      const txt = s ? (s.innerText || s.textContent || '').replace(/\s+/g, '') : '';
      if (!fallback && c.querySelector('input.radio-pjf')) fallback = c;
      if (txt === wants[i]) { hit = c; break; }
    }
    if (!hit) hit = fallback;
    if (!hit) { res.fail.push(i + 1); continue; }

    const inp = hit.querySelector('input.radio-pjf');
    if (inp) {
      inp.click();
      res.filled++;
    } else {
      const lb = hit.querySelector('label');
      if (lb) { lb.click(); res.filled++; } else res.fail.push(i + 1);
    }
  }
  res.wants = wants;
  return res;
}
"""

GC_LIST_JS = r"""
() => {
  // WebVPN 注入了 bundle.debug.js，会改写 Array.prototype.filter/forEach，
  // 这里全部用原生 for 循环，别碰数组高阶方法。
  const out = [];
  const items = document.querySelectorAll('li.mui-table-view-cell');
  for (let i = 0; i < items.length; i++) {
    const li = items[i];
    const t = (li.innerText || '').replace(/\s+/g, ' ').trim();
    if (!t) continue;
    if (t.indexOf('学时') < 0) continue;      // 只要课程项
    if (t.indexOf('？') >= 0 || t.indexOf('?') >= 0) continue;  // 排除题目
    if (li.getBoundingClientRect().width < 50) continue;
    // 文本可能是「理论学时大学英语AⅠ大学英语AⅠ-T56F1202蒋莉1」，末尾抠中文姓名
    let teacher = '';
    const m = t.match(/([\u4e00-\u9fa5]{2,5})\s*\d*$/);
    if (m) teacher = m[1];
    out.push({i: i, teacher: teacher, text: t.slice(0, 70)});
  }
  return out;
}
"""

GC_PICK_JS = r"""
(args) => {
  const items = document.querySelectorAll('li.mui-table-view-cell');
  if (args.teacher) {
    for (let i = 0; i < items.length; i++) {
      const t = (items[i].innerText || '').replace(/\s+/g, ' ');
      if (t.indexOf(args.teacher) >= 0) { items[i].click(); return true; }
    }
    return false;
  }
  if (args.i != null && args.i < items.length) { items[args.i].click(); return true; }
  return false;
}
"""

GC_FILL_JS = r"""
(args) => {
  function fire(el) {
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    try { jQuery(el).trigger('input').trigger('change'); } catch (e) {}
  }
  const res = {tas: 0, py: false, overall: null, overallCount: 0};

  // 1、2 题：两个 textarea[name=zgpj]
  const all = document.querySelectorAll('textarea[name="zgpj"]');
  const tas = [];
  for (let i = 0; i < all.length; i++) {
    const t = all[i];
    if (t.getBoundingClientRect().width > 30) tas.push(t);
  }
  res.tas = tas.length;
  if (tas[0]) { tas[0].focus(); tas[0].value = args.affirm; fire(tas[0]); }
  if (tas[1]) { tas[1].focus(); tas[1].value = args.improve; fire(tas[1]); }

  // 评语 #py
  const py = document.querySelector('#py');
  if (py) { py.focus(); py.value = args.comment; fire(py); res.py = true; }

  // 总评：div.block 里的 <p>优秀</p>，点它的 .cs
  const blocks = document.querySelectorAll('div.block');
  const cand = [];
  for (let i = 0; i < blocks.length; i++) {
    const p = blocks[i].querySelector('p');
    if (p && /优秀|良好|中等|及格|差/.test(p.innerText || '')) cand.push(blocks[i]);
  }
  res.overallCount = cand.length;
  for (let i = 0; i < cand.length; i++) {
    const p = cand[i].querySelector('p');
    const txt = (p.innerText || '').replace(/\s+/g, '');
    if (txt === args.overall) {
      const cs = cand[i].querySelector('.cs');
      (cs || p).click();
      res.overall = args.overall;
      break;
    }
  }
  return res;
}
"""

GC_SUBMIT_JS = r"""
() => {
  const cands = document.querySelectorAll('button, a, div, input[type=button]');
  for (let i = 0; i < cands.length; i++) {
    const e = cands[i];
    const t = (e.innerText || e.value || '').replace(/\s+/g, '');
    if (t === '提交' || t === '提交评价') {
      const r = e.getBoundingClientRect();
      if (r.width > 40 && r.height > 18) { e.click(); return true; }
    }
  }
  return false;
}
"""

def gc_overall_for(grade, cfg):
    """等级 → 过程评价总评文字。A=优秀 B=良好 C=中等 D=及格 E=差。"""
    g = (grade or cfg.get("grade_default") or "A").upper()
    if g == "A":
        return "优秀"
    if g == "B":
        return "良好"
    if g == "C":
        return "中等"
    if g == "D":
        return "及格"
    return "差"

def webvpn_login(page, cfg, solver):
    """校外全自动：ehallsdp 门户 → 点登录 → CAS 填表 → 回门户。
    返回 WebVPN 版 ehall 前缀；失败返回 None。"""
    outer = (cfg.get("outer_url") or "https://ehallsdp.njit.edu.cn").rstrip("/")
    try:
        page.goto(outer, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        log("外链门户打不开：%s" % str(e).splitlines()[0][:80])
        return None

    for _ in range(20):
        page.wait_for_timeout(1000)
        try:
            if page.evaluate("() => !!document.querySelector('#ampLoginBtn')"):
                break
        except Exception:
            pass

    try:
        page.evaluate("() => { const b=document.querySelector('#ampLoginBtn'); if(b) b.click(); }")
    except Exception:
        pass
    page.wait_for_timeout(8000)

    if "authserver" in page.url and page.locator("#username").count() > 0:
        log("外链统一认证 ...")
        if not sso_fill_current(page, cfg, solver):
            log("外链统一认证未通过")
            return None
        page.wait_for_timeout(9000)

    if "/new/" not in page.url:
        try:
            page.goto(outer, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(7000)
        except Exception:
            pass

    m = re.search(r"(https://casb\.njit\.edu\.cn/http/webvpn[0-9a-f]+)", page.url)
    if not m:
        log("没拿到 WebVPN 门户前缀：%s" % page.url[:80])
        return None
    log("外链门户就绪")
    return m.group(1)

COMMENT_JS = r"""
(text) => {
  const ta = document.querySelector('#panel_content textarea[name="py"]');
  if (!ta) return false;
  ta.focus();
  ta.value = text;
  ta.dispatchEvent(new Event('input', {bubbles: true}));
  ta.dispatchEvent(new Event('change', {bubbles: true}));
  try { jQuery(ta).trigger('input').trigger('keyup').trigger('change'); } catch (e) {}
  return true;
}
"""

def wait_form(page, timeout_ms=25000):
    """等右侧问卷加载完（loading 消失 + 出现题目行）。"""
    end = time.time() + timeout_ms / 1000.0
    while time.time() < end:
        st = page.evaluate(
            """() => {
                const load = document.querySelector('#panel_content #loading_status');
                const rows = document.querySelectorAll('#panel_content tr.tr-xspj');
                const alert = document.querySelector('#panel_content .alert-error');
                return {loading: !!load, rows: rows.length,
                        alert: alert ? (alert.innerText || '').trim().slice(0,40) : null};
            }"""
        )
        if st.get("alert") and st["rows"] == 0:
            return False, st["alert"]
        if not st["loading"] and st["rows"] > 0:
            return True, ""
        page.wait_for_timeout(400)
    return False, "加载超时"

def click_btn(page, sel):
    """正方装了个「必须真实鼠标移入才让点」的防脚本校验（#xspjjbzrkz=1），
    所以先 hover 再点。"""
    try:
        loc = page.locator(sel).first
        loc.hover(timeout=4000)
        page.wait_for_timeout(200)
        loc.click(timeout=8000)
        return True
    except Exception:
        pass
    try:
        page.evaluate(
            """(s) => {
                const e = document.querySelector(s);
                if (!e) return;
                const r = e.getBoundingClientRect();
                const x = r.left + r.width / 2, y = r.top + r.height / 2;
                ['mouseover', 'mouseenter', 'mousemove', 'mousedown', 'mouseup', 'click'].forEach(function (t) {
                    e.dispatchEvent(new MouseEvent(t, {bubbles: true, clientX: x, clientY: y}));
                });
                try { jQuery(e).data('enter', '1'); } catch (err) {}
                e.click();
            }""",
            sel,
        )
        return True
    except Exception:
        return False

def dismiss_dialog(page, wait=1500):
    """处理正方的 $.alert / $.confirm / layer 弹层。"""
    page.wait_for_timeout(wait)
    js = r"""
    () => {
      const sels = ['.layui-layer-btn0', '.bootbox .btn-primary',
                    '.modal-footer .btn-primary', '.sweet-alert button.confirm',
                    '.el-message-box__btns .el-button--primary'];
      for (const s of sels) {
        const e = document.querySelector(s);
        if (!e) continue;
        const r = e.getBoundingClientRect();
        if (r.width > 4 && r.height > 4) { e.setAttribute('data-pjok', '1'); return (e.innerText||'').trim(); }
      }
      const boxes = Array.from(document.querySelectorAll('.bootbox, .modal, .sweet-alert, .layui-layer'));
      for (const b of boxes) {
        const r0 = b.getBoundingClientRect();
        if (r0.width < 8 || r0.height < 8) continue;
        for (const e of Array.from(b.querySelectorAll('button, a, span'))) {
          const t = (e.innerText || '').trim();
          if (t !== '确定' && t !== '确认' && t !== '是' && t !== 'OK') continue;
          const r = e.getBoundingClientRect();
          if (r.width < 8 || r.height < 8) continue;
          e.setAttribute('data-pjok', '1');
          return t;
        }
      }
      return null;
    }
    """
    try:
        got = page.evaluate(js)
    except Exception:
        return ""
    if got:
        try:
            page.click('[data-pjok="1"]', force=True, timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(700)
        return got
    return ""

def get_toast(page):
    try:
        return page.evaluate(
            """() => {
                const sels = ['.bootbox-body', '.layui-layer-content', '.sweet-alert p',
                              '.modal-body', '.alert-success'];
                for (const s of sels) {
                    const e = document.querySelector(s);
                    if (!e) continue;
                    const r = e.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) continue;
                    return (e.innerText || '').trim().slice(0, 120);
                }
                return '';
            }"""
        )
    except Exception:
        return ""

GRADE_OPTIONS = {
    "A": "非常满意",
    "B": "比较满意",
    "C": "满意",
    "D": "一般",
    "E": "不满意",
}

def resolve_grade(row, cfg):
    """按教师名 / 课程名 / 教学班名匹配等级；匹配不到就用默认等级。没开等级模式返回 None。"""
    if not cfg.get("grade_mode"):
        return None
    gmap = cfg.get("grade_map_parsed") or {}
    for key in (row.get("jzgmc", ""), row.get("kcmc", ""), row.get("jxbmc", "")):
        if key and key in gmap:
            return gmap[key]
    return (cfg.get("grade_default") or "A").upper()

def eval_one(page, row, cfg, pool, dry=False):
    label = row_label(row)
    tjzt = str(row.get("tjzt", ""))
    if str(row.get("fqpjzt", "")) == "1":
        log("  已放弃评价，跳过：%s" % label)
        return "skip"

    if dry and tjzt == "1":
        log("  已提交（只读），试跑跳过：%s" % label)
        return "skip"

    page.evaluate("(rid) => jQuery('#tempGrid').setSelection(rid, true)", row["_id"])
    ok, msg = wait_form(page, int(cfg.get("form_wait_ms", 25000)))
    if not ok:
        log("  问卷加载失败：%s（%s）" % (msg or "无题目", label))
        return False

    info = page.evaluate(FORM_INFO_JS)
    if not info:
        log("  未解析到题目：%s" % label)
        return False
    state_txt = {"-1": "未评", "0": "已保存", "1": "已提交"}.get(tjzt, tjzt or "未知")
    log("  题目 %d 道（原状态 %s）" % (len(info), state_txt))

    grade = resolve_grade(row, cfg)
    grades = cfg.get("grades") or GRADE_OPTIONS
    if grade:
        opt = grades.get(grade) or cfg["default_option"]
        log("  等级 %s → %s" % (grade, opt))
        fill_args = {
            "kw": cfg["special_keywords"],
            "normal": opt,
            "special": cfg["special_option"],
            "spIndex": 0,
            "autoBreak": bool(cfg.get("auto_break_consistency", True)),
            "gradeMode": True,
            "lastFixed": cfg.get("last_question_option", "非常满意"),
        }
    else:
        fill_args = {
            "kw": cfg["special_keywords"],
            "normal": cfg["default_option"],
            "special": cfg["special_option"],
            "spIndex": int(cfg.get("special_question_index") or 0),
            "autoBreak": bool(cfg.get("auto_break_consistency", True)),
            "gradeMode": False,
            "lastFixed": cfg["default_option"],
        }

    res = page.evaluate(FILL_JS, fill_args)
    log("  已选 %d/%d，特殊题在第 %s 题"
        % (res.get("filled", 0), res.get("total", 0), res.get("special") or "未命中"))
    for i in res.get("special") or []:
        if 1 <= i <= len(info):
            log("    第%d题：%s" % (i, info[i - 1]["q"][:46]))
    if res.get("autoSpecial"):
        log("    第 %d 题降为「%s」，避开统一评分校验"
            % (res["autoSpecial"], cfg["special_option"]))
    if not res.get("special") and not grade:
        log("    !! 没有命中特殊题，检查 special_keywords")
    if res.get("fail"):
        log("    !! 未填充的题：%s" % res["fail"])

    if cfg.get("comment_enabled"):
        comment = pool.next()
        limit = int(cfg.get("max_comment_len", 200))
        if len(comment) > limit:
            comment = comment[: limit - 1] + "。"
        if page.evaluate(COMMENT_JS, comment):
            log("  评语：%s" % comment[:26])
        else:
            log("  页面上没有评语框")

    if dry:
        log("  [dry] 不保存")
        return "dry"

    if not click_btn(page, "#btn_xspj_bc"):
        log("  点击保存失败")
        return False
    page.wait_for_timeout(1200)
    dismiss_dialog(page, 1500)
    toast = get_toast(page)
    if toast:
        log("  返回：%s" % toast)
    dismiss_dialog(page, 800)

    page.wait_for_timeout(800)
    try:
        st = page.evaluate("(rid) => jQuery('#tempGrid').getRowData(rid).tjzt", row["_id"])
    except Exception:
        st = ""
    if str(st) == "0":
        log("  已保存：%s" % label)
        return True
    log("  !! 保存后状态仍为 %s，未生效" % st)
    return False

def submit_all(page, cfg):
    """正方这里是按教学班单条提交，不是批量。逐条点一遍。"""
    log("开始逐条提交 ...")
    tried = set()
    ok_n = 0
    for _ in range(120):
        rows = get_rows(page)
        pending = [r for r in rows
                   if str(r.get("tjzt")) == "0" and r["_id"] not in tried]
        if not pending:
            break
        r = pending[0]
        tried.add(r["_id"])
        log("  提交：%s" % row_label(r))

        try:
            page.evaluate("(rid) => jQuery('#tempGrid').setSelection(rid, true)", r["_id"])
        except Exception:
            pass
        got, msg = wait_form(page, int(cfg.get("form_wait_ms", 25000)))
        if not got:
            log("    问卷加载失败：%s" % msg)
            continue

        if not click_btn(page, "#btn_xspj_tj"):
            log("    点击提交失败")
            continue
        page.wait_for_timeout(1500)
        dlg = dismiss_dialog(page, 2000)
        if dlg:
            log("    弹层：%s" % dlg)
        toast = get_toast(page)
        if toast:
            log("    返回：%s" % toast)
        dismiss_dialog(page, 1500)
        page.wait_for_timeout(2000)
        toast2 = get_toast(page)
        if toast2 and toast2 != toast:
            log("    返回：%s" % toast2)
        dismiss_dialog(page, 800)

        try:
            st = page.evaluate("(rid) => jQuery('#tempGrid').getRowData(rid).tjzt", r["_id"])
        except Exception:
            st = ""
        if str(st) == "1":
            ok_n += 1
            log("    已提交")
        else:
            log("    !! 提交后状态为 %s" % st)

    rows = get_rows(page, expand=False)
    left = [r for r in rows if str(r.get("tjzt")) == "0"]
    done = [r for r in rows if str(r.get("tjzt")) == "1"]
    log("提交完成：本次提交 %d 条；当前已提交 %d 条，仍待提交 %d 条"
        % (ok_n, len(done), len(left)))
    return ok_n > 0

def dump_page(page, name):
    DUMP.mkdir(exist_ok=True)
    (DUMP / (name + ".html")).write_text(page.content(), encoding="utf-8")
    try:
        page.screenshot(path=str(DUMP / (name + ".png")), full_page=True)
    except Exception:
        pass
    log("已导出 dump/%s.html" % name)

def do_inspect(page, cfg):
    dump_page(page, "pjlist")
    rows = get_rows(page)
    log("列表 %d 条：" % len(rows))
    for r in rows:
        log("  [%s] %s | tjzt=%s fqpjzt=%s pjmbmcb=%s"
            % (r["_id"], row_label(r), r.get("tjzt"), r.get("fqpjzt"),
               (r.get("pjmbmcb_id") or "")[:12]))

    target = None
    for r in rows:
        if str(r.get("fqpjzt")) != "1":
            target = r
            break
    if not target:
        log("没有可评测的条目")
        return

    page.evaluate("(rid) => jQuery('#tempGrid').setSelection(rid, true)", target["_id"])
    ok, msg = wait_form(page, 20000)
    dump_page(page, "pjform")
    log("问卷加载：%s %s" % (ok, msg))
    info = page.evaluate(FORM_INFO_JS)
    for item in info:
        opts = "/".join(o["text"] for o in item["opts"])
        log("  第%d题 [%s] %s" % (item["i"] + 1, opts, item["q"][:60]))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-submit", action="store_true")
    ap.add_argument("--inspect", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    pool = CommentPool(seed=cfg.get("random_seed"))
    solver = CaptchaSolver(cfg)

    log("=" * 56)
    log("评语池 %d 条" % CommentPool.CAPACITY)
    log("=" * 56)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / cfg["user_data_dir"]),
            headless=cfg["headless"],
            slow_mo=cfg.get("slow_mo", 0),
            no_viewport=True,
            args=["--start-maximized", "--disable-blink-features=AutomationControlled"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(30000)

        page = ensure_login(page, ctx, cfg, solver)
        if page is None:
            log("登录失败，退出")
            ctx.close()
            return 1

        if args.inspect:
            do_inspect(page, cfg)
            log("inspect 完成，产物在 dump/")
            ctx.close()
            return 0

        rows = get_rows(page)
        log("列表共 %d 条" % len(rows))
        if not rows:
            log("没有取到教学班列表，先跑 --inspect 看结构")
            ctx.close()
            return 1

        ok_n = skip_n = fail_n = dry_n = 0
        for r in rows:
            if args.limit and (ok_n + skip_n + fail_n + dry_n) >= args.limit:
                log("达到 --limit %d" % args.limit)
                break
            if cfg.get("skip_submitted") and str(r.get("tjzt")) == "1":
                log("已提交，跳过：%s" % row_label(r))
                skip_n += 1
                continue
            log("-" * 50)
            log("处理：%s（tjzt=%s）" % (row_label(r), r.get("tjzt")))
            try:
                res = eval_one(page, r, cfg, pool, dry=args.dry)
            except Exception as e:
                log("  异常：%s" % e)
                res = False
            if res is True:
                ok_n += 1
            elif res == "skip":
                skip_n += 1
            elif res == "dry":
                dry_n += 1
            else:
                fail_n += 1
            log("进度：保存 %d / 跳过 %d / 失败 %d / 试跑 %d" % (ok_n, skip_n, fail_n, dry_n))
            human_pause(cfg)

        log("=" * 56)
        log("保存完成：%d 条；跳过 %d；失败 %d" % (ok_n, skip_n, fail_n))

        if args.dry or args.no_submit:
            log("按参数不提交")
        elif cfg.get("auto_submit") and ok_n > 0:
            submit_all(page, cfg)
        else:
            log("没有成功保存的记录，不提交")

        log("浏览器保持打开，Ctrl+C 结束")
        try:
            while True:
                page.wait_for_timeout(5000)
        except KeyboardInterrupt:
            pass
        ctx.close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
