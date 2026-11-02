@echo off
REM Roda um diagnostico unico no console (cerca de 1 minuto) sem ficar na bandeja.
cd /d "%~dp0"
python -m monitor_lentidao --diagnostico
pause
