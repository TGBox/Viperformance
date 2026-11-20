@echo off
setlocal
rem Doppelklick startet die Aufzeichnung

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0bottleneck_tracker.py" %*
    goto :done
)

where uv >nul 2>&1
if not errorlevel 1 (
    uv run --project "%~dp0." "%~dp0bottleneck_tracker.py" %*
    goto :done
)

python "%~dp0bottleneck_tracker.py" %*

:done
pause
