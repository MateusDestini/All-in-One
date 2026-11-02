; Instalador do Destini · Monitor de Lentidão (Inno Setup 6).
; Gerado pelo compilar.bat e pelo GitHub Actions depois do PyInstaller (pasta dist\MonitorLentidao).
; Instala só para o usuário atual, sem pedir administrador, em %LOCALAPPDATA%\Programs\Destini.

#ifndef Versao
  #define Versao "0.0.0"
#endif

[Setup]
AppId={{7B1F4C2E-5D8A-4E39-9C61-0D3E7A2B9F54}
AppName=Destini · Monitor de Lentidão
AppVersion={#Versao}
AppVerName=Destini · Monitor de Lentidão {#Versao}
AppPublisher=Destini
DefaultDirName={localappdata}\Programs\Destini
DefaultGroupName=Destini
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=DestiniSetup
SetupIconFile=monitor_lentidao\ui\marca\icone.ico
UninstallDisplayIcon={app}\MonitorLentidao.exe
UninstallDisplayName=Destini · Monitor de Lentidão
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
; Fecha o monitor aberto antes de atualizar (no fim, a opção "Abrir o monitor agora" abre de novo).
CloseApplications=force
RestartApplications=no

[Languages]
Name: "pt"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"

[Tasks]
Name: "iniciar"; Description: "Abrir junto com o Windows (fica perto do relógio, gastando o mínimo)"
Name: "atalho"; Description: "Criar atalho na área de trabalho"; Flags: unchecked

[Files]
Source: "dist\MonitorLentidao\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Restos de uma versão anterior (arquivos que deixaram de existir).
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{userprograms}\Destini · Monitor de Lentidão"; Filename: "{app}\MonitorLentidao.exe"
Name: "{userdesktop}\Destini · Monitor de Lentidão"; Filename: "{app}\MonitorLentidao.exe"; Tasks: atalho

[Registry]
; Mesmo valor que o interruptor "Iniciar com o Windows" do painel usa.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "MonitorLentidao"; ValueData: """{app}\MonitorLentidao.exe"" --bandeja"; Tasks: iniciar; Flags: uninsdeletevalue

[Run]
Filename: "{app}\MonitorLentidao.exe"; Description: "Abrir o monitor agora"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/im MonitorLentidao.exe /f"; Flags: runhidden; RunOnceId: "FecharMonitor"

[UninstallDelete]
; As configurações e o histórico ficam em %LOCALAPPDATA%\MonitorLentidao e não são apagados.
Type: filesandordirs; Name: "{app}"
