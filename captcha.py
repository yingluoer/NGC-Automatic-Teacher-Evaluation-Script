
"""
验证码识别：三种模式
  ocr      本地 ddddocr 离线识别（默认，装 pip install ddddocr 即可）
  manual   把图片存到 dump/，终端输入答案
  platform 打码平台：ttshitu（图鉴）/ chaojiying（超级鹰）
"""

import base64
import hashlib
import json
import pathlib
import time
import urllib.parse
import urllib.request

from utils import DUMP

ROOT = pathlib.Path(__file__).resolve().parent

class CaptchaSolver:
    def __init__(self, cfg):
        self.mode = (cfg.get("captcha_mode") or "ocr").lower()
        self.plat = cfg.get("captcha_platform") or {}
        self.timeout = int(cfg.get("captcha_timeout", 20))
        self._ocr = None
        self.cache = {}

    def solve(self, image_bytes):
        if not image_bytes:
            return ""
        key = hashlib.md5(image_bytes).hexdigest()[:32]
        if key in self.cache:
            return self.cache[key]

        text = ""
        if self.mode == "ocr":
            text = self._by_ocr(image_bytes)
        elif self.mode == "platform":
            text = self._by_platform(image_bytes)
        elif self.mode == "manual":
            text = self._by_manual(image_bytes)

        if not text and self.mode not in ("manual",):
            print("  [验证码] 自动识别失败，转人工输入")
            text = self._by_manual(image_bytes)

        text = (text or "").strip()
        self.cache[key] = text
        return text

    def _by_ocr(self, image_bytes):
        try:
            import ddddocr
        except ImportError:
            print("  [验证码] 未安装 ddddocr，执行：pip install ddddocr")
            return ""
        if self._ocr is None:
            self._ocr = ddddocr.DdddOcr(show_ad=False, beta=True)
        for _ in range(2):
            try:
                r = self._ocr.classification(image_bytes)
                if r:

                    return "".join(ch for ch in r if ch.isalnum())
            except Exception as e:
                print("  [验证码] OCR 异常：%s" % e)
                time.sleep(0.3)
        return ""

    def _by_manual(self, image_bytes):
        DUMP.mkdir(exist_ok=True)
        p = DUMP / ("captcha_%d.png" % int(time.time() * 1000))
        p.write_bytes(image_bytes)
        try:
            from PIL import Image
            Image.open(p).show()
        except Exception:
            pass
        print("  验证码图片已保存：%s" % p)
        try:
            return input("  请输入验证码后回车：").strip()
        except EOFError:
            return ""

    def _by_platform(self, image_bytes):
        kind = (self.plat.get("type") or "ttshitu").lower()
        if kind == "ttshitu":
            return self._ttshitu(image_bytes)
        if kind == "chaojiying":
            return self._chaojiying(image_bytes)
        print("  [验证码] 不支持的平台：%s" % kind)
        return ""

    def _ttshitu(self, image_bytes):
        user = self.plat.get("username") or ""
        pwd = self.plat.get("password") or ""
        if not user or not pwd:
            print("  [验证码] 未配置 ttshitu 账号")
            return ""
        body = json.dumps({
            "username": user,
            "password": pwd,
            "typeid": str(self.plat.get("typeid") or "3"),
            "image": base64.b64encode(image_bytes).decode(),
        }).encode()
        req = urllib.request.Request(
            "http://api.ttshitu.com/predict",
            data=body,
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                j = json.loads(r.read().decode())
            if j.get("success"):
                return j["data"]["result"]
            print("  [验证码] 图鉴返回：%s" % j.get("message"))
        except Exception as e:
            print("  [验证码] 图鉴请求失败：%s" % e)
        return ""

    def _chaojiying(self, image_bytes):
        user = self.plat.get("username") or ""
        pwd = self.plat.get("password") or ""
        soft = self.plat.get("softid") or ""
        if not user or not pwd:
            print("  [验证码] 未配置 chaojiying 账号")
            return ""
        data = urllib.parse.urlencode({
            "user": user,
            "pass2": pwd,
            "softid": soft,
            "codetype": str(self.plat.get("typeid") or "1902"),
        }).encode()
        boundary = "----wbboundary"
        body = (
            ("--%s\r\nContent-Disposition: form-data; name=\"user\"\r\n\r\n%s\r\n" % (boundary, user)).encode()
            + ("--%s\r\nContent-Disposition: form-data; name=\"pass2\"\r\n\r\n%s\r\n" % (boundary, pwd)).encode()
            + ("--%s\r\nContent-Disposition: form-data; name=\"softid\"\r\n\r\n%s\r\n" % (boundary, soft)).encode()
            + ("--%s\r\nContent-Disposition: form-data; name=\"codetype\"\r\n\r\n%s\r\n"
               % (boundary, str(self.plat.get("typeid") or "1902"))).encode()
            + ("--%s\r\nContent-Disposition: form-data; name=\"userfile\"; filename=\"a.png\"\r\n"
               "Content-Type: image/png\r\n\r\n" % boundary).encode()
            + image_bytes
            + ("\r\n--%s--\r\n" % boundary).encode()
        )
        req = urllib.request.Request(
            "http://upload.chaojiying.net/Upload/Processing.php",
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                j = json.loads(r.read().decode())
            if j.get("err_no") == 0:
                return j.get("pic_str", "")
            print("  [验证码] 超级鹰返回：%s" % j.get("err_str"))
        except Exception as e:
            print("  [验证码] 超级鹰请求失败：%s" % e)
        return ""
