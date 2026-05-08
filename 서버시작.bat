@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 통합 플레이어 서버

REM 현재 LAN IPv4 자동 감지
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4.*: 192\." /C:"IPv4.*: 172\." /C:"IPv4.*: 10\."') do (
    set "LAN_IP=%%a"
    goto :gotip
)
:gotip
set "LAN_IP=%LAN_IP: =%"

echo ============================================
echo    통합 플레이어 서버 시작
echo ============================================
echo  PC : http://localhost:5757/
if defined LAN_IP echo  폰 : http://%LAN_IP%:5757/
echo  종료: 이 창에서 Ctrl+C
echo ============================================
echo.
python server.py
echo.
echo [서버가 종료되었습니다]
pause
