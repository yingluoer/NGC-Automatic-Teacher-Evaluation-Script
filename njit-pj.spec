# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置 —— 单文件 exe，内置 headless 浏览器

  pyinstaller --noconfirm --clean njit-pj.spec

产物：dist/njit-pj.exe
  首次运行会把内置浏览器释放到 %LOCALAPPDATA%\\njit-pj-browsers（约 280MB，只做一次），
  之后启动直接复用，不再重复解压。
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("web", "web"),
    ("config.json", "."),
    ("browsers_src", "browsers"),
]
binaries = []
hiddenimports = ["runner", "pj", "captcha", "comments", "utils"]

for pkg in ("playwright", "ddddocr", "onnxruntime", "cv2", "numpy", "PIL"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print("collect_all(%s) skipped: %s" % (pkg, e))

try:
    hiddenimports += collect_submodules("playwright")
except Exception:
    pass

# 只做图片识别，opencv 的 ffmpeg 视频后端（30MB）用不到
_DROP = ("opencv_videoio_ffmpeg",)

a = Analysis(
    ["server.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter", "matplotlib", "scipy", "pandas", "pytest",
        "PyQt5", "PySide2", "notebook", "IPython", "slide",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

# cv2 hook 会把 ffmpeg 重新塞回来，必须在 Analysis 之后再过滤一次
a.binaries = [b for b in a.binaries
              if not any(x in str(b[0]).lower() for x in _DROP)]

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="NGC-ZDPJ",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon="icon.ico",
)
