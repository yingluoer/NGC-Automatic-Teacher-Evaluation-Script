@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================
echo   学生评教控制台 - 打包单文件 exe
echo ============================================

set PY=python
where python >nul 2>nul || set PY=py

echo [1/4] 检查依赖...
%PY% -m pip install --quiet --upgrade pip
%PY% -m pip install --quiet pyinstaller playwright ddddocr pillow opencv-python-headless
if errorlevel 1 goto :fail

echo [2/4] 准备内置浏览器...
%PY% prep_browsers.py
if errorlevel 1 echo [警告] 浏览器准备失败，exe 将依赖本机已装的 Chromium

echo [3/4] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
set TMP=%CD%\..\tmp
set TEMP=%CD%\..\tmp
if not exist "%TMP%" mkdir "%TMP%"

echo [4/4] 开始打包（约 2-4 分钟，产物约 320MB）...
%PY% -m PyInstaller --noconfirm --clean --workpath "%TMP%\pybuild" njit-pj.spec
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   完成：dist\njit-pj.exe
echo   双击运行，浏览器自动打开控制台
echo   首次启动会把内置浏览器释放到
echo   %%LOCALAPPDATA%%\njit-pj-browsers（约 280MB，只做一次）
echo ============================================
goto :eof

:fail
echo.
echo [失败] 上面有报错信息，检查依赖是否装全
exit /b 1
