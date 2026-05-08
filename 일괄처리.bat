@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo === urls.txt 일괄 처리 시작 ===
python batch.py
echo.
echo === 완료 ===
pause
