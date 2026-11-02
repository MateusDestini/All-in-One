"""Descobre por que o PC reiniciou ou desligou sozinho.

Cruza os eventos que o Windows grava em volta de cada inicialização:
  - Kernel-General 12: o sistema iniciou (marca cada boot)
  - Kernel-Power 41 / EventLog 6008: o desligamento anterior foi inesperado
  - WER-SystemErrorReporting 1001: tela azul, com o código do erro
  - User32 1074: um programa ou usuário pediu para reiniciar/desligar (ex.: Windows Update)
  - WHEA-Logger: erro de hardware pouco antes da queda
"""

import re
from datetime import datetime, timedelta

from . import windows
from .verificacoes import AVISO, CRITICO, Problema

_SCRIPT = r"""
$ev=Get-WinEvent -FilterHashtable @{LogName='System';Id=12,41,1001,1074,6008,17,18,19,47;StartTime=(Get-Date).AddDays(-%(dias)d)} -MaxEvents 3000 -ErrorAction SilentlyContinue
$r=foreach($e in $ev){
  $d=@{}; $i=0
  foreach($x in ([xml]$e.ToXml()).Event.EventData.Data){
    $i++
    if($x -is [string]){ $d["p$i"]=$x }
    else { $n=$x.Name; if(-not $n){ $n="p$i" }; $d[$n]=$x.'#text' } }
  [pscustomobject]@{Id=$e.Id;Prov=$e.ProviderName;Hora=$e.TimeCreated.ToString('s');Msg=(($e.Message -split "`n")[0]).Trim();Dados=$d} }
$r | ConvertTo-Json -Compress -Depth 4
"""

# Códigos de tela azul mais comuns: (nome, explicação simples, o que fazer)
TELAS_AZUIS = {
    0x0A: ("IRQL_NOT_LESS_OR_EQUAL", "um driver acessou memória de forma errada", "Atualize os drivers (chipset, rede, vídeo) e teste a RAM."),
    0x1A: ("MEMORY_MANAGEMENT", "problema na memória RAM ou em driver", "Rode o Diagnóstico de Memória do Windows; se falhar, troque o pente de RAM."),
    0x1E: ("KMODE_EXCEPTION_NOT_HANDLED", "um driver travou", "Atualize ou desinstale drivers instalados recentemente."),
    0x3B: ("SYSTEM_SERVICE_EXCEPTION", "erro em driver ou componente do sistema", "Atualize os drivers de vídeo e rode 'sfc /scannow' como administrador."),
    0x50: ("PAGE_FAULT_IN_NONPAGED_AREA", "memória RAM com defeito ou driver com problema", "Teste a RAM e atualize os drivers."),
    0x7A: ("KERNEL_DATA_INPAGE_ERROR", "o Windows não conseguiu ler dados do disco", "Verifique a saúde do disco (CrystalDiskInfo), cabos SATA e rode 'chkdsk /r'."),
    0x77: ("KERNEL_STACK_INPAGE_ERROR", "falha ao ler o disco", "Verifique a saúde do disco e os cabos."),
    0x7E: ("SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", "um driver travou", "Atualize os drivers, principalmente de vídeo."),
    0x9F: ("DRIVER_POWER_STATE_FAILURE", "um driver falhou ao entrar/sair da suspensão", "Atualize os drivers de rede, vídeo e chipset; desative a inicialização rápida."),
    0xA0: ("INTERNAL_POWER_ERROR", "falha no gerenciamento de energia", "Atualize a BIOS e os drivers de chipset."),
    0xC2: ("BAD_POOL_CALLER", "driver usando memória de forma errada", "Atualize ou remova drivers e antivírus de terceiros."),
    0x19: ("BAD_POOL_HEADER", "driver corrompeu a memória", "Atualize drivers e remova antivírus de terceiros."),
    0xD1: ("DRIVER_IRQL_NOT_LESS_OR_EQUAL", "driver com defeito (muito comum em placas de rede/Wi-Fi)", "Atualize o driver de rede e Wi-Fi pelo site do fabricante."),
    0xEF: ("CRITICAL_PROCESS_DIED", "um processo essencial do Windows parou", "Rode 'sfc /scannow' e 'DISM /Online /Cleanup-Image /RestoreHealth' como administrador."),
    0xF4: ("CRITICAL_OBJECT_TERMINATION", "processo essencial parou, geralmente por falha de disco", "Verifique a saúde do disco e os cabos."),
    0x101: ("CLOCK_WATCHDOG_TIMEOUT", "o processador parou de responder", "Desfaça overclock, verifique a temperatura e atualize a BIOS."),
    0x116: ("VIDEO_TDR_FAILURE", "a placa de vídeo travou", "Atualize o driver de vídeo, verifique a temperatura da placa e desfaça overclock."),
    0x117: ("VIDEO_TDR_TIMEOUT_DETECTED", "a placa de vídeo parou de responder", "Atualize o driver de vídeo e verifique a temperatura da placa."),
    0x124: ("WHEA_UNCORRECTABLE_ERROR", "erro de hardware (processador, memória, superaquecimento ou fonte)", "Limpe a poeira, verifique temperaturas, desfaça overclock e teste a fonte."),
    0x133: ("DPC_WATCHDOG_VIOLATION", "driver ou SSD demorou demais para responder", "Atualize o firmware do SSD e os drivers de armazenamento/chipset."),
    0x139: ("KERNEL_SECURITY_CHECK_FAILURE", "driver incompatível ou memória corrompida", "Atualize os drivers e teste a RAM."),
    0x154: ("UNEXPECTED_STORE_EXCEPTION", "falha no disco ou no armazenamento", "Verifique a saúde do disco e atualize o firmware do SSD."),
    0x1D3: ("", "erro do sistema", "Atualize o Windows e os drivers."),
}

# Quem costuma pedir reinício "sozinho" (evento 1074).
QUEM_REINICIA = [
    (("trustedinstaller", "mousocoreworker", "wuauclt", "usoclient", "musnotification", "updateorchestrator"),
     "Windows Update"),
    (("winlogon", "explorer.exe", "startmenuexperiencehost", "shutdown.exe"), "você (menu Iniciar ou comando)"),
]


def _hora(texto: str) -> datetime:
    return datetime.fromisoformat(texto[:19])


def _codigo(e: dict) -> int | None:
    if e["Id"] == 41:
        valor = str((e.get("Dados") or {}).get("BugcheckCode") or "0")
        try:
            return int(valor, 0) or None
        except ValueError:
            return None
    m = re.search(r"0x[0-9a-fA-F]{6,10}", e.get("Msg", "") + " " + " ".join(map(str, (e.get("Dados") or {}).values())))
    return int(m.group(0), 16) if m else None


def _quem(e: dict) -> str:
    processo = str((e.get("Dados") or {}).get("param1") or e.get("Msg", "")).lower()
    for nomes, rotulo in QUEM_REINICIA:
        if any(n in processo for n in nomes):
            return rotulo
    caminho = str((e.get("Dados") or {}).get("param1") or "")
    return caminho.split("\\")[-1].split(" ")[0] or "um programa"


def analisar(eventos: list[dict]) -> list[dict]:
    """Uma entrada por inicialização que veio depois de um desligamento inesperado ou pedido por programa."""
    for e in eventos:
        e["t"] = _hora(e.get("Hora", "1970-01-01T00:00:00"))
        e["prov"] = str(e.get("Prov") or "").lower()
    eventos.sort(key=lambda e: e["t"])
    boots = [e["t"] for e in eventos if e["Id"] == 12 and e["prov"] == "microsoft-windows-kernel-general"]
    resultado = []
    anterior = datetime.min
    for i, boot in enumerate(boots):
        limite = min(boot + timedelta(minutes=20), boots[i + 1] if i + 1 < len(boots) else datetime.max)
        depois = [e for e in eventos if boot <= e["t"] < limite]
        antes = [e for e in eventos if anterior < e["t"] < boot]
        anterior = boot
        inesperado = [e for e in depois if (e["Id"] == 41 and e["prov"] == "microsoft-windows-kernel-power")
                      or (e["Id"] == 6008 and e["prov"] == "eventlog")]
        tela_azul = [e for e in depois if e["Id"] == 1001 and "errorreporting" in e["prov"]]
        pedido = [e for e in antes if e["Id"] == 1074 and e["prov"] == "user32"]

        if inesperado or tela_azul:
            codigo = next((c for c in map(_codigo, tela_azul + inesperado) if c), None)
            k41 = next((e for e in inesperado if e["Id"] == 41), None)
            botao = k41 and str((k41.get("Dados") or {}).get("PowerButtonTimestamp") or "0") not in ("0", "")
            ultimo = antes[-1]["t"] if antes else None
            whea = [e for e in antes if "whea" in e["prov"] and ultimo and e["t"] >= ultimo - timedelta(minutes=30)]
            if codigo:
                base = codigo & 0xFFFF if codigo & 0x10000000 else codigo
                nome, explicacao, fazer = TELAS_AZUIS.get(base, ("", "erro do sistema",
                                                                 "Pesquise o código do erro e atualize drivers e Windows."))
                causa = f"Tela azul {codigo:#x}{' ' + nome if nome else ''}: {explicacao}"
                tipo = "tela_azul"
            elif whea:
                causa, tipo = "Erro de hardware registrado logo antes de desligar", "hardware"
                fazer = "Verifique temperatura, poeira, fonte e desfaça overclocks. Teste a RAM."
            elif botao:
                causa, tipo = "Botão de energia segurado (o PC estava travado?)", "botao"
                fazer = "Se o PC travou antes, veja os outros alertas de disco, memória e drivers."
            else:
                causa, tipo = ("Desligou de repente sem deixar registro: queda de energia, fonte, "
                               "superaquecimento ou travamento total"), "sem_registro"
                fazer = ("Verifique tomada/estabilizador e a fonte. Limpe a poeira e confira as temperaturas. "
                         "Se for notebook, teste a bateria.")
            resultado.append({"boot": boot, "tipo": tipo, "causa": causa, "fazer": fazer, "inesperado": True})
        elif pedido:
            e = pedido[-1]
            quem = _quem(e)
            if quem.startswith("você"):
                continue
            motivo = str((e.get("Dados") or {}).get("param3") or "").strip()
            resultado.append({"boot": boot, "tipo": "pedido", "inesperado": False, "quem": quem,
                              "causa": f"Reiniciado por {quem}" + (f" ({motivo})" if motivo else ""),
                              "fazer": "Em Configurações > Windows Update > Opções avançadas, defina o "
                                       "'Horário ativo' para o Windows não reiniciar enquanto você usa o PC."
                              if quem == "Windows Update" else "Verifique as configurações desse programa."})
    return resultado


def coletar(dias: int = 7) -> list[dict]:
    return analisar(windows.powershell_json(_SCRIPT % {"dias": dias}))


def problemas(reinicios: list[dict], dias: int = 7) -> list[Problema]:
    saida = []
    inesperados = [r for r in reinicios if r["inesperado"]]
    if inesperados:
        ultimo = inesperados[-1]
        linhas = [f"{r['boot']:%d/%m %H:%M}: {r['causa']}" for r in reversed(inesperados[-5:])]
        n = len(inesperados)
        saida.append(Problema(
            "reinicio_inesperado", CRITICO,
            "O PC reiniciou sozinho" + (f" {n} vezes" if n > 1 else "") + f" nos últimos {dias} dias",
            "\n".join(linhas),
            ultimo["fazer"] + (" Veja o motivo de cada reinício em Histórico." if n > 1 else ""),
            assinatura=ultimo["boot"].isoformat() + ultimo["tipo"], renotificar_min=0))
    pedidos = [r for r in reinicios if not r["inesperado"]]
    if pedidos:
        ultimo = pedidos[-1]
        saida.append(Problema(
            "reinicio_programado", AVISO, f"{ultimo['quem'][0].upper()}{ultimo['quem'][1:]} reiniciou o PC",
            "\n".join(f"{r['boot']:%d/%m %H:%M}: {r['causa']}" for r in reversed(pedidos[-3:])),
            ultimo["fazer"], assinatura=ultimo["boot"].isoformat(), renotificar_min=0))
    return saida
