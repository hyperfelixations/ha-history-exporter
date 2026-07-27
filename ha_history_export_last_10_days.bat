@echo off
setlocal

rem Always run from the directory containing this batch file.
cd /d "%~dp0"

rem Calculate ISO dates in local time:
rem   start date = today minus 10 calendar days
rem   end date   = today
for /f "usebackq tokens=1,2" %%A in (`powershell.exe -NoProfile -Command "$today = Get-Date; '{0:yyyy-MM-dd} {1:yyyy-MM-dd}' -f $today.AddDays(-10), $today"`) do (
    set "START_DATE=%%A"
    set "END_DATE=%%B"
)

if not defined START_DATE (
    echo [ERROR] Start- und Enddatum konnten nicht berechnet werden.
    goto :finish
)

echo ============================================================
echo   Home Assistant History Export
echo ============================================================
echo   Startdatum: %START_DATE%
echo   Enddatum:   %END_DATE%
echo   Hinweis: Der heutige, noch unvollstaendige Tag wird vom
echo            bestehenden Exporter automatisch uebersprungen.
echo ============================================================
echo.

python ha_history_batch_export.py --start-date %START_DATE% --end-date %END_DATE%
set "EXPORT_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXPORT_EXIT_CODE%"=="0" (
    echo Exporter wurde erfolgreich beendet.
) else (
    echo [ERROR] Exporter wurde mit Exit-Code %EXPORT_EXIT_CODE% beendet.
)

:finish
echo.
pause
endlocal
