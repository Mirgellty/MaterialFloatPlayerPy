@echo off
rem 启动悬浮播放器（用项目自带的虚拟环境）
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m mfp_py %*
) else (
    python -m mfp_py %*
)
