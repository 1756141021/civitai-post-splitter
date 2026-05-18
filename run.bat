@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -c "import patchright" 2>nul || (
    echo Installing patchright...
    pip install patchright
    patchright install chromium
)
python launcher.py
