
"""打包前把 headless 浏览器复制到 browsers_src，供 PyInstaller 内置进 exe。"""

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

DST = Path("browsers_src")

def find(la):
    pattern = os.path.join(la, "ms-playwright", "chromium_headless_shell-*")
    return sorted(glob.glob(pattern))

def main():
    la = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    srcs = find(la)
    if not srcs:
        print("本机没有 headless shell，先下载 ...")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=False)
        srcs = find(la)
    if not srcs:
        print("下载失败，浏览器不会被内置")
        return 1

    DST.mkdir(exist_ok=True)
    copied = 0
    for s in srcs:
        name = os.path.basename(s)
        target = DST / name
        if target.exists() and any(target.iterdir()):
            print("已存在，跳过：%s" % name)
            continue
        print("复制 %s ..." % name)
        shutil.copytree(s, target, dirs_exist_ok=True)
        copied += 1

    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print("browsers_src 就绪：%d 个组件，%.0f MB" % (len(srcs), total / 1024 / 1024))
    return 0

if __name__ == "__main__":
    sys.exit(main())
