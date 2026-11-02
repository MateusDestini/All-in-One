"""Instalar apps essenciais (pós-formatação) pelo winget, o gerenciador de pacotes do Windows.

O winget baixa sempre a versão oficial mais recente do site do fabricante e confere o hash do instalador.
Catálogo e códigos de retorno adaptados do ResetExplode (AppInstaller.cs; licença MIT, © 2026 Gustavo).
"""

import os
import subprocess
from pathlib import Path

from . import windows

CATALOGO = [
    {"id": "Valve.Steam", "nome": "Steam", "descricao": "Loja e biblioteca de jogos da Valve.", "prefixo": "Steam"},
    {"id": "Google.Chrome", "nome": "Google Chrome", "descricao": "Navegador do Google.", "prefixo": "Google Chrome"},
    {"id": "Discord.Discord", "nome": "Discord", "descricao": "Chat de voz e texto para jogar com os amigos.",
     "prefixo": "Discord"},
    {"id": "Skillbrains.Lightshot", "nome": "Lightshot", "descricao": "Captura de tela rápida com a tecla Print Screen.",
     "prefixo": "Lightshot"},
    {"id": "7zip.7zip", "nome": "7-Zip", "descricao": "Compactador gratuito (zip, rar, 7z).", "prefixo": "7-Zip"},
    {"id": "RARLab.WinRAR", "nome": "WinRAR", "descricao": "Compactador clássico (pago após o teste). Escolha ele ou o 7-Zip.",
     "prefixo": "WinRAR", "pre": False},
    {"id": "VideoLAN.VLC", "nome": "VLC", "descricao": "Player de vídeo e música que abre qualquer formato.",
     "prefixo": "VLC media player"},
]

LOJA_WINGET = "ms-windows-store://pdp/?productid=9NBLGGH4NNS1"  # "Instalador de Aplicativo", que traz o winget


def caminho_winget() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "winget.exe"


def winget_disponivel() -> bool:
    return windows.EH_WINDOWS and caminho_winget().exists()


def nomes_instalados() -> list[str]:
    """Nomes em "Aplicativos instalados" (máquina 64/32 bits e usuário atual)."""
    if not windows.EH_WINDOWS:
        return []
    import winreg
    nomes = []
    caminho = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for raiz, visao in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY), (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(raiz, caminho, 0, winreg.KEY_READ | visao) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as sub:
                            nome = str(winreg.QueryValueEx(sub, "DisplayName")[0]).strip()
                            if nome:
                                nomes.append(nome)
                    except OSError:
                        continue
        except OSError:
            continue
    return nomes


def catalogo() -> dict:
    nomes = [n.lower() for n in nomes_instalados()]
    lista = []
    for a in CATALOGO:
        instalado = any(n.startswith(a["prefixo"].lower()) for n in nomes)
        lista.append(dict(a, instalado=instalado, marcado=a.get("pre", True) and not instalado))
    return {"apps": lista, "winget": winget_disponivel()}


def _ultima_linha(saida: str) -> str:
    """Última linha útil da saída do winget (sem as barras de progresso)."""
    for linha in reversed(saida.replace("\r", "\n").split("\n")):
        linha = linha.strip()
        if len(linha) > 3 and not set(linha) <= set("-\\|/█▒ ") and "KB /" not in linha and "MB /" not in linha:
            return linha
    return ""


def instalar_winget(pacote: str, atualizar: bool = False) -> tuple[bool, str]:
    """Instala um pacote em silêncio. Devolve (ok, mensagem curta)."""
    if not winget_disponivel():
        return False, "O winget não foi encontrado."
    cmd = [str(caminho_winget()), "upgrade" if atualizar else "install", "--id", pacote, "-e", "--silent",
           "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=1800, creationflags=windows.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "Demorou demais (mais de 30 minutos)."
    except OSError as e:
        return False, f"Não foi possível iniciar o winget ({e})."
    codigo = r.returncode & 0xFFFFFFFF
    saida = (r.stdout + r.stderr).decode("utf-8", errors="replace")
    if codigo == 0:
        return True, "Instalado."
    if codigo in (0x8A15002B, 0x8A150061):
        return True, "Já estava instalado."
    if codigo == 0x8A150014:
        return False, "Pacote não encontrado no winget."
    if codigo == 0x8A150008:
        return False, "Falha no download: confira a internet e tente de novo."
    return False, f"Falhou (código 0x{codigo:08X}). {_ultima_linha(saida)}".strip()


def instalar(progresso, ids: list[str]) -> dict:
    """Instala os apps escolhidos, um por vez. Devolve o status de cada um."""
    escolhidos = [a for a in CATALOGO if a["id"] in ids]
    status, ok = {}, 0
    for n, a in enumerate(escolhidos):
        progresso(n / max(1, len(escolhidos)),
                  f"Instalando {a['nome']} ({n + 1} de {len(escolhidos)})... baixando do site oficial, pode levar alguns minutos.")
        sucesso, msg = instalar_winget(a["id"])
        status[a["id"]] = {"ok": sucesso, "mensagem": msg}
        ok += sucesso
    progresso(1.0, "Concluído")
    falhas = [a["nome"] for a in escolhidos if not status[a["id"]]["ok"]]
    return {"status": status, "resumo": f"{ok} de {len(escolhidos)} instalados." +
            (f" Falharam: {', '.join(falhas)} (veja o motivo na lista)." if falhas else "")}


def abrir_loja() -> None:
    if windows.EH_WINDOWS:
        os.startfile(LOJA_WINGET)
