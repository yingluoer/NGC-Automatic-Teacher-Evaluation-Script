
"""路径解析：源码运行和 PyInstaller 打包后都指向正确位置。"""

import pathlib
import sys

def app_dir():
    """可写目录：exe 同级（打包后）或脚本目录（源码）。"""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent

def res_dir():
    """只读资源目录：打包后是 _MEIPASS，源码时同 app_dir。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return pathlib.Path(base)
    return app_dir()

APP = app_dir()
RES = res_dir()
DATA = APP / "data"
DATA.mkdir(parents=True, exist_ok=True)

DUMP = DATA / "dump"
PROFILES = DATA / "profiles"

def config_file():
    """优先用 exe 同级的 config.json，没有就用打包进去的。"""
    local = APP / "config.json"
    if local.exists():
        return local
    return RES / "config.json"
