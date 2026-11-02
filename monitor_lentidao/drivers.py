"""Drivers: o que está instalado, driver novo da placa de vídeo, drivers do Windows Update, backup e suporte da placa-mãe.

- Placa de vídeo NVIDIA: consulta a base pública de drivers da NVIDIA (a mesma da página de download) pela série e
  pelo modelo, e compara com a versão instalada. AMD não tem consulta de versão: o monitor procura o instalador na
  página oficial do modelo e, se não achar, abre a página. Intel: página oficial.
- Windows Update: lista os drivers certificados que o Windows tem para este PC e ainda não instalou.
- Backup: `pnputil /export-driver *` copia todos os drivers de terceiros para uma pasta (bom antes de formatar).

Ler, listar, procurar e fazer backup não mudam nada no PC. Instalar (driver da placa de vídeo ou do Windows Update)
só acontece pelo botão, com confirmação; o instalador da NVIDIA ou da AMD abre com a tela dele.
"""

import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import __version__, config, windows

log = logging.getLogger(__name__)

NVIDIA_LISTA = "https://www.nvidia.com/Download/API/lookupValueSearch.aspx?TypeID={tipo}&ParentID={pai}"
NVIDIA_DRIVER = ("https://gfwsl.geforce.com/services_toolkit/services/com/nvidia/services/AjaxDriverService.php"
                 "?func=DriverManualLookup&psid={psid}&pfid={pfid}&osID=57&languageCode=1033&beta=0&isWHQL=1&dch=1"
                 "&upCRD={studio}&qnf=0&sort1=1&numberOfResults=1")
PAGINAS = {"NVIDIA": "https://www.nvidia.com/Download/index.aspx",
           "AMD": "https://www.amd.com/en/support/download/drivers.html",
           "Intel": "https://www.intel.com/content/www/us/en/download-center/home.html"}
SUPORTE_PLACA_MAE = [  # (trecho do fabricante, marca, página de suporte; {modelo} vira a busca)
    ("ASUSTeK", "ASUS", "https://www.asus.com/support/download-center/"),
    ("ASUS", "ASUS", "https://www.asus.com/support/download-center/"),
    ("Micro-Star", "MSI", "https://www.msi.com/search/{modelo}"),
    ("MSI", "MSI", "https://www.msi.com/search/{modelo}"),
    ("Gigabyte", "Gigabyte", "https://www.gigabyte.com/Support"),
    ("ASRock", "ASRock", "https://www.asrock.com/support/index.asp"),
    ("MAXSUN", "MAXSUN", "https://www.maxsun.com/pages/download"),
    ("BIOSTAR", "Biostar", "https://www.biostar.com.tw/app/en/support/download.php"),
    ("Dell", "Dell", "https://www.dell.com/support/home/"),
    ("LENOVO", "Lenovo", "https://support.lenovo.com/"),
    ("HP", "HP", "https://support.hp.com/"),
    ("Hewlett", "HP", "https://support.hp.com/"),
    ("Acer", "Acer", "https://www.acer.com/support"),
]


def _pedir(url: str, timeout: float = 20, referer: str | None = None) -> bytes:
    cab = {"User-Agent": f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) Destini-Monitor/{__version__}"}
    if referer:
        cab["Referer"] = referer
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=timeout) as r:
        return r.read(8_000_000)


_pedir_url = _pedir  # trocado nos testes


# ------------------------------------------------------------------ inventário
_PS_INVENTARIO = r"""
Get-CimInstance Win32_PnPSignedDriver -ErrorAction SilentlyContinue | Where-Object { $_.DeviceName } |
  Select-Object DeviceName, DeviceClass, Manufacturer, DriverProviderName, DriverVersion, DriverDate, IsSigned, InfName |
  ConvertTo-Json -Compress
"""


def _data(v) -> str:
    """CIM devolve "/Date(1700000000000)/" ou "20231114000000.000000-000"."""
    if not v:
        return ""
    m = re.search(r"/Date\((\d+)", str(v))
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%d/%m/%Y")
    m = re.match(r"(\d{4})(\d{2})(\d{2})", str(v))
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else str(v)[:10]


def inventario() -> list[dict]:
    """Drivers em uso, um por aparelho. Os da Microsoft vêm marcados (os de terceiros são os que o backup leva)."""
    linhas = windows.powershell_json(_PS_INVENTARIO, timeout=90)
    saida, vistos = [], set()
    for d in linhas:
        nome = (d.get("DeviceName") or "").strip()
        chave = (nome, d.get("DriverVersion"))
        if not nome or chave in vistos:
            continue
        vistos.add(chave)
        provedor = d.get("DriverProviderName") or d.get("Manufacturer") or ""
        saida.append({"aparelho": nome, "classe": d.get("DeviceClass") or "", "fabricante": d.get("Manufacturer") or "",
                      "provedor": provedor, "versao": d.get("DriverVersion") or "", "data": _data(d.get("DriverDate")),
                      "assinado": bool(d.get("IsSigned")), "inf": d.get("InfName") or "",
                      "microsoft": "microsoft" in provedor.lower()})
    saida.sort(key=lambda x: (x["microsoft"], x["classe"].lower(), x["aparelho"].lower()))
    return saida


# ------------------------------------------------------------------ placa de vídeo
_PS_PLACAS = r"""
Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue |
  Select-Object Name, DriverVersion, DriverDate, AdapterCompatibility, PNPDeviceID | ConvertTo-Json -Compress
"""


def versao_nvidia(wmi: str) -> str:
    """31.0.15.5222 -> 552.22 (o número que a NVIDIA usa)."""
    p = (wmi or "").split(".")
    if len(p) != 4:
        return wmi or ""
    dig = p[2] + p[3]
    if len(dig) < 5:
        return wmi
    dig = dig[-5:]
    return f"{dig[:3]}.{dig[3:]}"


def _comparar(a: str, b: str) -> int:
    pa = [int(x) for x in re.findall(r"\d+", a or "")]
    pb = [int(x) for x in re.findall(r"\d+", b or "")]
    return (pa > pb) - (pa < pb)


def placas() -> list[dict]:
    saida = []
    for g in windows.powershell_json(_PS_PLACAS, timeout=40):
        nome = g.get("Name") or ""
        pnp = (g.get("PNPDeviceID") or "").upper()
        if "VEN_10DE" in pnp or "NVIDIA" in nome.upper():
            marca, instalado = "NVIDIA", versao_nvidia(g.get("DriverVersion") or "")
        elif "VEN_1002" in pnp or re.search(r"\bAMD\b|Radeon", nome, re.I):
            marca, instalado = "AMD", g.get("DriverVersion") or ""
        elif "VEN_8086" in pnp or "Intel" in nome:
            marca, instalado = "Intel", g.get("DriverVersion") or ""
        else:
            continue  # adaptador básico, placa virtual, etc.
        saida.append({"nome": nome, "marca": marca, "instalado": instalado, "wmi": g.get("DriverVersion") or "",
                      "data": _data(g.get("DriverDate")), "pagina": PAGINAS[marca]})
    return saida


def _serie_nvidia(nome: str) -> tuple[str, bool, str]:
    movel = bool(re.search(r"Laptop|Max-Q|Mobile|Notebook", nome, re.I))
    m = re.search(r"(RTX|GTX|GT)\s*(\d{3,4})", nome, re.I)
    if not m:
        raise ValueError(f"modelo de placa não reconhecido: {nome}")
    prefixo, n = m.group(1).upper(), int(m.group(2))
    if prefixo == "RTX":
        serie = f"RTX {n // 100} Series"
    elif 1600 <= n < 1700:
        serie = "16 Series"
    elif 1000 <= n < 1100:
        serie = "10 Series"
    else:
        serie = f"{n // 100 * 100} Series"
    return serie, movel, m.group(0)


def _lista_nvidia(tipo: int, pai: int) -> list[tuple[str, int]]:
    raiz = ET.fromstring(_pedir_url(NVIDIA_LISTA.format(tipo=tipo, pai=pai)))
    saida = []
    for v in raiz.iter("LookupValue"):
        nome, valor = (v.findtext("Name") or "").strip(), (v.findtext("Value") or "").strip()
        if nome and valor.isdigit():
            saida.append((nome, int(valor)))
    return saida


def _achar(no, chaves: set, achado: dict) -> None:
    if isinstance(no, dict):
        for k, v in no.items():
            if k in chaves and isinstance(v, (str, int)) and k not in achado and str(v).strip():
                achado[k] = str(v)
            _achar(v, chaves, achado)
    elif isinstance(no, list):
        for v in no:
            _achar(v, chaves, achado)


def verificar_nvidia(placa: dict, studio: bool = False) -> dict:
    serie, movel, trecho = _serie_nvidia(placa["nome"])
    series = [(n, v) for n, v in _lista_nvidia(2, 1) if serie.lower() in n.lower() and ("notebook" in n.lower()) == movel]
    if not series:
        raise ValueError(f"a NVIDIA não listou a série {serie}")
    psid = sorted(series, key=lambda s: len(s[0]))[0][1]
    produtos = _lista_nvidia(3, psid)
    if not produtos:
        raise ValueError("a NVIDIA não listou os modelos da série")
    chave = re.sub(r"^NVIDIA\s+", "", placa["nome"], flags=re.I).replace("Laptop GPU", "").strip().lower()
    pfid = next((v for n, v in produtos if n.strip().lower() == chave), None) or \
        next((v for n, v in produtos if trecho.lower() in n.lower()), produtos[0][1])
    dados = json.loads(_pedir_url(NVIDIA_DRIVER.format(psid=psid, pfid=pfid, studio=1 if studio else 0)).decode("utf-8", "replace"))
    achado: dict = {}
    _achar(dados, {"Version", "DownloadURL", "DownloadURLFileSize", "ReleaseDateTime"}, achado)
    if not achado.get("Version") or not achado.get("DownloadURL"):
        raise ValueError("a NVIDIA não devolveu um driver para esta placa")
    url = achado["DownloadURL"].replace("http://", "https://")
    if not re.match(r"https://([a-z0-9-]+\.)*nvidia\.com/", url):
        raise ValueError("endereço de download inesperado")
    return {"ultima": achado["Version"].strip(), "url": url, "tamanho": achado.get("DownloadURLFileSize", ""),
            "lancamento": achado.get("ReleaseDateTime", ""), "studio": studio}


def _pagina_amd(nome: str) -> str | None:
    m = re.search(r"RX\s*(\d{4})\s*(XTX|XT|GRE)?", nome, re.I)
    if not m:
        return None
    n, suf = m.group(1), (m.group(2) or "").lower()
    familia = {"9": "rx-9000-series", "7": "rx-7000-series", "6": "rx-6000-series", "5": "rx-5000-series"}.get(n[0])
    if not familia:
        return None
    return (f"https://www.amd.com/en/support/downloads/drivers.html/graphics/radeon-rx/{familia}/"
            f"amd-radeon-rx-{n}{('-' + suf) if suf else ''}.html")


def verificar_amd(placa: dict) -> dict:
    pagina = _pagina_amd(placa["nome"])
    if not pagina:
        raise ValueError("modelo AMD não reconhecido")
    html = _pedir_url(pagina, referer="https://www.amd.com/").decode("utf-8", "replace")
    links = re.findall(r"https://drivers\.amd\.com/drivers/[^\"'\s<>\\]+\.exe", html, re.I)
    links = [u for u in links if "win11" in u.lower()] or links
    if not links:
        raise ValueError("a página da AMD não mostrou o instalador")
    v = re.search(r"(\d{2}\.\d{1,2}\.\d{1,2})", links[0])
    return {"ultima": v.group(1) if v else "", "url": links[0], "tamanho": "", "pagina_modelo": pagina}


def verificar_placa(placa: dict, studio: bool = False) -> dict:
    """{ultima, url, tamanho, novo: bool|None, mensagem}"""
    try:
        if placa["marca"] == "NVIDIA":
            r = verificar_nvidia(placa, studio)
            r["novo"] = _comparar(r["ultima"], placa["instalado"]) > 0
            r["mensagem"] = (f"Tem driver mais novo: {r['ultima']} (você está com o {placa['instalado']})." if r["novo"]
                             else f"Você já está com o mais recente ({placa['instalado']}).")
            return r
        if placa["marca"] == "AMD":
            if not _pagina_amd(placa["nome"]):
                return {"novo": None, "mensagem": "Vídeo integrado ou modelo sem consulta: o driver vem no AMD Software, na página oficial da AMD."}
            r = verificar_amd(placa)
            r["novo"] = None  # a versão do driver da AMD no Windows não é a mesma numeração do Adrenalin
            r["mensagem"] = f"O driver mais recente na AMD é o {r['ultima'] or 'da página oficial'}."
            return r
        return {"novo": None, "mensagem": "Para Intel, use o assistente de drivers da Intel na página oficial."}
    except Exception as e:
        log.info("drivers: consulta da %s falhou: %s", placa.get("marca"), e)
        return {"novo": None, "erro": True, "mensagem": f"Não deu para consultar a {placa['marca']} agora ({e}). Confira na página oficial."}


def baixar_instalador(info: dict, marca: str, progresso) -> Path:
    """Baixa o instalador oficial para a pasta temporária. O monitor só abre: a instalação é a tela da NVIDIA/AMD."""
    url = info["url"]
    if not re.match(r"https://([a-z0-9-]+\.)*(nvidia\.com|amd\.com)/", url):
        raise ValueError("endereço de download inesperado")
    pasta = config.pasta_dados() / "drivers"
    pasta.mkdir(exist_ok=True)
    destino = pasta / os.path.basename(urllib.parse.urlparse(url).path)
    cab = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if marca == "AMD":
        cab["Referer"] = "https://www.amd.com/"
    with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=60) as r, open(destino, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        feito = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            feito += len(b)
            if total:
                progresso(feito / total, f"Baixando {feito / 2**20:.0f} de {total / 2**20:.0f} MB...")
    if total and feito != total:
        destino.unlink(missing_ok=True)
        raise OSError("download incompleto")
    return destino


_PS_ASSINATURA = r"""
$s = Get-AuthenticodeSignature -LiteralPath '%s'
@{ status = [string]$s.Status; quem = [string]$s.SignerCertificate.Subject } | ConvertTo-Json -Compress
"""


def assinatura_ok(arquivo: Path, marca: str) -> bool:
    r = windows.powershell_json(_PS_ASSINATURA % str(arquivo).replace("'", "''"), timeout=60)
    if not r:
        return False
    quem = (r[0].get("quem") or "").lower()
    nome = {"NVIDIA": "nvidia", "AMD": "advanced micro devices"}.get(marca, "")
    return r[0].get("status") == "Valid" and nome and nome in quem


def instalar_placa(placa: dict, info: dict, progresso) -> dict:
    """Baixa, confere a assinatura digital do fabricante e abre o instalador oficial."""
    arquivo = baixar_instalador(info, placa["marca"], progresso)
    progresso(1.0, "Conferindo a assinatura digital...")
    if not assinatura_ok(arquivo, placa["marca"]):
        arquivo.unlink(missing_ok=True)
        raise OSError(f"o arquivo baixado não está assinado pela {placa['marca']}; apaguei por segurança")
    os.startfile(str(arquivo))  # type: ignore[attr-defined]
    return {"ok": True, "arquivo": str(arquivo),
            "mensagem": f"O instalador da {placa['marca']} abriu. Siga a tela dele; a tela pode piscar durante a instalação."}


# ------------------------------------------------------------------ placa-mãe
_PS_PLACA_MAE = r"""
$b = Get-CimInstance Win32_BaseBoard -ErrorAction SilentlyContinue | Select-Object -First 1
$s = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue | Select-Object -First 1
@{ fabricante = [string]$b.Manufacturer; modelo = [string]$b.Product; sistema = [string]$s.Manufacturer } | ConvertTo-Json -Compress
"""


def placa_mae() -> dict:
    r = (windows.powershell_json(_PS_PLACA_MAE, timeout=30) or [{}])[0]
    fab, modelo = (r.get("fabricante") or "").strip(), (r.get("modelo") or "").strip()
    tudo = f"{fab} {r.get('sistema') or ''}"
    for trecho, marca, url in SUPORTE_PLACA_MAE:
        if re.search(rf"\b{re.escape(trecho)}", tudo, re.I):
            return {"fabricante": fab, "modelo": modelo, "marca": marca, "url": url.replace("{modelo}", urllib.parse.quote(modelo))}
    busca = urllib.parse.quote(f"{fab} {modelo} drivers download")
    return {"fabricante": fab, "modelo": modelo, "marca": fab or "Fabricante", "url": f"https://www.bing.com/search?q={busca}"}


# ------------------------------------------------------------------ Windows Update
_PS_WU_BUSCAR = r"""
try {
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver'")
  $l = @(); foreach ($u in $r.Updates) {
    $l += @{ id = [string]$u.Identity.UpdateID; titulo = [string]$u.Title; classe = [string]$u.DriverClass
             fabricante = [string]$u.DriverManufacturer; mb = [math]::Round($u.MaxDownloadSize / 1MB, 1)
             data = $(try { ([datetime]$u.DriverVerDate).ToString('dd/MM/yyyy') } catch { '' }) } }
  @{ ok = $true; drivers = $l } | ConvertTo-Json -Compress -Depth 4
} catch { @{ ok = $false; erro = $_.Exception.Message } | ConvertTo-Json -Compress }
"""

_PS_WU_INSTALAR = r"""
try {
  $ids = @(%s)
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver'")
  $c = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $r.Updates) { if ($ids -contains [string]$u.Identity.UpdateID) { if (-not $u.EulaAccepted) { $u.AcceptEula() }; [void]$c.Add($u) } }
  if ($c.Count -eq 0) { @{ ok = $false; erro = 'nada para instalar' } | ConvertTo-Json -Compress; return }
  $d = $s.CreateUpdateDownloader(); $d.Updates = $c; [void]$d.Download()
  $i = $s.CreateUpdateInstaller(); $i.Updates = $c; $res = $i.Install()
  @{ ok = ($res.ResultCode -eq 2 -or $res.ResultCode -eq 3); codigo = [int]$res.ResultCode; reiniciar = [bool]$res.RebootRequired; total = $c.Count } | ConvertTo-Json -Compress
} catch { @{ ok = $false; erro = $_.Exception.Message } | ConvertTo-Json -Compress }
"""


def windows_update_buscar(progresso=lambda *a: None) -> dict:
    progresso(0.1, "Procurando no Windows Update (pode levar 1 a 2 minutos)...")
    r = windows.powershell_json(_PS_WU_BUSCAR, timeout=300)
    if not r or not r[0].get("ok"):
        raise OSError((r[0].get("erro") if r else None) or "o Windows Update não respondeu")
    drivers = r[0].get("drivers") or []
    if isinstance(drivers, dict):
        drivers = [drivers]
    return {"drivers": drivers, "quando": datetime.now().strftime("%d/%m %H:%M")}


def windows_update_instalar(ids: list[str], progresso=lambda *a: None) -> dict:
    if not windows.eh_admin():
        raise PermissionError("para instalar pelo Windows Update o monitor precisa estar aberto como administrador")
    ids = [i for i in ids if re.fullmatch(r"[0-9a-fA-F-]{36}", i or "")]
    if not ids:
        raise ValueError("nenhum driver escolhido")
    progresso(0.1, f"Baixando e instalando {len(ids)} driver(s) pelo Windows Update...")
    r = windows.powershell_json(_PS_WU_INSTALAR % ",".join(f"'{i}'" for i in ids), timeout=1800)
    if not r or not r[0].get("ok"):
        raise OSError((r[0].get("erro") if r else None) or "o Windows Update não terminou a instalação")
    return {"ok": True, "reiniciar": bool(r[0].get("reiniciar")), "total": r[0].get("total"),
            "mensagem": "Drivers instalados." + (" Reinicie o PC para terminar." if r[0].get("reiniciar") else "")}


# ------------------------------------------------------------------ backup
def pasta_backup() -> Path:
    docs = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Documents"
    return docs / "Destini-Drivers"


def backup(progresso=lambda *a: None) -> dict:
    """pnputil /export-driver * <pasta>: copia todos os drivers de terceiros (precisa de administrador)."""
    if not windows.eh_admin():
        raise PermissionError("o backup dos drivers precisa do monitor aberto como administrador")
    destino = pasta_backup() / datetime.now().strftime("%Y-%m-%d_%H%M")
    destino.mkdir(parents=True, exist_ok=True)
    progresso(0.1, "Copiando os drivers (pode levar alguns minutos)...")
    p = subprocess.run(["pnputil.exe", "/export-driver", "*", str(destino)], capture_output=True, timeout=1800,
                       creationflags=windows.CREATE_NO_WINDOW)
    saida = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    pacotes = [d for d in destino.iterdir() if d.is_dir()] if destino.exists() else []
    if p.returncode != 0 and not pacotes:
        raise OSError(f"o pnputil não conseguiu exportar ({saida.strip()[-200:]})")
    tamanho = sum(f.stat().st_size for f in destino.rglob("*") if f.is_file())
    return {"ok": True, "pasta": str(destino), "pacotes": len(pacotes), "mb": round(tamanho / 2**20),
            "mensagem": f"{len(pacotes)} drivers copiados para {destino}."}


def abrir_pasta_backup() -> None:
    p = pasta_backup()
    p.mkdir(parents=True, exist_ok=True)
    windows.abrir(p)


# ------------------------------------------------------------------ tudo de uma vez (para a página)
_cache: dict = {}
_trava = threading.Lock()


def estado(progresso=lambda *a: None, forcar: bool = False, studio: bool = False) -> dict:
    """Placas de vídeo (com a consulta de versão), placa-mãe e a lista de drivers. Lento na primeira vez."""
    with _trava:
        if _cache.get("quando") and not forcar and time.time() - _cache["quando"] < 600 and _cache.get("studio") == studio:
            return _cache["dados"]
        progresso(0.1, "Lendo as placas de vídeo...")
        gpus = placas()
        for i, g in enumerate(gpus):
            progresso(0.2 + 0.4 * i / max(1, len(gpus)), f"Consultando a {g['marca']}...")
            g["consulta"] = verificar_placa(g, studio)
        progresso(0.6, "Lendo a placa-mãe...")
        mae = placa_mae()
        progresso(0.7, "Listando os drivers instalados...")
        dados = {"placas": gpus, "placa_mae": mae, "drivers": inventario(), "admin": windows.eh_admin(),
                 "pasta_backup": str(pasta_backup()), "studio": studio}
        _cache.update(quando=time.time(), dados=dados, studio=studio)
        return dados


def instalar_placa_indice(progresso, indice: int) -> dict:
    """Instala o driver novo da placa `indice` da última consulta (o botão só aparece quando há um link)."""
    dados = _cache.get("dados") or {}
    gpus = dados.get("placas") or []
    if not 0 <= indice < len(gpus):
        raise ValueError("consulte os drivers de novo")
    g = gpus[indice]
    info = g.get("consulta") or {}
    if not info.get("url"):
        raise ValueError("não há instalador para esta placa; use a página oficial")
    return instalar_placa(g, info, progresso)
