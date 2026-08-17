@echo off
chcp 65001 >nul
rem 벤치마크 보기 — 이 파일을 더블클릭하면 된다.
rem 인물로 좁히려면 명령창에서: benchmark.bat 한소희
cd /d "%~dp0"

rem py 런처가 있으면 그걸 쓰고, 없으면 설치 경로를 직접 쓴다
set PY=py
where py >nul 2>nul || set PY="C:\Users\User\AppData\Local\Programs\Python\Python312\python.exe"

%PY% benchmark.py %*

echo.
echo ─────────────────────────────────────────
echo 창을 닫으려면 아무 키나 누르세요.
pause >nul
