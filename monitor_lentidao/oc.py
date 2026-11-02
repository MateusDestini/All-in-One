"""Abas NVIDIA, AMD e Processador: overclock, perfis e aplicação automática do perfil quando o Windows inicia.

- Os ajustes passam pelo driver oficial (NVML na NVIDIA, ADL na AMD) e ficam dentro da faixa que ele aceita.
- O processador usa o driver PawnIO (ver `oc_cpu`); placa de vídeo e processador têm cinco perfis cada.
- Um perfil de cada pode ser aplicado sozinho a cada logon: uma tarefa agendada do Windows abre o monitor
  como administrador só para aplicar os perfis (MonitorLentidao.exe --aplicar-perfil-gpu) e sair.
- Proteção: antes de aplicar, o monitor grava um marcador. Se o PC desligar de forma inesperada, o driver de
  vídeo reiniciar ou aparecer erro de hardware (WHEA) nos 3 minutos seguintes, a aplicação automática daquele
  perfil é desligada, o hardware volta ao padrão (quando der) e o monitor avisa na próxima vez.
"""

import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config, gpu, oc_amd, oc_cpu, oc_nvidia, saude, windows

log = logging.getLogger(__name__)

NOME_TAREFA = r"Destini\Aplicar perfil da placa de video"
# preferências de cada grupo: (perfis, perfil aplicado ao iniciar, aviso de suspensão, marcador)
GRUPOS = {"gpu": ("oc_perfis", "oc_perfil_inicio", "oc_aviso_inicio", "oc_aplicando.json"),
          "cpu": ("cpu_perfis", "cpu_perfil_inicio", "cpu_aviso_inicio", "cpu_aplicando.json")}
NOME_GRUPO = {"gpu": "placa de vídeo", "cpu": "processador"}


def grupo(fabricante: str) -> str:
    return "cpu" if fabricante == "cpu" else "gpu"


def _pref(g: str, i: int):
    return config.preferencias().get(GRUPOS[g][i])
SLOTS = ("1", "2", "3", "4", "5")
JANELA_SEGURANCA_S = 180
ESPERA_DRIVER_S = 90

_placas: list | None = None
_presenca: dict | None = None


# ------------------------------------------------------------------ placas
def presenca() -> dict:
    """Quais abas mostrar: NVIDIA (qualquer placa NVIDIA) e AMD (Radeon com memória de vídeo própria)."""
    global _presenca
    if _presenca is None:
        nvidia = amd = False
        try:
            for a in gpu.adaptadores_dxgi(com_nivel=False):
                if a["ven"] == "10DE":
                    nvidia = True
                elif a["ven"] == "1002" and (a.get("vram") or 0) >= 1 << 30:  # ignora a Radeon integrada do Ryzen
                    amd = True
        except Exception:
            log.exception("falha ao detectar as placas de vídeo")
        nvidia = nvidia or gpu.nvml().disponivel
        _presenca = {"nvidia": nvidia, "amd": amd}
    return dict(_presenca)


def placas(fabricante: str | None = None) -> list:
    global _placas
    if fabricante == "cpu":
        return oc_cpu.placas()
    if _placas is None:
        lista = []
        try:
            lista += oc_nvidia.placas()
        except Exception:
            log.exception("falha ao abrir a NVML")
        try:
            lista += oc_amd.placas()
        except Exception:
            log.exception("falha ao abrir a ADL")
        _placas = lista
    return [p for p in _placas if fabricante in (None, p.fabricante)]


def _esquecer_placas() -> None:
    global _placas
    _placas = None


def _placa(fabricante: str, indice: int = 0):
    lista = placas(fabricante)
    if not lista:
        return None
    return lista[min(max(0, int(indice)), len(lista) - 1)]


# ------------------------------------------------------------------ perfis
def perfis(g: str = "gpu") -> dict:
    return dict(_pref(g, 0) or {})


def _resumo(fabricante: str, valores: dict) -> str:
    curto = {"energia": "energia", "nucleo": "núcleo", "memoria": "memória", "temperatura": "alvo", "ventoinha": "fan",
             "teto": "teto", "clock_max": "núcleo", "clock_min": "mín.", "tensao": "tensão", "tensao_max": "tensão máx.",
             "tdc": "TDC", "temp_alvo": "alvo", "ventoinha_min": "fan mín.", "ventoinha_max": "fan máx.",
             "temp_max": "temp. máx.", "zero_rpm": "zero RPM", "ppt": "PPT", "edc": "EDC", "temp": "temp.",
             "boost": "boost", "co": "CO", "pl1": "PL1", "pl2": "PL2", "tau": "Tau", "iccmax": "IccMax",
             "uv": "undervolt"}
    unid = {"energia": "W" if fabricante == "nvidia" else "%", "nucleo": " MHz", "memoria": " MHz", "temperatura": " °C",
            "ventoinha": "%", "teto": " MHz", "clock_max": " MHz", "clock_min": " MHz", "tensao": " mV",
            "tensao_max": " mV", "tdc": "%", "temp_alvo": " °C", "ventoinha_min": "%", "ventoinha_max": " RPM",
            "temp_max": " °C", "ppt": " W", "edc": " A", "temp": " °C", "boost": " MHz", "pl1": " W",
            "pl2": " W", "tau": " s", "iccmax": " A", "uv": " mV"}
    if fabricante == "cpu":
        unid["tdc"] = " A"
    partes = []
    cos = [v for k, v in sorted(valores.items(), key=lambda kv: int(kv[0][3:]) if kv[0][3:].isdigit() else 0)
           if k.startswith("co_") and k[3:].isdigit()]
    if cos:
        partes.append(f"CO {cos[0]:+d}" if len(set(cos)) == 1 else f"CO {max(cos):+d} a {min(cos):+d} por núcleo")
    for k, v in valores.items():
        if k.startswith("co_") and k[3:].isdigit():
            continue
        if k in ("ventoinha", "teto") and not v:
            continue
        sinal = "+" if k in ("nucleo", "memoria", "boost", "co", "uv") and v > 0 else ""
        partes.append(f"{curto.get(k, k)} {sinal}{v}{unid.get(k, '')}")
    return " · ".join(partes)


def salvar_perfil(slot: str, fabricante: str, indice: int, valores: dict, nome: str = "") -> dict:
    if slot not in SLOTS:
        return {"ok": False, "mensagem": "Perfil inválido."}
    p = _placa(fabricante, indice)
    if not p:
        return {"ok": False, "mensagem": "Placa não encontrada."}
    validos = {c["id"] for c in p.controles()}
    valores = {k: int(round(float(v))) for k, v in valores.items() if k in validos}
    g = grupo(fabricante)
    todos = perfis(g)
    todos[slot] = {"nome": (nome or f"Perfil {slot}").strip()[:40], "fabricante": fabricante, "placa": p.nome,
                   "valores": valores, "resumo": _resumo(fabricante, valores), "quando": datetime.now().strftime("%d/%m %H:%M")}
    config.definir_preferencia(GRUPOS[g][0], todos)
    return {"ok": True, "mensagem": f"Salvo no perfil {slot}."}


def apagar_perfil(slot: str, fabricante: str = "gpu") -> dict:
    g = grupo(fabricante)
    todos = perfis(g)
    todos.pop(slot, None)
    config.definir_preferencia(GRUPOS[g][0], todos)
    if _pref(g, 1) == slot:
        return definir_inicio(None, fabricante)
    return {"ok": True, "mensagem": f"Perfil {slot} apagado."}


def aplicar_perfil(slot: str, fabricante: str = "gpu") -> dict:
    g = grupo(fabricante)
    perfil = perfis(g).get(slot)
    if not perfil:
        return {"ok": False, "mensagem": "Esse perfil está vazio."}
    p = _placa(perfil["fabricante"])
    if not p:
        return {"ok": False, "mensagem": f"O {NOME_GRUPO[g]} desse perfil não foi encontrado." if g == "cpu"
                else "A placa desse perfil não foi encontrada."}
    if not _mesmo_processador(perfil, p):
        return {"ok": False, "mensagem": f"Esse perfil foi feito para outro processador ({perfil.get('placa')}). "
                                         "Os valores de tensão não servem para este; rode o ajuste de novo."}
    return _resultado(p.aplicar(perfil["valores"], so_mudancas=False), f"Perfil {slot} aplicado.", g)


def _mesmo_processador(perfil: dict, p) -> bool:
    """Perfis do processador guardam tensão por núcleo: só valem para o mesmo modelo."""
    return perfil.get("fabricante") != "cpu" or not perfil.get("placa") or perfil["placa"] == p.nome


def _resultado(itens: list[dict], texto_ok: str, g: str = "gpu") -> dict:
    falhas = [i for i in itens if not i["ok"]]
    if not windows.eh_admin() and falhas:
        return {"ok": False, "precisa_admin": True, "itens": itens,
                "mensagem": f"Para mudar o {NOME_GRUPO[g]}, abra o monitor como administrador." if g == "cpu" else
                "Para mudar a placa de vídeo, abra o monitor como administrador."}
    if falhas:
        return {"ok": False, "itens": itens, "mensagem": "Alguns ajustes não foram aceitos: " +
                "; ".join(f"{i['id']}: {i['mensagem']}" for i in falhas)}
    return {"ok": True, "itens": itens, "mensagem": texto_ok if itens else "Nada mudou."}


# ------------------------------------------------------------------ painel
def estado(fabricante: str) -> dict:
    lista = placas(fabricante)
    g = grupo(fabricante)
    saida = {"admin": windows.eh_admin(), "placas": [], "perfis": perfis(g), "perfil_inicio": _pref(g, 1),
             "tarefa": tarefa_existe(), "aviso_inicio": _pref(g, 2), "erros": None}
    if g == "cpu":
        from . import pawnio, sensores
        saida.update(motivo=oc_cpu.motivo(), pawnio=pawnio.disponivel(), ajuste=oc_cpu.estado_ajuste(),
                     teste=oc_cpu.estado_teste(), nome_cpu=sensores.nome_cpu(), marca=oc_cpu.presenca()["marca"],
                     nucleos=oc_cpu.nucleos_fisicos())
    for i, p in enumerate(lista):
        try:
            item = {"indice": i, "nome": p.nome, "fabricante": p.fabricante, "controles": p.controles(),
                    "leituras": p.leituras()}
            if g == "cpu":
                item["info"] = p.info()
            saida["placas"].append(item)
        except Exception as e:
            log.exception("falha ao ler os ajustes da placa")
            saida["placas"].append({"indice": i, "nome": p.nome, "fabricante": p.fabricante, "controles": [],
                                    "leituras": [], "erro": str(e)})
    if fabricante == "amd" and not lista:
        saida["sem_driver"] = ("Não consegui falar com o driver da AMD (Adrenalin). Atualize o driver pelo site da AMD "
                               "ou pela página Drivers.")
    return saida


def limpar_aviso(fabricante: str = "gpu") -> dict:
    config.definir_preferencia(GRUPOS[grupo(fabricante)][2], None)
    return {"ok": True, "mensagem": "Aviso apagado."}


def erros_recentes() -> dict | None:
    return saude.erros_hardware()


def aplicar(fabricante: str, indice: int, valores: dict) -> dict:
    p = _placa(fabricante, indice)
    if not p:
        return {"ok": False, "mensagem": "Placa não encontrada." if fabricante != "cpu" else "Processador indisponível."}
    return _resultado(p.aplicar({k: v for k, v in (valores or {}).items()}), "Ajustes aplicados. Rode o teste de estabilidade.",
                      grupo(fabricante))


def padrao(fabricante: str, indice: int) -> dict:
    p = _placa(fabricante, indice)
    if not p:
        return {"ok": False, "mensagem": "Placa não encontrada." if fabricante != "cpu" else "Processador indisponível."}
    return _resultado(p.padrao(), "Processador de volta ao padrão de fábrica (reiniciar volta ao que está na BIOS)."
                      if fabricante == "cpu" else
                      "Placa de volta ao padrão de fábrica.", grupo(fabricante))


# ------------------------------------------------------------------ tarefa agendada (aplicar ao iniciar)
def _comando_tarefa() -> tuple[str, str, str]:
    if getattr(sys, "frozen", False):
        exe = sys.executable
        return exe, "--aplicar-perfil-gpu", str(Path(exe).parent)
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return str(pyw if pyw.exists() else py), "-m monitor_lentidao --aplicar-perfil-gpu", str(Path(__file__).resolve().parent.parent)


def _xml_tarefa() -> str:
    from xml.sax.saxutils import escape
    usuario = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    exe, args, pasta = _comando_tarefa()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Aplica o perfil de overclock escolhido no Destini · Monitor de Lentidão quando você entra no Windows.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(usuario)}</UserId><Delay>PT30S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(usuario)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>
    <Priority>7</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(pasta)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _executar_admin(exe: str, args: str) -> int | None:
    """Roda um programa como administrador e espera. Sem admin, o Windows pede confirmação (None = recusada)."""
    if windows.eh_admin():
        try:
            return subprocess.run([exe] + _dividir(args), capture_output=True, timeout=60,
                                  creationflags=windows.CREATE_NO_WINDOW).returncode
        except (OSError, subprocess.TimeoutExpired):
            return None
    from ctypes import wintypes as w

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", w.HWND), ("lpVerb", w.LPCWSTR),
                    ("lpFile", w.LPCWSTR), ("lpParameters", w.LPCWSTR), ("lpDirectory", w.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", w.HINSTANCE), ("lpIDList", ctypes.c_void_p), ("lpClass", w.LPCWSTR),
                    ("hkeyClass", w.HKEY), ("dwHotKey", w.DWORD), ("hIcon", w.HANDLE), ("hProcess", w.HANDLE)]
    info = SHELLEXECUTEINFOW(cbSize=ctypes.sizeof(SHELLEXECUTEINFOW), fMask=0x40, lpVerb="runas", lpFile=exe,
                             lpParameters=args, nShow=0)  # SEE_MASK_NOCLOSEPROCESS, janela escondida
    shell32 = ctypes.WinDLL("shell32")
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    if not shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        return None
    k = ctypes.WinDLL("kernel32")
    k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
    k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
    k.CloseHandle.argtypes = [w.HANDLE]
    k.WaitForSingleObject(info.hProcess, 60_000)
    codigo = w.DWORD()
    k.GetExitCodeProcess(info.hProcess, ctypes.byref(codigo))
    k.CloseHandle(info.hProcess)
    return codigo.value


def _dividir(args: str) -> list[str]:
    import shlex
    return shlex.split(args, posix=False)


def tarefa_existe() -> bool:
    if not windows.EH_WINDOWS:
        return False
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", NOME_TAREFA], capture_output=True, timeout=15,
                           creationflags=windows.CREATE_NO_WINDOW)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _criar_tarefa() -> bool:
    arquivo = config.pasta_dados() / "tarefa-perfil-gpu.xml"
    arquivo.write_text(_xml_tarefa(), encoding="utf-16")
    r = _executar_admin("schtasks.exe", f'/Create /F /TN "{NOME_TAREFA}" /XML "{arquivo}"')
    return r == 0


def _apagar_tarefa() -> bool:
    r = _executar_admin("schtasks.exe", f'/Delete /F /TN "{NOME_TAREFA}"')
    return r == 0


def definir_inicio(slot: str | None, fabricante: str = "gpu") -> dict:
    """Escolhe o perfil aplicado a cada logon (None desliga). Criar/apagar a tarefa pede confirmação do Windows."""
    g = grupo(fabricante)
    if slot and slot not in perfis(g):
        return {"ok": False, "mensagem": "Salve o perfil antes de escolher."}
    if slot:
        if not tarefa_existe() and not _criar_tarefa():
            return {"ok": False, "mensagem": "O Windows não criou a tarefa de inicialização (a confirmação foi recusada?)."}
        config.definir_preferencia(GRUPOS[g][1], slot)
        config.definir_preferencia(GRUPOS[g][2], None)
        return {"ok": True, "mensagem": f"O perfil {slot} será aplicado sempre que você entrar no Windows."}
    config.definir_preferencia(GRUPOS[g][1], None)
    if any(_pref(outro, 1) for outro in GRUPOS):  # o outro grupo ainda usa a tarefa
        return {"ok": True, "mensagem": "Aplicação automática desligada."}
    if tarefa_existe() and not _apagar_tarefa():
        return {"ok": True, "mensagem": "Aplicação automática desligada. A tarefa do Windows continua lá, mas não faz nada."}
    return {"ok": True, "mensagem": "Aplicação automática desligada."}


# ------------------------------------------------------------------ execução no logon (processo à parte, como admin)
_SCRIPT_DESDE = r"""
$t=[datetime]::Parse('%(desde)s')
$q=@(Get-WinEvent -FilterHashtable @{LogName='System';Id=41,6008,1001;StartTime=$t} -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match 'Kernel-Power|EventLog|WER-SystemErrorReporting' }).Count
$g=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='nvlddmkm','Display','amdkmdag';StartTime=$t} -ErrorAction SilentlyContinue | Where-Object { ($_.ProviderName -eq 'nvlddmkm' -and ($_.Id -eq 13 -or $_.Id -eq 153)) -or ($_.ProviderName -eq 'Display' -and $_.Id -eq 4101) -or $_.ProviderName -eq 'amdkmdag' }).Count
[pscustomobject]@{Quedas=$q;Driver=$g} | ConvertTo-Json -Compress
"""


def _eventos_desde(instante: float) -> dict:
    desde = datetime.fromtimestamp(instante).strftime("%Y-%m-%dT%H:%M:%S")
    r = windows.powershell_json(_SCRIPT_DESDE % {"desde": desde}, timeout=60)
    return {"quedas": int(r[0].get("Quedas") or 0), "driver": int(r[0].get("Driver") or 0)} if r else {"quedas": 0, "driver": 0}


def _arquivo(nome: str) -> Path:
    return config.pasta_dados() / nome


def _gravar_resultado(ok: bool, texto: str, suspensos: list[str] | None = None) -> None:
    _arquivo("oc_resultado.json").write_text(json.dumps({"ok": ok, "texto": texto, "suspensos": suspensos or [],
                                                         "quando": time.time()}, ensure_ascii=False), encoding="utf-8")


def _suspender(g: str, texto: str) -> None:
    config.definir_preferencia(GRUPOS[g][1], None)
    config.definir_preferencia(GRUPOS[g][2], texto)
    if not any(_pref(outro, 1) for outro in GRUPOS):
        _apagar_tarefa()
    log.warning("aplicação automática do perfil (%s) desligada: %s", g, texto)


def _placa_do_perfil(perfil: dict, espera_s: float):
    """O driver de vídeo (ou o PawnIO) pode ainda estar subindo logo depois do logon."""
    fim = time.time() + espera_s
    while True:
        if perfil["fabricante"] == "cpu":
            oc_cpu.esquecer()
        else:
            _esquecer_placas()
        alvo = _placa(perfil["fabricante"])
        if (alvo and alvo.controles()) or time.time() >= fim:
            return alvo
        time.sleep(5)


def aplicar_no_inicio() -> None:
    """Chamado pela tarefa agendada no logon (já como administrador): aplica o perfil da placa e o do processador."""
    textos, suspensos, ok_geral, aplicados = [], [], True, []
    for g in GRUPOS:
        slot = _pref(g, 1)
        marcador = _arquivo(GRUPOS[g][3])
        if not slot:
            marcador.unlink(missing_ok=True)
            continue
        perfil = perfis(g).get(slot)
        if not perfil:
            textos.append(f"O perfil {slot} do {NOME_GRUPO[g]} não existe mais; nada foi aplicado.")
            ok_geral = False
            continue
        if marcador.exists():
            try:
                anterior = json.loads(marcador.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                anterior = {"quando": time.time() - 3600}
            marcador.unlink(missing_ok=True)
            if _eventos_desde(float(anterior.get("quando", 0)))["quedas"]:
                t = (f"O PC desligou de repente logo depois de aplicar o perfil {anterior.get('perfil', slot)} do "
                     f"{NOME_GRUPO[g]}. Por segurança, não apliquei de novo e desliguei a aplicação automática. Revise o "
                     "perfil e teste a estabilidade antes de ligar de novo.")
                _suspender(g, t)
                textos.append(t)
                suspensos.append(g)
                ok_geral = False
                continue
        alvo = _placa_do_perfil(perfil, ESPERA_DRIVER_S if g == "gpu" else 45)
        if not alvo:
            textos.append(f"Não achei o {NOME_GRUPO[g]} do perfil {slot}; nada foi aplicado." if g == "cpu"
                          else f"Não achei a placa do perfil {slot}; nada foi aplicado.")
            ok_geral = False
            continue
        if not _mesmo_processador(perfil, alvo):
            t = (f"O perfil {slot} do processador foi feito para outro modelo ({perfil.get('placa')}). Não apliquei e "
                 "desliguei a aplicação automática.")
            _suspender(g, t)
            textos.append(t)
            suspensos.append(g)
            ok_geral = False
            continue
        inicio = time.time()
        marcador.write_text(json.dumps({"quando": inicio, "perfil": slot}), encoding="utf-8")
        itens = alvo.aplicar(perfil["valores"], so_mudancas=False)
        falhas = [i for i in itens if not i["ok"]]
        if falhas:
            ok_geral = False
            textos.append(f"Perfil {slot} do {NOME_GRUPO[g]} aplicado em parte. Não aceitos: " +
                          "; ".join(f"{i['id']} ({i['mensagem']})" for i in falhas))
        else:
            textos.append(f"Perfil {slot} ({perfil.get('nome')}) aplicado n{'o' if g == 'cpu' else 'a'} {alvo.nome}.")
        log.info("perfil %s (%s) aplicado no logon: %s", slot, g, itens)
        aplicados.append((g, slot, alvo, inicio, marcador))
    _gravar_resultado(ok_geral, " ".join(textos), suspensos)
    if not aplicados:
        return
    inicio = min(a[3] for a in aplicados)
    while time.time() - inicio < JANELA_SEGURANCA_S and aplicados:
        time.sleep(15)
        ev = _eventos_desde(inicio)
        whea = oc_cpu.whea_desde(inicio) if any(a[0] == "cpu" for a in aplicados) else 0
        for a in list(aplicados):
            g, slot, alvo = a[0], a[1], a[2]
            if (g == "gpu" and ev["driver"]) or (g == "cpu" and whea):
                try:
                    alvo.padrao()
                except Exception:
                    log.exception("falha ao voltar ao padrão")
                t = (f"O driver de vídeo reiniciou logo depois de aplicar o perfil {slot}. Voltei a placa ao padrão e "
                     "desliguei a aplicação automática. Diminua o overclock e teste a estabilidade." if g == "gpu" else
                     f"Apareceram erros de hardware (WHEA) logo depois de aplicar o perfil {slot} do processador. Voltei "
                     "o processador ao padrão e desliguei a aplicação automática. Rode o ajuste automático de novo.")
                _suspender(g, t)
                suspensos.append(g)
                _gravar_resultado(False, t, suspensos)
                a[4].unlink(missing_ok=True)
                aplicados.remove(a)
    for a in aplicados:
        a[4].unlink(missing_ok=True)


def resultado_inicio() -> dict | None:
    """Lido pelo monitor da bandeja: o que aconteceu na aplicação dos perfis (uma vez só)."""
    arq = _arquivo("oc_resultado.json")
    if not arq.exists():
        return None
    try:
        r = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - float(r.get("quando", 0)) > 15:  # o processo do logon ainda pode reescrever logo em seguida
        arq.unlink(missing_ok=True)
        suspensos = r.get("suspensos") or (["gpu"] if r.get("suspenso") else [])
        for g in suspensos:
            # O monitor guarda as preferências em memória: sem isto, a próxima gravação religaria o perfil.
            config.definir_preferencia(GRUPOS[g][1], None)
            config.definir_preferencia(GRUPOS[g][2], r.get("texto"))
        return r
    return None
