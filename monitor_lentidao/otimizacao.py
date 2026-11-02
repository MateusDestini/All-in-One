"""Otimização: limpar a memória, fechar apps de segundo plano e ajustes do Windows com "Restaurar originais".

Só ajustes documentados e reversíveis; nada que reduza a segurança (Defender, VBS, UAC, Windows Update).
O valor original de cada ajuste é guardado antes da primeira mudança.
Adaptado do ResetExplode (Optimizer.cs e WindowsTweaks.cs, licença MIT, © 2026 Gustavo).
"""

import ctypes
import json
import logging
import os
import time

import psutil

from . import config, windows
from .energia import ALTO_DESEMPENHO

log = logging.getLogger(__name__)

EQUILIBRADO = "381b4222-f694-41f0-9685-ff5bb260df2e"
AUSENTE = "<ausente>"

# ------------------------------------------------------------------ memória

def limpar_memoria() -> dict:
    """Esvazia a lista de espera da RAM (como o ISLC) e enxuga a memória dos processos.

    A lista de espera é cache que o Windows libera sozinho quando precisa; esvaziar ajuda quando a RAM
    está quase cheia e programas ou jogos engasgam ao carregar coisas novas.
    """
    antes = psutil.virtual_memory().available
    enxugados, esvaziou, erro = 0, False, ""
    if windows.EH_WINDOWS:
        from ctypes import wintypes
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        psapi.EmptyWorkingSet.argtypes = [wintypes.HANDLE]
        eu = os.getpid()
        for pid in psutil.pids():
            if pid <= 4 or pid == eu:
                continue
            h = k32.OpenProcess(0x1000 | 0x0100, False, pid)  # QUERY_LIMITED_INFORMATION | SET_QUOTA
            if h:
                enxugados += bool(psapi.EmptyWorkingSet(h))
                k32.CloseHandle(h)
        try:
            _habilitar_privilegio("SeProfileSingleProcessPrivilege")
            comando = ctypes.c_int(4)  # MemoryPurgeStandbyList
            status = ctypes.WinDLL("ntdll").NtSetSystemInformation(80, ctypes.byref(comando), ctypes.sizeof(comando))
            esvaziou = status == 0
            if not esvaziou:
                erro = "O Windows só deixa esvaziar o cache com o monitor aberto como administrador."
        except OSError as e:
            erro = str(e)
    time.sleep(0.3)
    liberado = max(0, psutil.virtual_memory().available - antes)
    return {"liberado_mb": round(liberado / 2**20), "enxugados": enxugados, "cache_esvaziado": esvaziou, "erro": erro}


def _habilitar_privilegio(nome: str) -> None:
    from ctypes import wintypes

    class LUID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Luid", ctypes.c_int64), ("Attributes", wintypes.DWORD)]

    class TOKEN_PRIVILEGES(ctypes.Structure):
        _fields_ = [("PrivilegeCount", wintypes.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(ctypes.WinDLL("kernel32").GetCurrentProcess(), 0x0020 | 0x0008, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    luid = ctypes.c_int64()
    if adv.LookupPrivilegeValueW(None, nome, ctypes.byref(luid)):
        tp = TOKEN_PRIVILEGES(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid.value, 2)))
        adv.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
    ctypes.WinDLL("kernel32").CloseHandle(token)


# ------------------------------------------------------------------ apps de segundo plano

SEGUNDO_PLANO = {
    "onedrive.exe": "Sincronização do OneDrive (usa disco e rede)",
    "ms-teams.exe": "Microsoft Teams", "teams.exe": "Microsoft Teams (clássico)", "skype.exe": "Skype",
    "phoneexperiencehost.exe": "Vincular ao Celular", "yourphone.exe": "Vincular ao Celular",
    "widgets.exe": "Widgets do Windows", "widgetservice.exe": "Serviço de Widgets",
    "adobearm.exe": "Atualizador da Adobe", "ccxprocess.exe": "Adobe Creative Cloud",
    "creative cloud.exe": "Adobe Creative Cloud", "adobeipcbroker.exe": "Adobe (comunicação em segundo plano)",
    "googledrivefs.exe": "Google Drive", "dropbox.exe": "Dropbox", "cortana.exe": "Cortana",
    "officeclicktorun.exe": "Atualizador do Office",
}


def apps_segundo_plano() -> list[dict]:
    grupos: dict[str, list] = {}
    for p in psutil.process_iter(["name", "memory_info"]):
        nome = (p.info["name"] or "").lower()
        if nome in SEGUNDO_PLANO and p.info["memory_info"]:
            grupos.setdefault(nome, []).append(p.info["memory_info"].rss)
    return sorted(({"nome": n, "descricao": SEGUNDO_PLANO[n], "processos": len(m), "ram_mb": round(sum(m) / 2**20)}
                   for n, m in grupos.items()), key=lambda a: -a["ram_mb"])


def fechar_apps(nomes: list[str]) -> dict:
    alvo = {n.lower() for n in nomes if n.lower() in SEGUNDO_PLANO}
    procs = [p for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() in alvo]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, vivos = psutil.wait_procs(procs, timeout=4)
    fechados = sorted({SEGUNDO_PLANO[n] for n in alvo} - {SEGUNDO_PLANO[(p.info["name"] or "").lower()] for p in vivos})
    msg = (f"Fechados: {', '.join(fechados)}." if fechados else "Nada foi fechado.")
    if vivos:
        msg += " Alguns precisam do monitor aberto como administrador."
    return {"ok": bool(fechados), "mensagem": msg}


# ------------------------------------------------------------------ ajustes do Windows

def _arquivo_backup():
    return config.pasta_dados() / "ajustes_originais.json"


def _backup() -> dict:
    try:
        return json.loads(_arquivo_backup().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _guardar(chave: str, valor) -> None:
    b = _backup()
    if chave not in b:  # só o valor de antes da PRIMEIRA mudança
        b[chave] = valor
        _arquivo_backup().write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")


def _ler_reg(raiz, caminho, nome):
    import winreg
    try:
        with winreg.OpenKey(raiz, caminho) as k:
            return winreg.QueryValueEx(k, nome)[0]
    except OSError:
        return None


def _gravar_reg(raiz, caminho, nome, valor, tipo=None):
    import winreg
    with winreg.CreateKeyEx(raiz, caminho, 0, winreg.KEY_SET_VALUE) as k:
        if valor is None:
            try:
                winreg.DeleteValue(k, nome)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(k, nome, 0, tipo if tipo is not None else
                              (winreg.REG_DWORD if isinstance(valor, int) else winreg.REG_SZ), valor)


class _Dword:
    """Ajuste que liga/desliga um valor DWORD do registro."""

    def __init__(self, raiz_hklm, caminho, nome, ligado, desligado, ausente_liga):
        self.hklm, self.caminho, self.nome = raiz_hklm, caminho, nome
        self.ligado, self.desligado, self.ausente_liga = ligado, desligado, ausente_liga

    def _raiz(self):
        import winreg
        return winreg.HKEY_LOCAL_MACHINE if self.hklm else winreg.HKEY_CURRENT_USER

    def ler(self):
        v = _ler_reg(self._raiz(), self.caminho, self.nome)
        return self.ausente_liga if v is None else v == self.ligado

    def aplicar(self, chave, ligar):
        v = _ler_reg(self._raiz(), self.caminho, self.nome)
        _guardar(chave, AUSENTE if v is None else v)
        _gravar_reg(self._raiz(), self.caminho, self.nome, self.ligado if ligar else self.desligado)

    def restaurar(self, original):
        _gravar_reg(self._raiz(), self.caminho, self.nome, None if original == AUSENTE else int(original))


_DX = r"Software\Microsoft\DirectX\UserGpuPreferences"


def _dx() -> dict:
    import winreg
    bruto = _ler_reg(winreg.HKEY_CURRENT_USER, _DX, "DirectXUserGlobalSettings") or ""
    return dict(p.split("=", 1) for p in bruto.split(";") if "=" in p)


AJUSTES = [
    {"id": "energia", "titulo": "Plano de energia Alto desempenho",
     "descricao": "Mantém o processador pronto para acelerar na hora, sem economia agressiva de energia.",
     "efeito": "Menos engasgos ao abrir programas e em jogos. Em notebook gasta mais bateria."},
    {"id": "modo_jogo", "titulo": "Modo de Jogo",
     "descricao": "Dá prioridade ao jogo e evita que o Windows Update instale ou reinicie no meio da partida.",
     "efeito": "Recomendado ligado (é o padrão do Windows 11)."},
    {"id": "game_dvr", "titulo": "Desligar a gravação em segundo plano (Game DVR)",
     "descricao": "Desliga a captura contínua do Xbox Game Bar, que grava os últimos minutos de jogo o tempo todo.",
     "efeito": "Recupera um pouco de desempenho e reduz uso da placa de vídeo e do disco."},
    {"id": "hags", "titulo": "Agendamento de GPU acelerado por hardware",
     "descricao": "A própria placa de vídeo organiza sua fila de trabalho, em vez do processador.",
     "efeito": "Pode reduzir a latência; necessário para o Frame Generation do DLSS. Precisa reiniciar o PC.",
     "admin": True, "reiniciar": True},
    {"id": "janela", "titulo": "Otimizações para jogos em janela",
     "descricao": "Jogos DirectX 10/11 em janela ou sem bordas passam a usar o modo de apresentação moderno.",
     "efeito": "Menor latência e suporte a G-Sync/FreeSync em janela. Recurso do Windows 11."},
    {"id": "mouse", "titulo": "Desligar a aceleração do mouse",
     "descricao": "Desmarca \"Aumentar a precisão do ponteiro\": o cursor anda sempre a mesma distância para o mesmo movimento da mão.",
     "efeito": "Mira mais consistente em jogos de tiro. A velocidade do cursor pode parecer diferente no começo.",
     "confirmar": "Muda 3 valores do mouse no registro do seu usuário (Painel de Controle\\Mouse) e vale na hora. "
                  "Os valores de antes ficam guardados: desligar aqui ou \"Restaurar originais\" volta exatamente como estava."},
]

_MOUSE = r"Control Panel\Mouse"
_MOUSE_NOMES = ("MouseThreshold1", "MouseThreshold2", "MouseSpeed")
_MOUSE_PADRAO = ("6", "10", "1")  # padrão do Windows: aceleração ligada


def _mouse_ler() -> list:
    import winreg
    return [_ler_reg(winreg.HKEY_CURRENT_USER, _MOUSE, n) for n in _MOUSE_NOMES]


def _mouse_definir(valores) -> None:
    """Grava os 3 valores e avisa o Windows (SPI_SETMOUSE), para valer sem sair da conta."""
    import winreg
    for nome, v in zip(_MOUSE_NOMES, valores):
        _gravar_reg(winreg.HKEY_CURRENT_USER, _MOUSE, nome, None if v is None else str(v), winreg.REG_SZ)
    try:
        nums = (ctypes.c_int * 3)(*[int(v) if str(v or "").lstrip("-").isdigit() else int(p)
                                    for v, p in zip(valores, _MOUSE_PADRAO)])
        ctypes.windll.user32.SystemParametersInfoW(0x0004, 0, nums, 0x02)  # SPI_SETMOUSE, SPIF_SENDCHANGE
    except Exception:
        log.warning("não deu para avisar o Windows da mudança do mouse", exc_info=True)


def _dword(id_):
    return {
        "modo_jogo": _Dword(False, r"Software\Microsoft\GameBar", "AutoGameModeEnabled", 1, 0, True),
        "hags": _Dword(True, r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers", "HwSchMode", 2, 1, False),
    }.get(id_)


def _plano_ativo() -> str | None:
    p = windows.plano_energia()
    return p[0] if p else None


def ler_ajuste(id_) -> bool:
    import winreg
    if id_ == "energia":
        return _plano_ativo() == ALTO_DESEMPENHO
    if id_ == "game_dvr":
        return (_ler_reg(winreg.HKEY_CURRENT_USER, r"System\GameConfigStore", "GameDVR_Enabled") == 0 and
                _ler_reg(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled") == 0)
    if id_ == "janela":
        return _dx().get("SwapEffectUpgradeEnable") == "1"
    if id_ == "mouse":
        return str(_mouse_ler()[2]) == "0"
    return _dword(id_).ler()


def aplicar_ajuste(id_, ligar: bool) -> None:
    import winreg
    if id_ == "energia":
        _guardar("energia", _plano_ativo())
        if ligar:
            if ALTO_DESEMPENHO not in windows.executar(["powercfg", "/list"], timeout=15).lower():
                # alguns PCs vêm sem o plano listado: duplicar o modelo o recria com o mesmo GUID
                windows.executar(["powercfg", "-duplicatescheme", ALTO_DESEMPENHO, ALTO_DESEMPENHO], timeout=15)
            windows.executar(["powercfg", "/setactive", ALTO_DESEMPENHO], timeout=15)
        else:
            antes = _backup().get("energia")
            windows.executar(["powercfg", "/setactive", antes if antes and antes != ALTO_DESEMPENHO else EQUILIBRADO], timeout=15)
    elif id_ == "game_dvr":
        for chave, caminho, nome in (("game_dvr.a", r"System\GameConfigStore", "GameDVR_Enabled"),
                                     ("game_dvr.b", r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled")):
            v = _ler_reg(winreg.HKEY_CURRENT_USER, caminho, nome)
            _guardar(chave, AUSENTE if v is None else v)
            _gravar_reg(winreg.HKEY_CURRENT_USER, caminho, nome, 0 if ligar else 1)
    elif id_ == "janela":
        bruto = _ler_reg(winreg.HKEY_CURRENT_USER, _DX, "DirectXUserGlobalSettings")
        _guardar("janela", AUSENTE if bruto is None else bruto)
        d = _dx()  # preserva as outras opções que o usuário já tinha
        d["SwapEffectUpgradeEnable"] = "1" if ligar else "0"
        _gravar_reg(winreg.HKEY_CURRENT_USER, _DX, "DirectXUserGlobalSettings", "".join(f"{k}={v};" for k, v in d.items()))
    elif id_ == "mouse":
        atual = _mouse_ler()
        _guardar("mouse", [AUSENTE if v is None else str(v) for v in atual])
        if ligar:
            _mouse_definir(("0", "0", "0"))
        else:
            antes = _backup().get("mouse") or []
            com_aceleracao = len(antes) == 3 and str(antes[2]) not in ("0", AUSENTE)
            _mouse_definir([None if v == AUSENTE else v for v in antes] if com_aceleracao else _MOUSE_PADRAO)
    else:
        _dword(id_).aplicar(id_, ligar)


def ajustes() -> list[dict]:
    saida = []
    for a in AJUSTES:
        try:
            ligado = ler_ajuste(a["id"]) if windows.EH_WINDOWS else False
        except OSError:
            ligado = False
        saida.append({**a, "ligado": ligado})
    return saida


def definir_ajuste(id_: str, ligar: bool) -> dict:
    a = next((a for a in AJUSTES if a["id"] == id_), None)
    if not a or not windows.EH_WINDOWS:
        return {"ok": False, "mensagem": "Ajuste indisponível."}
    try:
        aplicar_ajuste(id_, ligar)
    except PermissionError:
        return {"ok": False, "mensagem": "Esse ajuste precisa do monitor aberto como administrador."}
    except OSError as e:
        return {"ok": False, "mensagem": f"Não foi possível mudar: {e}"}
    return {"ok": True, "mensagem": f"{a['titulo']}: {'ligado' if ligar else 'desligado'}."
            + (" Reinicie o PC para valer." if a.get("reiniciar") else "")}


def tem_backup() -> bool:
    return bool(_backup())


def restaurar_originais() -> dict:
    import winreg
    b, falhas = _backup(), []
    for chave, original in b.items():
        try:
            if chave == "energia":
                if original:
                    windows.executar(["powercfg", "/setactive", original], timeout=15)
            elif chave.startswith("game_dvr."):
                caminho, nome = ((r"System\GameConfigStore", "GameDVR_Enabled") if chave.endswith("a") else
                                 (r"Software\Microsoft\Windows\CurrentVersion\GameDVR", "AppCaptureEnabled"))
                _gravar_reg(winreg.HKEY_CURRENT_USER, caminho, nome, None if original == AUSENTE else int(original))
            elif chave == "janela":
                _gravar_reg(winreg.HKEY_CURRENT_USER, _DX, "DirectXUserGlobalSettings", None if original == AUSENTE else original)
            elif chave == "mouse":
                _mouse_definir([None if v == AUSENTE else v for v in original])
            elif _dword(chave):
                _dword(chave).restaurar(original)
        except OSError:
            log.warning("falha ao restaurar %s", chave, exc_info=True)
            falhas.append(chave)
    restantes = {k: v for k, v in b.items() if k in falhas}
    _arquivo_backup().write_text(json.dumps(restantes, ensure_ascii=False), encoding="utf-8")
    if falhas:
        return {"ok": False, "mensagem": "Alguns ajustes precisam do monitor como administrador para voltar ao original."}
    return {"ok": True, "mensagem": "Tudo voltou a como estava antes." if b else "Nada para restaurar."}
