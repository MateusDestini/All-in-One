@echo off
REM Gera o instalador dist\DestiniSetup.exe e a pasta dist\MonitorLentidao (versao portatil).
cd /d "%~dp0"
python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" || goto versao
python -m pip install --upgrade -r requirements.txt pyinstaller || goto erro
python -m PyInstaller --noconfirm --onedir --noconsole --name MonitorLentidao ^
  --icon "monitor_lentidao\ui\marca\icone.ico" ^
  --hidden-import pystray._win32 --hidden-import clr --collect-all webview ^
  --collect-all pythonnet --collect-all clr_loader ^
  --add-data "monitor_lentidao\ui;monitor_lentidao\ui" ^
  --add-data "monitor_lentidao\nativo\lhm\*.dll;monitor_lentidao\nativo\lhm" ^
  --add-data "monitor_lentidao\nativo\pawnio\*.bin;monitor_lentidao\nativo\pawnio" ^
  --add-binary "monitor_lentidao\nativo\memteste.dll;monitor_lentidao\nativo" ^
  --add-binary "monitor_lentidao\nativo\estavel.dll;monitor_lentidao\nativo" MonitorLentidao.pyw || goto erro
for /f %%v in ('python -c "import monitor_lentidao as m; print(m.__version__)"') do set VERSAO=%%v
set ISCC=
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set ISCC=%%p
if not defined ISCC (
  echo.
  echo Pasta pronta: dist\MonitorLentidao\MonitorLentidao.exe
  echo Para gerar o instalador, instale o Inno Setup 6 ^(winget install JRSoftware.InnoSetup^) e rode de novo.
  pause
  exit /b 0
)
%ISCC% /Qp /DVersao=%VERSAO% instalador.iss || goto erro
echo.
echo Pronto: dist\DestiniSetup.exe ^(instalador^) e dist\MonitorLentidao ^(portatil^)
pause
exit /b 0
:versao
echo.
echo Instale o Python 3.11 ou mais novo (python.org) marcando "Add python.exe to PATH".
pause
exit /b 1
:erro
echo.
echo Falhou. Verifique se o Python esta instalado e no PATH.
pause
exit /b 1
