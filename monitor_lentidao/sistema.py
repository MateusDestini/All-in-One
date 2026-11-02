"""Informações do sistema: Windows, placa-mãe e BIOS, processador, memória RAM, vídeo e armazenamento.

O resumo da RAM (perfil EXPO/XMP estimado pelo part number e canais pelos nomes dos slots) foi adaptado do
ResetExplode (SystemInfoCollector.cs e DriveMap.cs; licença MIT, © 2026 Gustavo).
"""

import logging
import platform
import re
import socket

import psutil

from . import windows

log = logging.getLogger(__name__)

_SCRIPT = r"""
$d=@{}
$d.os=Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,BuildNumber,OSArchitecture,
  @{n='Instalado';e={if($_.InstallDate){$_.InstallDate.ToString('dd/MM/yyyy')}}},
  @{n='Boot';e={if($_.LastBootUpTime){$_.LastBootUpTime.ToString('dd/MM/yyyy HH:mm')}}},
  TotalVisibleMemorySize,FreePhysicalMemory,TotalVirtualMemorySize,FreeVirtualMemory
$d.pc=Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model,PCSystemType
$d.placa=Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product,Version,SerialNumber
$d.bios=Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion,
  @{n='Data';e={if($_.ReleaseDate){$_.ReleaseDate.ToString('dd/MM/yyyy')}}}
$d.cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,SocketDesignation,NumberOfCores,
  NumberOfLogicalProcessors,MaxClockSpeed,L2CacheSize,L3CacheSize,VirtualizationFirmwareEnabled)
$d.ram=@(Get-CimInstance Win32_PhysicalMemory | Select-Object BankLabel,DeviceLocator,Capacity,SMBIOSMemoryType,
  FormFactor,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,SerialNumber,DataWidth,TotalWidth,Attributes,
  ConfiguredVoltage,MinVoltage,MaxVoltage)
$d.arranjo=@(Get-CimInstance Win32_PhysicalMemoryArray | Select-Object MemoryDevices,MaxCapacity,MaxCapacityEx)
$d.video=@(Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM,
  CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)
$d.discos=@(Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,Size,InterfaceType,SerialNumber,FirmwareRevision,PNPDeviceID,MediaType)
$d.part=@(Get-Partition -ErrorAction SilentlyContinue | Where-Object DriveLetter | Select-Object @{n='Letra';e={[string]$_.DriveLetter}},DiskNumber)
$d.vol=@(Get-Volume -ErrorAction SilentlyContinue | Where-Object DriveLetter | Select-Object @{n='Letra';e={[string]$_.DriveLetter}},FileSystemLabel,FileSystem,@{n='Tipo';e={[string]$_.DriveType}},Size,SizeRemaining)
[pscustomobject]$d | ConvertTo-Json -Depth 4 -Compress
"""

TIPOS_RAM = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
FORMATOS_RAM = {8: "DIMM", 12: "SO-DIMM", 13: "Micro-DIMM", 15: "FB-DIMM", 9: "RIMM", 11: "SRIMM", 6: "Proprietário"}
RANKS = {1: "1 (single)", 2: "2 (dual)", 3: "3", 4: "4 (quad)"}

# Velocidades que costumam aparecer no part number (ex.: CMK32GX5M2B6000C30, F5-6000J3038F16GX2).
_VELOCIDADE_NO_MODELO = re.compile(
    r"(?<!\d)(2133|2400|2666|2933|3000|3200|3466|3600|3733|3800|4000|4133|4266|4400|4800|5200|5600|5800|6000|6200|"
    r"6400|6600|6800|7000|7200|7400|7600|7800|8000|8200|8400|8800)(?!\d)")
_KINGSTON = re.compile(r"\bKF([345])(\d{2})[A-Z]")  # KF560C36 = 6000 MT/s, KF436C17 = 3600 MT/s
_CANAL_NO_SLOT = re.compile(
    r"(?i)(?:channel|chan|ch)[\s_-]*([A-H0-9])|dimm[\s_-]*([A-H])\d|(?:^|[^A-Za-z])([A-H])[12](?:$|[^0-9])")


def _no(nome, valor=None, filhos=None) -> dict:
    return {"nome": nome, "valor": valor, "filhos": filhos}


def tamanho(b) -> str:
    if not b:
        return "—"
    v, i = float(b), 0
    while v >= 1024 and i < 4:
        v, i = v / 1024, i + 1
    return f"{v:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " " + ("B", "KB", "MB", "GB", "TB")[i]


def _s(d: dict | None, chave: str) -> str:
    v = (d or {}).get(chave)
    return "—" if v in (None, "") else str(v).strip() or "—"


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _volt(mv) -> str:
    v = _int(mv)
    return f"{v / 1000:.3f}".rstrip("0").rstrip(".").replace(".", ",") + " V" if v > 0 else "—"


def canal_do_slot(texto: str) -> str | None:
    m = _CANAL_NO_SLOT.search(texto or "")
    if not m:
        return None
    return next(g.upper() for g in m.groups() if g)


def resumo_ram(mods: list[dict], slots: int, maximo: int) -> list[dict]:
    """Linhas do resumo da memória: velocidade, perfil EXPO/XMP estimado, canais, ECC e tensão."""
    linhas = [_no("Pentes instalados", f"{len(mods)} de {slots} slots" if slots else str(len(mods)))]
    if maximo:
        linhas.append(_no("Capacidade máxima da placa", tamanho(maximo)))
    if not mods:
        return linhas
    linhas.append(_no("Tipo", " / ".join(dict.fromkeys(TIPOS_RAM.get(_int(m.get("SMBIOSMemoryType")), "Desconhecido")
                                                       for m in mods))))
    em_uso = min((_int(m.get("ConfiguredClockSpeed")) for m in mods if _int(m.get("ConfiguredClockSpeed"))), default=0)
    nominal = max((_int(m.get("Speed")) for m in mods), default=0)
    linhas.append(_no("Velocidade em uso", f"{em_uso} MT/s" if em_uso else "—"))
    linhas.append(_no("Velocidade nominal informada pelo pente", f"{nominal} MT/s" if nominal else "—"))

    # O Windows não diz se o perfil está ligado: estima pela velocidade escrita no part number.
    def velocidade_no_modelo(part: str) -> int:
        if m := _VELOCIDADE_NO_MODELO.search(part):
            return int(m.group())
        if m := _KINGSTON.search(part.upper()):
            return int(m.group(2)) * 100
        return 0
    rotulo = max((velocidade_no_modelo(x.get("PartNumber") or "") for x in mods), default=0)
    if not em_uso:
        perfil, nivel = "Não foi possível ler a velocidade em uso.", None
    elif rotulo and em_uso < rotulo * 0.95:
        perfil, nivel = (f"Provavelmente DESLIGADO: a RAM roda a {em_uso} MT/s, mas o modelo dos pentes indica {rotulo} MT/s. "
                         "Ative o EXPO (AMD) ou XMP (Intel) na BIOS."), "aviso"
    elif rotulo:
        perfil, nivel = f"Provavelmente LIGADO: roda a {em_uso} MT/s (o modelo dos pentes indica {rotulo} MT/s).", "ok"
    elif nominal and em_uso > nominal * 1.05:
        perfil, nivel = (f"Provavelmente LIGADO: roda a {em_uso} MT/s, acima dos {nominal} MT/s padrão do pente "
                         "(só chega nisso com EXPO/XMP)."), "ok"
    elif nominal and em_uso < nominal:
        perfil, nivel = f"Roda abaixo da velocidade nominal ({em_uso} de {nominal} MT/s). Confira EXPO/XMP na BIOS.", "aviso"
    else:
        perfil, nivel = "Sem como estimar (o modelo dos pentes não traz a velocidade). Confira na BIOS.", None
    linhas.append(dict(_no("Perfil EXPO/XMP (estimado)", perfil), nivel=nivel))

    # Canais: deduzidos pelos nomes dos slots (ex.: DIMM_A2 + DIMM_B2 = dois canais).
    canais = [canal_do_slot(m.get("DeviceLocator") or "") or canal_do_slot(m.get("BankLabel") or "") for m in mods]
    if len(mods) == 1:
        texto, nivel = ("Canal único (1 pente). Com 2 pentes iguais a RAM trabalha em dual channel e fica bem mais rápida.",
                        "aviso")
    elif all(canais):
        distintos = len(set(canais))
        texto, nivel = ((f"{distintos} canais (dual channel ou mais)", "ok") if distintos >= 2 else
                        ("Canal único: os pentes estão no mesmo canal. Mude para os slots recomendados no manual da placa.",
                         "aviso"))
    else:
        locais = ", ".join(m.get("DeviceLocator") or "?" for m in mods)
        texto, nivel = f"Não deu para identificar pelos nomes dos slots ({locais}). Veja o manual da placa.", None
    linhas.append(dict(_no("Canais (estimado)", texto), nivel=nivel))

    ecc = any(_int(m.get("TotalWidth")) > _int(m.get("DataWidth")) > 0 for m in mods)
    linhas.append(_no("ECC", "Sim (correção de erros)" if ecc else "Não"))
    tensoes = list(dict.fromkeys(v for m in mods if (v := _volt(m.get("ConfiguredVoltage"))) != "—"))
    if tensoes:
        linhas.append(_no("Tensão em uso", " / ".join(tensoes)))
    linhas.append(_no("Timings (CL, tRCD, tRP...)", "O Windows não informa. Veja na BIOS ou no CPU-Z (aba SPD)."))
    return linhas


def _secao_ram(d: dict) -> list[dict]:
    mods = d.get("ram") or []
    slots = sum(_int(a.get("MemoryDevices")) for a in d.get("arranjo") or [])
    maximo = sum((_int(a.get("MaxCapacityEx")) or _int(a.get("MaxCapacity"))) * 1024 for a in d.get("arranjo") or [])
    total = sum(_int(m.get("Capacity")) for m in mods)
    itens = [_no("Total instalado", tamanho(total or psutil.virtual_memory().total)),
             _no("Resumo", filhos=resumo_ram(mods, slots, maximo))]
    vm = psutil.virtual_memory()
    itens[1]["filhos"].append(_no("Em uso agora", f"{tamanho(vm.used)} de {tamanho(vm.total)} ({vm.percent:.0f}%)"))
    os_ = d.get("os") or {}
    if _int(os_.get("TotalVirtualMemorySize")):
        tot, livre = _int(os_["TotalVirtualMemorySize"]) * 1024, _int(os_.get("FreeVirtualMemory")) * 1024
        itens[1]["filhos"].append(_no("Memória virtual em uso (RAM + arquivo de paginação)",
                                      f"{tamanho(tot - livre)} de {tamanho(tot)}"))
    for m in mods:
        conf, nom = _int(m.get("ConfiguredClockSpeed")), _int(m.get("Speed"))
        faixa = (f"{_volt(m.get('MinVoltage'))} – {_volt(m.get('MaxVoltage'))}"
                 if _int(m.get("MinVoltage")) and _int(m.get("MaxVoltage")) else "—")
        largura = (f"{_int(m.get('DataWidth'))} / {_int(m.get('TotalWidth'))} bits" if _int(m.get("DataWidth")) else "—")
        itens.append(_no(f"Pente {_s(m, 'BankLabel')} / {_s(m, 'DeviceLocator')}", filhos=[
            _no("Capacidade", tamanho(_int(m.get("Capacity")))),
            _no("Tipo", TIPOS_RAM.get(_int(m.get("SMBIOSMemoryType")), "Desconhecido")),
            _no("Formato", FORMATOS_RAM.get(_int(m.get("FormFactor")), "—")),
            _no("Ranks", RANKS.get(_int(m.get("Attributes")), "—")),
            _no("Velocidade em uso", f"{conf} MT/s" if conf else "—"),
            _no("Velocidade nominal", f"{nom} MT/s" if nom else "—"),
            _no("Tensão em uso", _volt(m.get("ConfiguredVoltage"))),
            _no("Faixa de tensão do pente", faixa),
            _no("Largura de dados / total", largura),
            _no("Fabricante", _s(m, "Manufacturer")),
            _no("Part number", _s(m, "PartNumber")),
            _no("Nº de série", _s(m, "SerialNumber")),
        ]))
    return itens


def unidades(d: dict | None = None) -> list[dict]:
    """Cartões das unidades (letras) com ocupação e o disco físico de cada uma."""
    d = d or {}
    disco_da_letra = {p["Letra"].upper(): _int(p.get("DiskNumber")) for p in d.get("part") or [] if p.get("Letra")}
    discos = {_int(x.get("Index")): x for x in d.get("discos") or []}
    volumes = {v["Letra"].upper(): v for v in d.get("vol") or [] if v.get("Letra")}
    cartoes = []
    for p in psutil.disk_partitions(all=False):
        try:
            uso = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue  # leitor de cartão vazio, unidade de rede caída...
        letra = p.mountpoint.rstrip("\\/")[:2].upper() if windows.EH_WINDOWS else p.mountpoint
        disco = discos.get(disco_da_letra.get(letra[:1], -1), {})
        usb = (disco.get("InterfaceType") or "").upper() == "USB" or \
            (disco.get("PNPDeviceID") or "").upper().startswith("USBSTOR")
        opts = p.opts.lower()
        tipo = ("Removível" if "removable" in opts else "Externo (USB)" if usb else
                "Óptico" if "cdrom" in opts else "Rede" if "remote" in opts else "Interno")
        vol = volumes.get(letra[:1], {})
        cartoes.append({"letra": letra, "rotulo": vol.get("FileSystemLabel") or "", "tipo": tipo,
                        "sistema_arquivos": p.fstype or vol.get("FileSystem") or "",
                        "disco": (disco.get("Model") or "").strip(), "total": uso.total, "livre": uso.free,
                        "pct": round(uso.percent), "removivel": tipo in ("Removível", "Externo (USB)")})
    return sorted(cartoes, key=lambda c: c["letra"])


def coletar() -> dict:
    """{"secoes": [{nome, filhos}], "unidades": [...]}; cada item é {nome, valor, filhos}."""
    d = (windows.powershell_json(_SCRIPT, timeout=90) or [{}])[0]
    secoes = []

    def secao(nome, preencher):
        try:
            filhos = preencher()
        except Exception as e:  # uma parte quebrada não pode esconder as outras
            log.exception("falha ao montar %s", nome)
            filhos = [_no("Erro ao ler", str(e))]
        secoes.append(_no(nome, filhos=filhos))

    def sistema_operacional():
        os_ = d.get("os") or {}
        pc = d.get("pc") or {}
        tipo_pc = {1: "PC de mesa", 2: "Notebook", 3: "Estação de trabalho", 8: "Tablet"}.get(_int(pc.get("PCSystemType")))
        return [_no("Nome", os_.get("Caption") or platform.platform()),
                _no("Versão", os_.get("Version") or platform.version()),
                _no("Build", _s(os_, "BuildNumber")),
                _no("Arquitetura", os_.get("OSArchitecture") or platform.machine()),
                _no("Instalado em", _s(os_, "Instalado")),
                _no("Ligado desde", _s(os_, "Boot")),
                _no("Nome do computador", socket.gethostname()),
                _no("Fabricante / modelo", " ".join(x for x in (pc.get("Manufacturer"), pc.get("Model")) if x) or "—"),
                _no("Tipo", tipo_pc or "—")]

    def placa():
        b, bios = d.get("placa") or {}, d.get("bios") or {}
        return [_no("Placa-mãe", filhos=[_no("Fabricante", _s(b, "Manufacturer")), _no("Modelo", _s(b, "Product")),
                                         _no("Versão", _s(b, "Version")), _no("Nº de série", _s(b, "SerialNumber"))]),
                _no("BIOS", filhos=[_no("Fabricante", _s(bios, "Manufacturer")),
                                    _no("Versão", _s(bios, "SMBIOSBIOSVersion")), _no("Data", _s(bios, "Data"))])]

    def processador():
        itens = []
        for c in d.get("cpu") or []:
            virt = c.get("VirtualizationFirmwareEnabled")
            itens.append(_no(_s(c, "Name"), filhos=[
                _no("Fabricante", _s(c, "Manufacturer")), _no("Soquete", _s(c, "SocketDesignation")),
                _no("Núcleos físicos", _s(c, "NumberOfCores")), _no("Threads (lógicos)", _s(c, "NumberOfLogicalProcessors")),
                _no("Clock base declarado", f"{_s(c, 'MaxClockSpeed')} MHz"),
                _no("Cache L2", tamanho(_int(c.get("L2CacheSize")) * 1024)),
                _no("Cache L3", tamanho(_int(c.get("L3CacheSize")) * 1024)),
                _no("Virtualização na BIOS", "Ligada" if virt else "Desligada" if virt is False else "—")]))
        if not itens:
            itens.append(_no(platform.processor() or "Processador", filhos=[
                _no("Threads (lógicos)", str(psutil.cpu_count()))]))
        return itens

    def video():
        return [_no(_s(g, "Name"), filhos=[
            _no("Versão do driver", _s(g, "DriverVersion")),
            _no("Resolução atual", f"{_s(g, 'CurrentHorizontalResolution')}×{_s(g, 'CurrentVerticalResolution')} a "
                                   f"{_s(g, 'CurrentRefreshRate')} Hz" if g.get("CurrentHorizontalResolution") else "—"),
            _no("Detalhes completos", "Veja a página Placa de vídeo")]) for g in d.get("video") or []]

    def armazenamento():
        return [_no((x.get("Model") or "Disco").strip(), filhos=[
            _no("Capacidade", tamanho(_int(x.get("Size")))), _no("Interface", _s(x, "InterfaceType")),
            _no("Tipo", _s(x, "MediaType")), _no("Firmware", _s(x, "FirmwareRevision")),
            _no("Nº de série", _s(x, "SerialNumber"))]) for x in d.get("discos") or []]

    secao("Sistema operacional", sistema_operacional)
    secao("Placa-mãe e BIOS", placa)
    secao("Processador", processador)
    secao("Memória RAM", lambda: _secao_ram(d))
    secao("Placa de vídeo", video)
    secao("Armazenamento", armazenamento)
    try:
        cartoes = unidades(d)
    except Exception:
        log.exception("falha ao ler unidades")
        cartoes = []
    return {"secoes": secoes, "unidades": cartoes}


def sem_serie(no: dict) -> dict | None:
    """Cópia da árvore sem números de série (o relatório costuma ser enviado a terceiros)."""
    nome = (no.get("nome") or "").lower()
    if "série" in nome or "serial" in nome:
        return None
    filhos = no.get("filhos")
    if filhos is not None:
        filhos = [x for f in filhos if (x := sem_serie(f)) is not None]
    return dict(no, filhos=filhos)
