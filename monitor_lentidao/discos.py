"""Discos: saúde (S.M.A.R.T. pelo próprio Windows) e teste de velocidade no estilo CrystalDiskMark.

Leitura da saúde e critérios adaptados do ResetExplode (DiskHealth.cs e DiskBench.cs, licença MIT, © 2026 Gustavo).
"""

import json
import logging
import os
import struct
import time
from datetime import datetime
from pathlib import Path

import psutil

from . import config, windows
from .verificacoes import AVISO, CRITICO, Problema

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ saúde

_SCRIPT_SAUDE = r"""
$r=@{}
foreach($d in Get-CimInstance Win32_DiskDrive){ $r["$($d.Index)"]=[ordered]@{Indice=[int]$d.Index;Modelo="$($d.Model)".Trim();
  Serie="$($d.SerialNumber)".Trim();Firmware="$($d.FirmwareRevision)".Trim();Barramento="$($d.InterfaceType)";Tamanho=[int64]$d.Size;
  Pnp="$($d.PNPDeviceID)";Midia='';Saude=5;Letras=@()} }
$fisicos=@(); try { $fisicos=@(Get-PhysicalDisk -ErrorAction Stop) } catch {}
foreach($p in $fisicos){
  # O número do disco nem sempre bate (controladores de terceiros): tenta pelo número, depois pela série e pelo nome.
  $x=$r["$($p.DeviceId)"]
  if(-not $x -and $p.SerialNumber){ $x=@($r.Values | Where-Object { $_.Serie -and $_.Serie.Trim('. ') -eq "$($p.SerialNumber)".Trim('. ') })[0] }
  if(-not $x){ $x=@($r.Values | Where-Object { $_.Modelo -eq "$($p.FriendlyName)".Trim() -and $_.Saude -eq 5 })[0] }
  if(-not $x){ continue }
  $x.Saude=[int]$p.HealthStatus; $x.Midia=[int]$p.MediaType; $x.Bus=[int]$p.BusType
  try { $c=Get-StorageReliabilityCounter -PhysicalDisk $p -ErrorAction Stop
    $x.Temp=$c.Temperature;$x.TempMax=$c.TemperatureMax;$x.Desgaste=$c.Wear;$x.Horas=$c.PowerOnHours
    $x.ErrLeitura=$c.ReadErrorsTotal;$x.ErrEscrita=$c.WriteErrorsTotal;$x.ErrLeituraNC=$c.ReadErrorsUncorrected
    $x.ErrEscritaNC=$c.WriteErrorsUncorrected;$x.Ciclos=$c.StartStopCycleCount } catch { $x.ErroContador="$($_.Exception.Message)" } }
try { foreach($f in Get-CimInstance -Namespace root/wmi MSStorageDriver_FailurePredictStatus -ErrorAction Stop){
  foreach($x in $r.Values){ if($x.Pnp -and $f.InstanceName -like "$($x.Pnp)*"){ $x.FalhaPrevista=[bool]$f.PredictFailure } } } } catch {}
try { foreach($p in Get-Partition -ErrorAction Stop){ if($p.DriveLetter -and $r["$($p.DiskNumber)"]){ $r["$($p.DiskNumber)"].Letras+="$($p.DriveLetter):" } } } catch {}
@($r.Values) | ConvertTo-Json -Compress -Depth 3
"""

MIDIA = {3: "HDD", 4: "SSD", 5: "SCM"}
BARRAMENTO = {17: "NVMe", 11: "SATA", 7: "USB", 10: "SAS", 8: "RAID"}


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v < 0 or v > 1e12 else v  # 0xFFFF... = "não suportado" em vários drivers


def _tamanho(b) -> str:
    v, i, u = float(b or 0), 0, ["B", "KB", "MB", "GB", "TB"]
    while v >= 1000 and i < len(u) - 1:
        v, i = v / 1000, i + 1
    return f"{v:.0f} {u[i]}" if b else ""


def nvme_saude(indice: int) -> dict:
    """Lê o registro de saúde (SMART/Health, log 02h) direto do SSD NVMe, como o CrystalDiskInfo.

    Funciona mesmo quando o Windows não preenche os contadores de confiabilidade (comum em SSDs de marcas
    menores ou com driver de terceiros). Devolve {} se o disco não for NVMe ou o Windows recusar.
    """
    if not windows.EH_WINDOWS:
        return {}
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    invalido = wintypes.HANDLE(-1).value
    tam = 8 + 40 + 512  # STORAGE_PROPERTY_QUERY + STORAGE_PROTOCOL_SPECIFIC_DATA + log de 512 bytes
    for acesso in (0, 0xC0000000):  # sem acesso basta na maioria dos PCs; leitura/escrita precisa de administrador
        h = k.CreateFileW(f"\\\\.\\PhysicalDrive{indice}", acesso, 3, None, 3, 0, None)
        if h in (None, invalido):
            continue
        try:
            buf = (ctypes.c_ubyte * tam)()
            # PropertyId=StorageDeviceProtocolSpecificProperty(50), QueryType=0, NVMe(3), LogPage(2), log 02h
            struct.pack_into("<II10I", buf, 0, 50, 0, 3, 2, 2, 0, 40, 512, 0, 0, 0, 0)
            n = wintypes.DWORD()
            if not k.DeviceIoControl(h, 0x2D1400, buf, tam, buf, tam, ctypes.byref(n), None):
                continue
            desloc = struct.unpack_from("<I", buf, 8 + 16)[0]  # ProtocolDataOffset (relativo aos dados específicos)
            log_ = bytes(buf[8 + desloc:8 + desloc + 512])
        finally:
            k.CloseHandle(h)
        if len(log_) < 512 or not any(log_):
            return {}
        u128 = lambda o: int.from_bytes(log_[o:o + 16], "little")  # noqa: E731
        kelvin = int.from_bytes(log_[1:3], "little")
        return {
            "AvisoCritico": log_[0], "Temp": kelvin - 273 if kelvin > 200 else None,
            "Reserva": log_[3], "ReservaMin": log_[4], "Desgaste": min(log_[5], 255),
            "LidoGB": u128(32) * 512000 / 1e9, "GravadoGB": u128(48) * 512000 / 1e9,
            "Ciclos": u128(112), "Horas": u128(128), "Desligamentos": u128(144),
            "ErrMidia": u128(160), "ErrLog": u128(176),
        }
    return {}


def _completar_com_nvme(d: dict) -> dict:
    nv = nvme_saude(int(d.get("Indice", -1))) if d.get("Indice") is not None else {}
    if not nv:
        return d
    d = dict(d, Nvme=nv)
    for chave in ("Temp", "Desgaste", "Horas", "Ciclos"):
        if _num(d.get(chave)) in (None, 0) and nv.get(chave) is not None:
            d[chave] = nv[chave]
    d.setdefault("Bus", 17)
    if not d.get("Midia"):
        d["Midia"] = 4
    return d


def avaliar(d: dict) -> dict:
    """Transforma a leitura bruta de um disco num cartão: veredito, motivos e métricas."""
    midia = MIDIA.get(d.get("Midia"), "")
    bus = BARRAMENTO.get(d.get("Bus"), "USB" if str(d.get("Pnp", "")).upper().startswith("USBSTOR") else d.get("Barramento") or "")
    ssd = midia == "SSD" or bus == "NVMe"
    saude = d.get("Saude", 5)
    temp, temp_max, desgaste, horas = (_num(d.get(k)) for k in ("Temp", "TempMax", "Desgaste", "Horas"))
    err_l, err_e = _num(d.get("ErrLeituraNC")), _num(d.get("ErrEscritaNC"))
    falha = d.get("FalhaPrevista")
    nv = d.get("Nvme") or {}
    tem_dados = saude != 5 or falha is not None or desgaste is not None or temp is not None or horas is not None or bool(nv)

    criticos, avisos = [], []
    if falha:
        criticos.append("O próprio disco avisa (S.M.A.R.T.) que deve falhar em breve. Faça backup agora.")
    if saude == 2:
        criticos.append("O Windows classifica o disco como NÃO saudável.")
    elif saude == 1:
        avisos.append("O Windows classifica a saúde do disco como \"atenção\".")
    if desgaste is not None:
        if desgaste >= 95:
            criticos.append(f"Desgaste de {desgaste:.0f}%: o SSD está no fim da vida útil. Troque e faça backup.")
        elif desgaste >= 80:
            avisos.append(f"Desgaste de {desgaste:.0f}%: o SSD já gastou boa parte da vida útil. Planeje a troca.")
    if temp:
        ok_max, crit_max = (70, 85) if ssd else (55, 65)
        if temp >= crit_max:
            criticos.append(f"Temperatura de {temp:.0f} °C é muito alta para este disco.")
        elif temp >= ok_max:
            avisos.append(f"Temperatura de {temp:.0f} °C está alta. Melhore a ventilação.")
    if nv:
        aviso = nv.get("AvisoCritico", 0)
        if aviso & 0x04:
            criticos.append("O SSD avisa que a confiabilidade está comprometida (erros internos). Faça backup agora.")
        if aviso & 0x08:
            criticos.append("O SSD entrou em modo somente leitura para proteger os dados. Faça backup e troque.")
        if aviso & 0x01 or (nv.get("ReservaMin") and nv.get("Reserva", 100) < nv["ReservaMin"]):
            criticos.append(f"A área reserva do SSD acabou ({nv.get('Reserva')}% restante). Ele pode falhar logo.")
        if nv.get("ErrMidia"):
            avisos.append(f"{nv['ErrMidia']} erros de mídia (dados que o SSD não conseguiu ler/gravar direito).")
    if (err_l or 0) > 0 or (err_e or 0) > 0:
        avisos.append(f"Erros não corrigidos: {err_l or 0:.0f} de leitura e {err_e or 0:.0f} de escrita.")

    nivel = "critico" if criticos else "aviso" if avisos else "ok" if tem_dados else "sem_dados"
    motivos = criticos + avisos or [("Nenhum problema encontrado nas leituras disponíveis." if tem_dados else
                                     "Este disco não informa dados de saúde (comum em pendrives e alguns HDs externos).")]
    metricas = []

    def m(rotulo, valor):
        if valor is not None:
            metricas.append([rotulo, valor])

    m("Temperatura", f"{temp:.0f} °C" + (f" (máx. registrada {temp_max:.0f} °C)" if temp_max else "") if temp else None)
    if desgaste is not None:
        m("Vida útil restante", f"{max(0, 100 - desgaste):.0f}%")
    m("Horas ligado", f"{horas:,.0f} h ({horas / 24:,.0f} dias)".replace(",", ".") if horas is not None else None)
    m("Ciclos liga/desliga", f"{_num(d.get('Ciclos')):,.0f}".replace(",", ".") if _num(d.get("Ciclos")) is not None else None)
    for rot, tot, nc in (("Erros de leitura", "ErrLeitura", err_l), ("Erros de escrita", "ErrEscrita", err_e)):
        if _num(d.get(tot)) is not None:
            m(rot, f"{_num(d.get(tot)):.0f}" + (f" ({nc:.0f} não corrigidos)" if nc else ""))
    m("Falha prevista (S.M.A.R.T.)", ("SIM" if falha else "Não") if falha is not None else None)
    m("Saúde segundo o Windows", {0: "Saudável", 1: "Atenção", 2: "Não saudável"}.get(saude))
    if nv:
        m("Total gravado", f"{nv['GravadoGB'] / 1000:.1f} TB".replace(".", ",") if nv["GravadoGB"] >= 1000 else f"{nv['GravadoGB']:.0f} GB")
        m("Total lido", f"{nv['LidoGB'] / 1000:.1f} TB".replace(".", ",") if nv["LidoGB"] >= 1000 else f"{nv['LidoGB']:.0f} GB")
        m("Área reserva", f"{nv['Reserva']}%")
        m("Desligamentos inesperados", f"{nv['Desligamentos']:,}".replace(",", "."))
        m("Erros de mídia", str(nv["ErrMidia"]))
    m("Firmware", d.get("Firmware") or None)
    return {
        "modelo": d.get("Modelo") or f"Disco {d.get('Indice', '')}",
        "meta": " · ".join(x for x in (midia or ("SSD" if ssd else ""), bus, _tamanho(d.get("Tamanho")),
                                       ("Unidades " + " ".join(d.get("Letras") or [])) if d.get("Letras") else "") if x),
        "nivel": "sem_dados" if bus == "USB" and not tem_dados else nivel,
        "motivos": " ".join(motivos), "metricas": metricas,
        "letras": d.get("Letras") or [], "hd": midia == "HDD",
    }


def saude() -> list[dict]:
    brutos = [_completar_com_nvme(d) for d in windows.powershell_json(_SCRIPT_SAUDE, timeout=60)]
    try:  # leitura bruta guardada para diagnóstico (Ajustes > Abrir pasta)
        (config.pasta_dados() / "discos_leitura.json").write_text(json.dumps(brutos, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    return [avaliar(d) for d in sorted(brutos, key=lambda d: d.get("Indice", 0))]


def problemas(cartoes: list[dict]) -> list[Problema]:
    """Usado na verificação periódica."""
    sistema = os.environ.get("SystemDrive", "C:").upper()
    saida = []
    for c in cartoes:
        if c["nivel"] in ("critico", "aviso"):
            saida.append(Problema(f"saude_{c['modelo']}", CRITICO if c["nivel"] == "critico" else AVISO,
                                  f"Disco {c['modelo']} " + ("com problema de saúde" if c["nivel"] == "critico" else "precisa de atenção"),
                                  c["motivos"], "Faça backup dos arquivos importantes. Detalhes na página Discos.",
                                  assinatura=c["motivos"], renotificar_min=24 * 60))
        if c["hd"] and sistema in c["letras"]:
            saida.append(Problema("sistema_em_hd", AVISO, "Windows instalado em HD mecânico",
                                  f"O disco do sistema ({c['modelo']}) é um HD, muito mais lento que um SSD.",
                                  "Trocar por um SSD costuma ser a melhoria de velocidade mais perceptível.",
                                  renotificar_min=7 * 24 * 60))
    return saida


# ------------------------------------------------------------------ teste de velocidade

# Os 4 testes do perfil padrão do CrystalDiskMark 8: (chave, rótulo, bloco, aleatório, fila)
TESTES = [
    ("seq_q8", "Sequencial 1 MB, 8 na fila", 1 << 20, 0, 8),
    ("seq_q1", "Sequencial 1 MB, 1 por vez", 1 << 20, 0, 1),
    ("rnd_q32", "Aleatório 4 KB, 32 na fila", 4096, 1, 32),
    ("rnd_q1", "Aleatório 4 KB, 1 por vez", 4096, 1, 1),
]
RODADAS = 3       # medidas por teste; vale a melhor, como no CrystalDiskMark
SEGUNDOS = 3.0    # duração de cada medida
AQUECIMENTO = 1.0  # roda antes, sem medir, para o disco sair do modo de economia
PAUSA = 1.0       # descanso entre testes (o SSD termina de gravar o cache)


def unidades() -> list[dict]:
    """Unidades que podem ser testadas (com pelo menos 1,5 GB livres)."""
    saida = []
    for p in psutil.disk_partitions(all=False):
        if "cdrom" in p.opts or not p.fstype or p.fstype.lower() in ("squashfs", "tmpfs", "overlay"):
            continue
        try:
            uso = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        if uso.free >= 1536 * 2**20:
            saida.append({"raiz": p.mountpoint, "nome": p.mountpoint.rstrip("\\"), "livre_gb": round(uso.free / 2**30)})
    return saida


def _lib():
    import ctypes
    from .memoria import _biblioteca
    lib = _biblioteca()
    lib.dt_preparar.restype = ctypes.c_int
    lib.dt_preparar.argtypes = [ctypes.c_wchar_p, ctypes.c_int64]
    lib.dt_verificar.restype = ctypes.c_int
    lib.dt_verificar.argtypes = [ctypes.c_wchar_p, ctypes.c_int64]
    lib.dt_medir.restype = ctypes.c_double
    lib.dt_medir.argtypes = [ctypes.c_wchar_p, ctypes.c_int64, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_double]
    return lib


def testar_velocidade(progresso, raiz: str, tamanho_mb: int = 1024) -> dict:
    """Mesmos 4 testes do CrystalDiskMark (leitura e escrita), melhor de 3 rodadas, em MB/s decimais."""
    lib = _lib()
    tamanho = tamanho_mb * 2**20
    if psutil.disk_usage(raiz).free < tamanho + 512 * 2**20:
        raise OSError(f"pouco espaço livre em {raiz} (precisa de {tamanho_mb + 512} MB)")
    pasta = Path(raiz) / "MonitorLentidaoTeste"
    pasta.mkdir(exist_ok=True)
    arquivo = str(pasta / "teste.tmp")
    resultado = {"quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "unidade": raiz.rstrip("\\/") or raiz,
                 "tamanho_mb": tamanho_mb}
    try:
        progresso(0.0, f"Preparando arquivo de teste de {tamanho_mb // 1024 or 1} GB...")
        if not lib.dt_preparar(arquivo, tamanho):
            raise OSError("não foi possível criar o arquivo de teste (sem permissão ou disco cheio?)")
        conferencia = lib.dt_verificar(arquivo, tamanho)
        if conferencia == 1:
            raise OSError("o Windows deixou o arquivo de teste comprimido, e assim ele mediria a memória e não o disco. "
                          "Desative a compressão da unidade (Propriedades > Compactar esta unidade) ou teste outra unidade")
        if conferencia == 2:
            raise OSError("o arquivo de teste não ficou gravado no disco (algum programa de cache ou antivírus está "
                          "interferindo); o resultado não seria confiável")
        total = len(TESTES) * 2 * RODADAS
        feito = 0
        for chave, rotulo, bloco, aleat, fila in TESTES:      # mesma ordem do CrystalDiskMark
            for escrever in (0, 1):
                tipo = "Escrita" if escrever else "Leitura"
                progresso(0.05 + 0.95 * feito / total, f"{tipo}: {rotulo}...")
                time.sleep(PAUSA)
                lib.dt_medir(arquivo, tamanho, bloco, aleat, escrever, fila, 1, AQUECIMENTO)
                medidas = []
                for _ in range(RODADAS):
                    progresso(0.05 + 0.95 * feito / total, f"{tipo}: {rotulo}...")
                    bps = lib.dt_medir(arquivo, tamanho, bloco, aleat, escrever, fila, 1, SEGUNDOS)
                    if bps < 0:
                        raise OSError("erro de leitura/escrita no disco durante o teste")
                    medidas.append(bps)
                    feito += 1
                nome = f"{chave}_{tipo.lower()}"
                melhor = max(medidas)
                resultado[nome] = round(melhor / 1e6, 2)
                resultado[nome + "_iops"] = round(melhor / bloco)
                resultado[nome + "_var"] = round(100 * (melhor - min(medidas)) / melhor, 1) if melhor else 0
        if resultado["seq_q8_leitura"] > 16000:  # acima do limite de um PCIe 5.0 x4: veio de cache, não do disco
            raise OSError("a leitura passou do limite físico de qualquer SSD, então veio de um cache e não do disco. "
                          "Algum programa de cache (PrimoCache, StoreMI, RAPID...) pode estar na frente desta unidade")
        variacoes = [v for k, v in resultado.items() if k.endswith("_var")]
        resultado["variacao_max"] = max(variacoes, default=0)
    finally:
        try:
            os.remove(arquivo)
        except OSError:
            pass
        try:
            pasta.rmdir()
        except OSError:
            pass
    resultado["avaliacao"] = _avaliar_velocidade(resultado)
    _salvar(resultado)
    progresso(1.0, "Concluído")
    return resultado


def _avaliar_velocidade(r: dict) -> dict:
    s = r["seq_q8_leitura"]
    if s >= 2500:
        tipo, texto = "SSD NVMe", "Velocidade de SSD NVMe. O disco não é gargalo para o dia a dia."
    elif s >= 400:
        tipo, texto = "SSD SATA", "Velocidade típica de SSD SATA. Ótimo para o Windows e programas."
    elif s >= 90:
        tipo, texto = "HD", ("Velocidade de HD mecânico. Abrir programas e o próprio Windows fica bem mais lento que num SSD; "
                             "se for o disco do sistema, trocar por um SSD é a melhoria mais perceptível.")
    else:
        tipo, texto = "Lento", ("Muito lento para um disco interno. Pode ser pendrive/USB 2.0, um HD com problema "
                                "ou outro programa usando o disco durante o teste.")
    nivel = "aviso" if tipo in ("HD", "Lento") else "ok"
    if r.get("variacao_max", 0) > 15:
        texto += (f" As rodadas variaram até {r['variacao_max']:.0f}% entre si: algum programa usou o disco durante o teste "
                  "ou o SSD esquentou. Feche o que estiver usando o disco e repita.")
    if r["rnd_q1_leitura_iops"] < 1000 and nivel == "ok":
        nivel, texto = "aviso", texto + " Mas o acesso aleatório ficou baixo: veja se o disco não está quase cheio ou em uso."
    return {"tipo": tipo, "nivel": nivel, "texto": texto}


def _arquivo() -> Path:
    return config.pasta_dados() / "testes_disco.json"


def historico() -> list[dict]:
    try:  # só testes no formato atual (estilo CrystalDiskMark)
        return [h for h in json.loads(_arquivo().read_text(encoding="utf-8")) if "seq_q8_leitura" in h]
    except (OSError, ValueError):
        return []


def _salvar(r: dict) -> None:
    resumo = {k: r[k] for k in ("quando", "unidade") + tuple(f"{c}_{t}" for c, *_ in TESTES for t in ("leitura", "escrita"))}
    _arquivo().write_text(json.dumps((historico() + [resumo])[-20:], ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ carregamento de jogo
PACOTE_MB, ARQUIVOS, ARQUIVO_KB, FIOS_CARREGAMENTO = 512, 1000, 64, 8


def _lib_carregamento():
    import ctypes
    lib = _lib()
    lib.dc_ler_pacote.restype = ctypes.c_double
    lib.dc_ler_pacote.argtypes = [ctypes.c_wchar_p, ctypes.c_int64]
    lib.dc_ler_arquivos.restype = ctypes.c_double
    lib.dc_ler_arquivos.argtypes = [ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_int, ctypes.c_int, ctypes.c_int]
    return lib


def testar_carregamento(progresso, raiz: str) -> dict:
    """Simula um jogo carregando um mapa: um pacote de 512 MB lido em sequência e mil arquivos de 64 KB abertos por
    8 threads, tudo sem o cache do Windows. Antes de medir, lê cada arquivo uma vez pelo caminho normal para o
    antivírus examinar (senão a medida seria a do antivírus). Os arquivos são apagados no final."""
    import ctypes
    lib = _lib_carregamento()
    pacote_bytes = PACOTE_MB * 2**20
    arq_bytes = ARQUIVO_KB * 1024
    if psutil.disk_usage(raiz).free < pacote_bytes + ARQUIVOS * arq_bytes + 512 * 2**20:
        raise OSError(f"pouco espaço livre em {raiz} (precisa de {PACOTE_MB + 64 + 512} MB)")
    pasta = Path(raiz) / "MonitorLentidaoCarregamento"
    pasta.mkdir(exist_ok=True)
    pacote = pasta / "mapa.pak"
    nomes = [str(pasta / f"asset{i:04d}.dat") for i in range(ARQUIVOS)]
    r = {"quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "unidade": raiz.rstrip("\\/") or raiz}
    try:
        progresso(0.0, f"Preparando o mapa de teste ({PACOTE_MB} MB)...")
        if not lib.dt_preparar(str(pacote), pacote_bytes):
            raise OSError("não foi possível criar os arquivos de teste (sem permissão ou disco cheio?)")
        if lib.dt_verificar(str(pacote), pacote_bytes) == 1:
            raise OSError("o Windows deixou os arquivos comprimidos, e assim o teste mediria a memória e não o disco")
        bloco = os.urandom(arq_bytes)
        for i, n in enumerate(nomes):
            with open(n, "wb") as f:
                f.write(bloco[i % 64:] + bloco[:i % 64])  # cada arquivo diferente, nada comprime
                f.flush()
                os.fsync(f.fileno())
            if i % 100 == 0:
                progresso(0.3 + 0.3 * i / ARQUIVOS, "Preparando os arquivos pequenos...")
        progresso(0.6, "Deixando o antivírus examinar os arquivos...")
        for n in [str(pacote)] + nomes:
            with open(n, "rb") as f:
                f.read(1)
        time.sleep(1.0)
        progresso(0.7, "Carregando o mapa...")
        t_pacote = lib.dc_ler_pacote(str(pacote), pacote_bytes)
        progresso(0.85, "Carregando os arquivos pequenos...")
        vetor = (ctypes.c_wchar_p * ARQUIVOS)(*nomes)
        t_arquivos = lib.dc_ler_arquivos(vetor, ARQUIVOS, arq_bytes, FIOS_CARREGAMENTO)
        if t_pacote <= 0 or t_arquivos <= 0:
            raise OSError("erro de leitura no disco durante o teste")
    finally:
        for n in [str(pacote)] + nomes:
            try:
                os.remove(n)
            except OSError:
                pass
        try:
            pasta.rmdir()
        except OSError:
            pass
    r.update(pacote_s=round(t_pacote, 3), pacote_mbs=round(pacote_bytes / 1e6 / t_pacote),
             arquivos_s=round(t_arquivos, 3), arquivos_por_s=round(ARQUIVOS / t_arquivos),
             arquivos_mbs=round(ARQUIVOS * arq_bytes / 1e6 / t_arquivos), total_s=round(t_pacote + t_arquivos, 2),
             pacote_mb=PACOTE_MB, arquivos=ARQUIVOS, arquivo_kb=ARQUIVO_KB)
    if r["pacote_mbs"] > 16000:
        raise OSError("a leitura passou do limite físico de qualquer SSD: veio de um cache, não do disco")
    r["avaliacao"] = _avaliar_carregamento(r)
    _salvar_carregamento(r)
    progresso(1.0, "Concluído")
    return r


def _avaliar_carregamento(r: dict) -> dict:
    t = r["total_s"]
    if t <= 0.6:
        return {"tipo": "Rápido", "nivel": "ok", "texto": "Carregamento de SSD NVMe: nos jogos, o tempo de tela de carregamento "
                "passa a depender do processador (descompactar e montar o mapa), não do disco."}
    if t <= 2.5:
        return {"tipo": "Bom", "nivel": "ok", "texto": "Carregamento de SSD SATA: rápido para qualquer jogo. Um NVMe ganha "
                "pouco na prática, menos em jogos com DirectStorage."}
    if t <= 8:
        return {"tipo": "Lento", "nivel": "aviso", "texto": "Os arquivos pequenos demoram: é o comportamento de HD mecânico. "
                "Instalar os jogos num SSD corta as telas de carregamento e os engasgos ao andar pelo mapa."}
    return {"tipo": "Muito lento", "nivel": "aviso", "texto": "Muito lento para jogos: HD mecânico cheio ou fragmentado, "
            "pendrive/HD externo em USB 2.0, ou outro programa usando o disco no teste."}


def _arquivo_carregamento() -> Path:
    return config.pasta_dados() / "testes_carregamento.json"


def historico_carregamento() -> list[dict]:
    try:
        return json.loads(_arquivo_carregamento().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _salvar_carregamento(r: dict) -> None:
    resumo = {k: r[k] for k in ("quando", "unidade", "pacote_s", "pacote_mbs", "arquivos_s", "arquivos_por_s", "total_s")}
    try:
        _arquivo_carregamento().write_text(json.dumps((historico_carregamento() + [resumo])[-20:], ensure_ascii=False),
                                           encoding="utf-8")
    except OSError:
        log.exception("não consegui salvar o teste de carregamento")
