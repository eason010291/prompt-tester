@echo off
rem Build the frontend, then start Prompt Tester.
rem Works from any directory and returns to it afterwards.
rem Extra arguments go to prompt-tester, e.g.  build_n_start.bat --port 8001
setlocal
set "ROOT=%~dp0"

pushd "%ROOT%web" || (echo [error] web folder not found & exit /b 1)
if not exist node_modules (
    echo [1/3] Installing frontend packages...
    call npm install || goto :fail
)
echo [2/3] Building frontend...
call npm run build || goto :fail
popd

echo [3/3] Starting server (default http://127.0.0.1:8000, Ctrl+C to stop)
uv run --project "%ROOT%." prompt-tester %*
exit /b %errorlevel%

:fail
popd
echo [error] Frontend build failed.
exit /b 1
