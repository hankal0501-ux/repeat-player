@echo off
chcp 65001 > nul
cd /d "%~dp0"
title Mobile PWA 서버 (테스트용)

REM LAN IP 자동 감지
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R /C:"IPv4.*: 192\." /C:"IPv4.*: 172\." /C:"IPv4.*: 10\."') do (
    set "LAN_IP=%%a"
    goto :gotip
)
:gotip
set "LAN_IP=%LAN_IP: =%"

echo ============================================
echo    Mobile PWA 테스트 서버
echo ============================================
echo  PC : http://localhost:8080/
if defined LAN_IP echo  폰 : http://%LAN_IP%:8080/
echo.
echo  배포 시: 이 폴더 통째로 GitHub Pages/Netlify에 업로드
echo  종료: Ctrl+C
echo ============================================
echo.
python -m http.server 8080
