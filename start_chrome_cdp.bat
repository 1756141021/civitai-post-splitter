@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
echo ============================================================
echo  启动 Chrome（CDP 远程调试端口 9222）
echo ============================================================
echo.

:: 从注册表查 Chrome 路径
set "CHROME_PATH="
for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe" /ve 2^>nul') do set "CHROME_PATH=%%b"
if not defined CHROME_PATH (
    if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
        set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
    ) else if exist "C:\Program Files (x86)\Google\Chrome\Application\chrome.exe" (
        set "CHROME_PATH=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"
    ) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
        set "CHROME_PATH=%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"
    )
)
if not defined CHROME_PATH (
    echo [错误] 找不到 Chrome。请安装 Chrome 或手动编辑此文件指定路径。
    pause
    exit /b 1
)
echo Chrome: !CHROME_PATH!
echo.

:: Chrome 已在运行时，新实例的 --remote-debugging-port 会被忽略
tasklist /FI "IMAGENAME eq chrome.exe" 2>nul | find "chrome.exe" >nul
if !errorlevel!==0 (
    echo [!] 检测到 Chrome 正在运行。
    echo     必须先关闭所有 Chrome 窗口，否则 CDP 端口不会生效。
    echo.
    set /p "confirm=关闭所有 Chrome 并继续？(Y/N) "
    if /i "!confirm!"=="Y" (
        taskkill /F /IM chrome.exe >nul 2>&1
        timeout /t 2 /nobreak >nul
    ) else (
        echo 已取消。请手动关闭 Chrome 后重试。
        pause
        exit /b
    )
)

echo 正在启动 Chrome...
start "" "!CHROME_PATH!" --remote-debugging-port=9222
echo.
echo [OK] Chrome 已启动（CDP 端口 9222）
echo.
echo ---- 使用流程 ----
echo 1. 在刚打开的 Chrome 里登录小红书
echo 2. 然后运行 run_web.bat 或 run.bat
echo 3. 程序会直接连接这个浏览器，不再另开窗口
echo.
echo 发布期间不要关闭这个 Chrome。
echo.
pause
