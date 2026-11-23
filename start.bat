@echo off
setlocal
rem Doppelklick startet die Aufzeichnung
rem Die CPU-Temperatur ist nur mit Admin-Rechten lesbar: einmal per UAC nachfragen.
rem Wird die Nachfrage abgelehnt, bleibt das Fenster zu - dann  uv run bottleneck_tracker.py  nutzen.

fltmc >nul 2>&1
if errorlevel 1 (
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
    )
    exit /b
)

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
