"""Maker: portas COM e drivers seriais (Arduino, ESP32, CH340, CP210x, FTDI, Prolific...).

- Portas COM: lista placas e conversores conectados e os "fantasmas" (aparelhos que já foram plugados e ficaram
  registrados), remove os fantasmas escolhidos e libera os números COM presos (ComDB), com backup para desfazer.
- Drivers seriais: diagnostica cada placa conectada pelo código de problema do Gerenciador de Dispositivos, reinstala o
  aparelho, lista as versões do driver guardadas no Windows e remove uma versão (com backup e "Restaurar removidos").
- Arduino IDE: versão instalada, instalar/atualizar pelo winget, programas que prendem a porta e limpeza do cache.

Tudo pelo pnputil e pelo registro, que já vêm no Windows. Ler não muda nada; o resto pede confirmação no painel e
precisa do monitor aberto como administrador. Ideias do ResetExplode (UsbCleaner.cs e MakerDrivers.cs, licença MIT);
o código é próprio. Os drivers que o ResetExplode embute não vêm junto: o painel aponta para a página oficial de cada chip.
"""

import base64
import json
import logging
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import psutil

from . import apps, config, windows

log = logging.getLogger(__name__)

# (nome, VIDs, .inf originais, trecho do fornecedor, página oficial)
CHIPS = [
    ("CH340/CH341", ("1A86",), ("ch341ser.inf", "ch343ser.inf", "ch341s64.inf"), ("wch",), "https://www.wch-ic.com/downloads/CH341SER_EXE.html"),
    ("CP210x", ("10C4",), ("silabser.inf", "slabvcp.inf", "silabenm.inf"), ("silicon lab",), "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers"),
    ("FTDI", ("0403",), ("ftdibus.inf", "ftdiport.inf"), ("ftdi",), "https://ftdichip.com/drivers/vcp-drivers/"),
    ("Prolific PL2303", ("067B",), ("ser2pl.inf",), ("prolific",), "https://www.prolific.com.tw/"),
    ("Arduino", ("2341", "2A03"), ("arduino.inf", "genuino.inf", "arduino-org.inf"), ("arduino",), "https://www.arduino.cc/en/software"),
]
FABRICANTES = {"2341": "Arduino", "2A03": "Arduino", "1A86": "CH340/CH341", "0403": "FTDI", "10C4": "CP210x", "303A": "Espressif (ESP32)",
               "239A": "Adafruit", "16C0": "Teensy", "1B4F": "SparkFun", "2E8A": "Raspberry Pi Pico", "067B": "Prolific PL2303",
               "0483": "STMicro", "1366": "SEGGER"}
CLASSES = ("Ports", "Modem", "libusbK devices", "libusb-win32 devices", "WinUSB")
PRENDEM_PORTA = ("serial-monitor", "serial-discovery", "arduino-cli", "putty", "CoolTerm", "ttermpro", "Cura", "UltiMaker-Cura",
                 "PrusaSlicer", "Repetier-Host", "pronterface", "MobaXterm", "RealTerm", "Termite")
WINGET_IDE = "ArduinoSA.IDE.stable"
_ARBITRO = r"SYSTEM\CurrentControlSet\Control\COM Name Arbiter"


def chip_por_vid(vid: str | None):
    vid = (vid or "").upper()
    return next((c for c in CHIPS if vid in c[1]), None)


# ------------------------------------------------------------------ leitura dos aparelhos
_PS_APARELHOS = r"""
$vids = '%s'
$classes = @(%s)
$l = @()
foreach ($d in Get-PnpDevice -ErrorAction SilentlyContinue) {
  $id = [string]$d.InstanceId; $cls = [string]$d.Class
  $vid = ''; $pid_ = ''
  if ($id -match 'VID_([0-9A-Fa-f]{4})') { $vid = $Matches[1].ToUpper() }
  if ($id -match 'PID_([0-9A-Fa-f]{4})') { $pid_ = $Matches[1].ToUpper() }
  $conhecido = $vid -and ($vid -match "^($vids)$")
  if (-not ($classes -contains $cls) -and -not $conhecido) { continue }
  if ($id -like 'USB\ROOT_HUB*' -or $id -like 'PCI\*') { continue }
  $porta = ''
  try { $porta = [string](Get-ItemProperty -LiteralPath "HKLM:\SYSTEM\CurrentControlSet\Enum\$id\Device Parameters" -Name PortName -ErrorAction Stop).PortName } catch {}
  $p = @{}
  try { Get-PnpDeviceProperty -InstanceId $id -KeyName DEVPKEY_Device_DriverVersion,DEVPKEY_Device_DriverProvider,DEVPKEY_Device_DriverInfPath,DEVPKEY_Device_LastArrivalDate -ErrorAction Stop |
        ForEach-Object { $p[$_.KeyName] = $_.Data } } catch {}
  $chegada = ''; if ($p['DEVPKEY_Device_LastArrivalDate']) { $chegada = ([datetime]$p['DEVPKEY_Device_LastArrivalDate']).ToString('dd/MM/yyyy') }
  $l += @{ id = $id; nome = [string]$(if ($d.FriendlyName) { $d.FriendlyName } else { $id }); classe = $cls; presente = [bool]$d.Present
           problema = [int]$d.ConfigManagerErrorCode; porta = $porta; vid = $vid; pid = $pid_
           versao = [string]$p['DEVPKEY_Device_DriverVersion']; provedor = [string]$p['DEVPKEY_Device_DriverProvider']
           inf = [string]$p['DEVPKEY_Device_DriverInfPath']; chegada = $chegada }
}
ConvertTo-Json -InputObject @($l) -Compress -Depth 3
"""


def _porta_do_nome(nome: str) -> str:
    m = re.search(r"\((COM\d+)\)", nome or "")
    return m.group(1) if m else ""


def _num_com(texto) -> int | None:
    m = re.fullmatch(r"COM(\d+)", str(texto or "").strip(), re.I)
    return int(m.group(1)) if m else None


def aparelhos() -> list[dict]:
    script = _PS_APARELHOS % ("|".join(FABRICANTES), ",".join(f"'{c}'" for c in CLASSES))
    bruto = windows.powershell_json(script, timeout=120)
    if len(bruto) == 1 and isinstance(bruto[0], list):
        bruto = bruto[0]
    saida = []
    for d in bruto:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        presente = bool(d.get("presente"))
        problema = int(d.get("problema") or 0)
        vid = (d.get("vid") or "").upper()
        item = {"id": d["id"], "nome": d.get("nome") or d["id"], "classe": d.get("classe") or "", "presente": presente,
                "problema": problema if presente and problema not in (0, 45) else None,
                "porta": d.get("porta") or _porta_do_nome(d.get("nome") or ""), "vid": vid, "pid": (d.get("pid") or "").upper(),
                "fabricante": FABRICANTES.get(vid), "versao": d.get("versao") or "", "provedor": d.get("provedor") or "",
                "inf": d.get("inf") or "", "chegada": d.get("chegada") or ""}
        # Pré-marca só fantasmas de porta COM ou de placas conhecidas; a serial da placa-mãe (ACPI) nunca.
        item["sugerido"] = (not presente and not item["id"].upper().startswith("ACPI\\")
                            and (item["classe"] == "Ports" or item["fabricante"] is not None))
        saida.append(item)
    saida.sort(key=lambda a: (not a["presente"], a["fabricante"] is None, not a["porta"], a["nome"].lower()))
    return saida


# ------------------------------------------------------------------ números COM (ComDB)
def _ler_comdb() -> bytes:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ARBITRO) as k:
            v = winreg.QueryValueEx(k, "ComDB")[0]
            return bytes(v) if v else b""
    except OSError:
        return b""


def _gravar_comdb(dados: bytes) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ARBITRO, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "ComDB", 0, winreg.REG_BINARY, dados)


def _portas_publicadas() -> set[int]:
    """Portas que o Windows publicou agora em HARDWARE\\DEVICEMAP\\SERIALCOMM."""
    import winreg
    portas = set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM") as k:
            i = 0
            while True:
                try:
                    _, valor, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                n = _num_com(valor)
                if n:
                    portas.add(n)
                i += 1
    except OSError:
        pass
    return portas


def reservadas(comdb: bytes) -> list[int]:
    return [n for n in range(1, len(comdb) * 8 + 1) if comdb[(n - 1) // 8] & (1 << ((n - 1) % 8))]


def montar_comdb(em_uso: set[int], tamanho: int) -> bytes:
    db = bytearray(max(32, tamanho))
    for p in em_uso:
        if 1 <= p <= len(db) * 8:
            db[(p - 1) // 8] |= 1 << ((p - 1) % 8)
    return bytes(db)


def _em_uso(lista: list[dict]) -> set[int]:
    return _portas_publicadas() | {n for a in lista if a["presente"] for n in [_num_com(a["porta"])] if n}


def _arquivo() -> Path:
    return config.pasta_dados() / "maker.json"


def _dados() -> dict:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _salvar(d: dict) -> None:
    _arquivo().write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def portas(progresso=lambda *a: None) -> dict:
    progresso(0.2, "Procurando placas, conversores e portas COM...")
    lista = aparelhos()
    db = _ler_comdb() if windows.EH_WINDOWS else b""
    uso = sorted(_em_uso(lista)) if windows.EH_WINDOWS else []
    res = reservadas(db)
    return {"aparelhos": lista, "reservadas": res, "em_uso": uso, "presas": [n for n in res if n not in uso],
            "tem_backup_comdb": bool(_dados().get("comdb")), "admin": windows.eh_admin()}


def _exigir_admin(acao: str) -> None:
    if not windows.eh_admin():
        raise PermissionError(f"{acao} precisa do monitor aberto como administrador")


def _pnputil(*args: str, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(["pnputil.exe", *args], capture_output=True, timeout=timeout, creationflags=windows.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    saida = (p.stdout + p.stderr).decode("utf-8", "replace")
    return p.returncode, saida


def _ultima(saida: str) -> str:
    linhas = [l.strip() for l in (saida or "").splitlines() if l.strip()]
    return linhas[-1] if linhas else ""


def remover_fantasmas(ids: list[str], progresso=lambda *a: None) -> dict:
    """Remove só aparelhos que NÃO estão conectados (confere de novo antes). Plugar de novo recria o aparelho."""
    _exigir_admin("Remover aparelhos")
    atuais = {a["id"]: a for a in aparelhos()}
    removidos, falhas = 0, []
    for i, id_ in enumerate(ids):
        a = atuais.get(id_)
        progresso(0.1 + 0.85 * i / max(1, len(ids)), f"Removendo {a['nome'] if a else id_}...")
        if not a:
            falhas.append(f"{id_} (não encontrado)")
            continue
        if a["presente"]:
            falhas.append(f"{a['nome']} (está conectado)")
            continue
        codigo, saida = _pnputil("/remove-device", id_, timeout=60)
        if codigo == 0:
            removidos += 1
        else:
            falhas.append(f"{a['nome']} ({_ultima(saida) or 'código ' + str(codigo)})")
    msg = f"{removidos} aparelho(s) antigo(s) removido(s)."
    if falhas:
        msg += " Não removidos: " + "; ".join(falhas[:5]) + ("..." if len(falhas) > 5 else "")
    return {"ok": not falhas or removidos > 0, "removidos": removidos, "falhas": falhas, "mensagem": msg}


def liberar_numeros(progresso=lambda *a: None) -> dict:
    """Deixa reservados no ComDB só os números em uso agora; guarda o valor de antes para desfazer."""
    _exigir_admin("Liberar os números COM")
    antes = _ler_comdb()
    uso = _em_uso(aparelhos())
    novo = montar_comdb(uso, len(antes))
    liberados = [n for n in reservadas(antes) if n not in uso]
    if not liberados:
        return {"ok": True, "liberados": [], "mensagem": "Nenhum número COM preso: só os em uso estão reservados."}
    d = _dados()
    d["comdb"] = {"valor": base64.b64encode(antes).decode(), "quando": datetime.now().strftime("%d/%m/%Y %H:%M")}
    _salvar(d)
    _gravar_comdb(novo)
    mostrar = ", ".join(f"COM{n}" for n in liberados[:12]) + ("..." if len(liberados) > 12 else "")
    return {"ok": True, "liberados": liberados,
            "mensagem": f"{len(liberados)} número(s) liberado(s): {mostrar}. A próxima placa plugada pega o menor número livre."}


def desfazer_numeros(progresso=lambda *a: None) -> dict:
    _exigir_admin("Desfazer")
    d = _dados()
    b = d.get("comdb")
    if not b:
        return {"ok": False, "mensagem": "Não há backup dos números COM."}
    _gravar_comdb(base64.b64decode(b["valor"]))
    d.pop("comdb", None)
    _salvar(d)
    return {"ok": True, "mensagem": f"Números COM voltaram a como estavam em {b['quando']}."}


# ------------------------------------------------------------------ drivers seriais
def diagnosticar(a: dict) -> dict:
    """{ok, veredito, conselho} pelo código de problema do Gerenciador de Dispositivos."""
    chip = chip_por_vid(a.get("vid"))
    nome = chip[0] if chip else a.get("fabricante") or ""
    if nome == "FTDI" and a.get("pid") == "0000":
        return {"ok": False, "veredito": "Chip FTDI falsificado travado (PID 0000)",
                "conselho": "Um driver antigo da FTDI zerou o identificador de chips FTDI falsificados e o Windows não reconhece mais o conversor. "
                            "Dá para regravar o PID 6001 com o FT_Prog (gratuito, da FTDI) ou trocar por um conversor CH340/CP210x."}
    p = a.get("problema")
    porta = a.get("porta")
    if p is None:
        return {"ok": True, "veredito": f"Funcionando na {porta}" if porta else "Funcionando", "conselho": ""}
    if p == 28:
        return {"ok": False, "veredito": "Sem driver (código 28)",
                "conselho": "Instale o driver oficial do chip (botão Página oficial) ou procure no Windows Update, na página Drivers."}
    if p == 10:
        dicas = {
            "CH340/CH341": "É comum em clones de CH340 com a versão mais nova do driver. Tente Reinstalar; se continuar, remova a versão mais "
                           "nova do driver CH340 na lista abaixo para o Windows usar a anterior.",
            "FTDI": "Pode ser um chip FTDI falsificado: os drivers recentes da FTDI recusam cópias. Remova a versão mais nova do driver FTDI "
                    "abaixo para voltar a uma anterior, ou troque por um conversor original ou CH340.",
            "Prolific PL2303": "Os drivers atuais da Prolific recusam PL2303 antigos ou falsificados. Remova a versão mais nova do driver abaixo "
                               "para voltar a uma anterior.",
        }
        return {"ok": False, "veredito": "Não inicia (código 10)",
                "conselho": dicas.get(nome, "Tente Reinstalar. Se continuar, troque o cabo e a porta USB (prefira as de trás do PC, sem hub).")}
    if p == 43:
        return {"ok": False, "veredito": "O Windows parou o aparelho (código 43)",
                "conselho": "Quase sempre é cabo ruim (alguns só carregam) ou porta USB com pouca energia. Troque o cabo e a porta e toque em Reinstalar."}
    if p == 22:
        return {"ok": False, "veredito": "Desativado (código 22)", "conselho": "Toque em Reinstalar para ativar de novo."}
    return {"ok": False, "veredito": f"Problema no driver (código {p})",
            "conselho": "Toque em Reinstalar. Se não resolver, instale o driver da página oficial."}


_PS_PACOTES = r"""
try {
  Get-WindowsDriver -Online -ErrorAction Stop | Where-Object { $_.ClassName -in 'Ports','USB','Modem' } |
    Select-Object @{n='publicado';e={[string]$_.Driver}}, @{n='original';e={Split-Path -Leaf ([string]$_.OriginalFileName)}},
      @{n='provedor';e={[string]$_.ProviderName}}, @{n='classe';e={[string]$_.ClassName}}, @{n='versao';e={[string]$_.Version}},
      @{n='data';e={ if ($_.Date) { ([datetime]$_.Date).ToString('dd/MM/yyyy') } else { '' } }} | ConvertTo-Json -Compress
} catch { }
"""


def _chip_do_pacote(original: str, provedor: str, classe: str):
    o, p = (original or "").lower(), (provedor or "").lower()
    return next((c for c in CHIPS if o in c[2] or (classe in ("Ports", "USB") and any(t in p for t in c[3]))), None)


def _versao_tupla(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def pacotes(em_uso_infs: set[str]) -> list[dict]:
    """Versões dos drivers seriais guardadas no Windows (driver store). Precisa de administrador."""
    if not windows.eh_admin():
        return []
    saida = []
    for d in windows.powershell_json(_PS_PACOTES, timeout=180):
        chip = _chip_do_pacote(d.get("original", ""), d.get("provedor", ""), d.get("classe", ""))
        if not chip or not re.fullmatch(r"oem\d+\.inf", d.get("publicado") or "", re.I):
            continue
        saida.append({"publicado": d["publicado"], "original": d.get("original", ""), "provedor": d.get("provedor", ""),
                      "versao": d.get("versao", ""), "data": d.get("data", ""), "chip": chip[0],
                      "em_uso": d["publicado"].lower() in em_uso_infs})
    saida.sort(key=lambda x: (x["chip"], tuple(-n for n in _versao_tupla(x["versao"]))))
    return saida


def _pasta_backup() -> Path:
    return config.pasta_dados() / "drivers_seriais"


def tem_backup_pacotes() -> bool:
    p = _pasta_backup()
    return p.exists() and any(p.rglob("*.inf"))


def ide_versao() -> str | None:
    if not windows.EH_WINDOWS:
        return None
    import winreg
    for raiz, flag in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY), (winreg.HKEY_CURRENT_USER, 0),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(raiz, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0, winreg.KEY_READ | flag) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i)) as s:
                            nome = winreg.QueryValueEx(s, "DisplayName")[0]
                            if str(nome).lower().startswith("arduino ide"):
                                try:
                                    return str(winreg.QueryValueEx(s, "DisplayVersion")[0])
                                except OSError:
                                    return str(nome)
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def _processos(nomes) -> list[psutil.Process]:
    alvo = {n.lower() + ".exe" for n in nomes} | {n.lower() for n in nomes}
    saida = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() in alvo:
                saida.append(p)
        except (psutil.Error, OSError):
            continue
    return saida


def ide_aberta() -> bool:
    return bool(_processos(["Arduino IDE"]))


def seriais(progresso=lambda *a: None) -> dict:
    progresso(0.2, "Verificando placas, conversores e drivers...")
    lista = aparelhos()
    conectados = []
    for a in lista:
        chip = chip_por_vid(a["vid"])
        if not a["presente"] or not chip:
            continue
        # FTDI aparece duas vezes (barramento USB e a porta): mostra só a porta, a não ser que o barramento tenha problema.
        if chip[0] == "FTDI" and a["id"].upper().startswith("USB\\") and a["problema"] is None and a["pid"] != "0000":
            continue
        conectados.append({**a, "chip": chip[0], "pagina": chip[4], **diagnosticar(a)})
    progresso(0.6, "Lendo as versões de driver guardadas no Windows...")
    em_uso = {a["inf"].lower() for a in conectados if a.get("inf")}
    return {"conectados": conectados, "pacotes": pacotes(em_uso), "admin": windows.eh_admin(), "tem_backup": tem_backup_pacotes(),
            "chips": [{"nome": c[0], "pagina": c[4]} for c in CHIPS], "ide": ide_versao(), "ide_aberta": ide_aberta(),
            "prendendo": sorted({p.info["name"] for p in _processos(PRENDEM_PORTA)}), "winget": apps.winget_disponivel()}


def reinstalar(id_: str, progresso=lambda *a: None) -> dict:
    """Remove o aparelho conectado e pede ao Windows para detectar de novo (ele instala com o melhor driver que tiver)."""
    _exigir_admin("Reinstalar")
    if not any(a["id"] == id_ and a["presente"] for a in aparelhos()):
        raise ValueError("o aparelho não está conectado")
    progresso(0.3, "Removendo e detectando de novo...")
    codigo, saida = _pnputil("/remove-device", id_, timeout=60)
    _pnputil("/scan-devices", timeout=120)
    if codigo != 0:
        return {"ok": False, "mensagem": f"O pnputil não conseguiu remover: {_ultima(saida)}"}
    return {"ok": True, "mensagem": "Reinstalado: o Windows detectou o aparelho de novo."}


def remover_pacote(publicado: str, progresso=lambda *a: None) -> dict:
    """Faz backup (pnputil /export-driver) e remove a versão do driver do Windows."""
    _exigir_admin("Remover o driver")
    if not re.fullmatch(r"oem\d+\.inf", publicado or "", re.I):
        raise ValueError("nome de driver inválido")
    p = next((x for x in pacotes(set()) if x["publicado"].lower() == publicado.lower()), None)
    if not p:
        raise ValueError("esse driver não é de um chip serial conhecido")
    destino = _pasta_backup() / f"{datetime.now():%Y%m%d-%H%M%S}-{Path(p['original']).stem}-{p['versao']}"
    destino.mkdir(parents=True, exist_ok=True)
    progresso(0.2, "Fazendo backup do driver...")
    codigo, saida = _pnputil("/export-driver", publicado, str(destino))
    if codigo != 0 or not any(destino.rglob("*.inf")):
        shutil.rmtree(destino, ignore_errors=True)
        return {"ok": False, "mensagem": f"O backup falhou, então nada foi removido: {_ultima(saida)}"}
    progresso(0.6, "Removendo do Windows...")
    codigo, saida = _pnputil("/delete-driver", publicado, "/uninstall", "/force")
    _pnputil("/scan-devices", timeout=120)
    if codigo != 0:
        return {"ok": False, "mensagem": f"Não deu para remover: {_ultima(saida)} (o backup ficou em {destino})"}
    return {"ok": True, "mensagem": f"{p['chip']} {p['versao']} removido. Backup em {destino}; \"Restaurar removidos\" devolve."}


def restaurar_pacotes(progresso=lambda *a: None) -> dict:
    _exigir_admin("Restaurar")
    if not tem_backup_pacotes():
        return {"ok": False, "mensagem": "Não há drivers removidos para restaurar."}
    progresso(0.3, "Devolvendo os drivers ao Windows...")
    codigo, saida = _pnputil("/add-driver", str(_pasta_backup() / "*.inf"), "/subdirs", "/install")
    if codigo in (0, 3010, 259):
        return {"ok": True, "mensagem": "Drivers restaurados." + (" Reinicie o PC para terminar." if codigo == 3010 else "")}
    return {"ok": False, "mensagem": f"Falhou: {_ultima(saida)}"}


def instalar_ide(progresso=lambda *a: None) -> dict:
    if ide_aberta():
        return {"ok": False, "mensagem": "Feche a Arduino IDE antes (salve o sketch) e tente de novo."}
    instalada = ide_versao() is not None
    progresso(0.2, ("Atualizando" if instalada else "Instalando") + " a Arduino IDE pelo winget (pode levar alguns minutos)...")
    ok, msg = apps.instalar_winget(WINGET_IDE, atualizar=instalada)
    if instalada and ok and msg != "Instalado.":
        msg = "A IDE já está na versão mais recente."
    elif instalada and not ok and "0x8A15002B" in msg:
        ok, msg = True, "A IDE já está na versão mais recente."
    return {"ok": ok, "mensagem": msg}


def fechar_prendendo() -> dict:
    fechados = 0
    for p in _processos(PRENDEM_PORTA):
        try:
            p.kill()
            fechados += 1
        except (psutil.Error, OSError):
            continue
    return {"ok": True, "mensagem": f"{fechados} programa(s) fechado(s). Tente o upload de novo." if fechados else "Nada para fechar."}


def _arduino15() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Arduino15"


def limpar_cache_ide() -> dict:
    """Apaga os índices baixados e a pasta staging do Arduino15. Placas e bibliotecas instaladas ficam."""
    if ide_aberta():
        return {"ok": False, "mensagem": "Feche a Arduino IDE antes de limpar o cache."}
    base = _arduino15()
    if not base.is_dir():
        return {"ok": False, "mensagem": "A pasta Arduino15 não existe (a IDE ainda não foi aberta neste usuário)."}
    n = 0
    for f in base.glob("*_index*.json*"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    liberado = 0
    staging = base / "staging"
    if staging.is_dir():
        liberado = sum(f.stat().st_size for f in staging.rglob("*") if f.is_file())
        shutil.rmtree(staging, ignore_errors=True)
    return {"ok": True, "mensagem": f"{n} índice(s) apagado(s) e {liberado / 2**20:.0f} MB de downloads temporários liberados. "
                                    "A IDE baixa os índices de novo ao abrir."}
