@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import patchright" 2>nul || (
    echo Installing patchright...
    pip install patchright
    patchright install chromium
)
echo Starting Civitai Post Splitter Web Mode...
python web_server.py
pause
