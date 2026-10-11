@echo off
rem Startar Turbo-ranking. Dubbelklicka pa filen. Stang fonstret for att stoppa.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py --version
  py turbo_site.py
) else (
  python --version
  python turbo_site.py
)
echo.
echo Sajten har stoppats. Om det star ett fel ovan: kopiera texten till Claude.
pause
