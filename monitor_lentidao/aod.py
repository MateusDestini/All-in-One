"""Timings e tensões da memória gravados na BIOS pelo Windows, do jeito que o Ryzen Master faz.

As placas AM5 (e muitas AM4) trazem na BIOS a interface AOD da AMD ("AMD Overclocking"), que o Windows expõe como a
classe WMI `AMD_ACPI` (em root\\wmi, pelo driver de WMI do próprio Windows). Ela se descreve sozinha:
- `GetObjectID` / `GetObjectID2` listam as funções que a BIOS aceita (ID + nome, ex.: "Set Tcl");
- `GetMinMaxSteppingValues(ID)` diz o valor de agora, o mínimo, o máximo e o passo de cada uma;
- `RunCommand(Inbuf)` executa: 8 bytes, o ID (uint32) e o valor (uint32). As mudanças vão entre
  "Command Buffer Start" e "Command Buffer End" e a BIOS as aplica no próximo boot, quando treina a memória.

O monitor só usa o que a BIOS da pessoa lista, só dentro do mínimo, máximo e passo que ela informa, guarda os valores
de antes para desfazer, tem a volta ao padrão da BIOS ("Reset Mem Para") e confere depois do reinício se os timings
entraram (lendo o controlador de memória). Tensões têm teto próprio, abaixo do máximo da BIOS.
"""

import base64
import json
import logging
import re
import subprocess
import threading
import time

import psutil

from . import config, oc_cpu, windows

log = logging.getLogger(__name__)


def _norm(nome: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (nome or "").lower())


# nome da função na BIOS (normalizado) -> (chave do monitor, rótulo, grupo)
MAPA = {
    "settcl": ("cl", "CL", "Primários"), "settrcd": ("trcd", "tRCD", "Primários"), "settrp": ("trp", "tRP", "Primários"),
    "settras": ("tras", "tRAS", "Primários"), "settrc": ("trc", "tRC", "Primários"),
    "settrfc1": ("trfc", "tRFC", "Refresh"), "settrfc2": ("trfc2", "tRFC2", "Refresh"), "settrfcsb": ("trfcsb", "tRFCsb", "Refresh"),
    "settrrds": ("trrds", "tRRD_S", "Secundários"), "settrrdl": ("trrdl", "tRRD_L", "Secundários"),
    "settfaw": ("tfaw", "tFAW", "Secundários"), "settwtrs": ("twtrs", "tWTR_S", "Secundários"),
    "settwtrl": ("twtrl", "tWTR_L", "Secundários"), "settwr": ("twr", "tWR", "Secundários"),
    "settrtp": ("trtp", "tRTP", "Secundários"),
    "settrdrdscl": ("trdrdscl", "tRDRD_SCL", "Turnaround"), "settrdrdsc": ("trdrdsc", "tRDRD_SC", "Turnaround"),
    "settrdrdsd": ("trdrdsd", "tRDRD_SD", "Turnaround"), "settrdrddd": ("trdrddd", "tRDRD_DD", "Turnaround"),
    "settwrwrscl": ("twrwrscl", "tWRWR_SCL", "Turnaround"), "settwrwrsc": ("twrwrsc", "tWRWR_SC", "Turnaround"),
    "settwrwrsd": ("twrwrsd", "tWRWR_SD", "Turnaround"), "settwrwrdd": ("twrwrdd", "tWRWR_DD", "Turnaround"),
    "settrdwr": ("trdwr", "tRDWR", "Turnaround"), "settwrrd": ("twrrd", "tWRRD", "Turnaround"),
    "setvddio": ("vdd", "VDD (memória)", "Tensões"), "setdimmvddq": ("vddq", "VDDQ", "Tensões"),
    "setvpp": ("vpp", "VPP", "Tensões"),
    # estas a BIOS dá como lista de opções (Getdvalues), não como faixa
    "setmemclock": ("mclk", "Velocidade", "Frequência"), "setfclkfrequency": ("fclk", "FCLK", "Frequência"),
    "setuclkdiv1": ("uclk1", "UCLK 1:1", "Frequência"),
}
GRUPOS = ("Frequência", "Primários", "Refresh", "Secundários", "Turnaround", "Tensões")
LISTAS = {"mclk", "fclk", "uclk1"}


def faixa_segura(chave: str, ddr5: bool) -> tuple[int, int]:
    """Das opções da BIOS, o que o monitor deixa escolher. DDR5 (AM5): até 6400 MT/s, que é o limite do controlador
    em 1:1, e FCLK até 2200 MHz. DDR4 (AM4): até 3800 MT/s e FCLK até 1900 MHz."""
    if chave == "mclk":
        return (2000, 3200) if ddr5 else (800, 1900)
    if chave == "fclk":
        return (1600, 2200) if ddr5 else (1200, 1900)
    return (0, 1)
TENSOES = {"vdd", "vddq", "vpp"}
TETO_MV = {"vdd": 1450, "vddq": 1450, "vpp": 1900}  # abaixo do máximo da BIOS: acima disso só com refrigeração nos pentes
INICIO, FIM, RESET = "commandbufferstart", "commandbufferend", "resetmempara"

# Acha a instância da AMD_ACPI. Em algumas placas o Windows não a lista (Get-WmiObject volta vazio), mas ela
# responde chamada pelo nome: "<dispositivo PNP0C14>_0".
_PS_ACHAR = r"""
$o = $null
foreach ($d in @(Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object { $_.InstanceId -like 'ACPI\PNP0C14\*' })) {
  for ($n = 0; $n -lt 2 -and -not $o; $n++) {
    $nome = ($d.InstanceId + '_' + $n).Replace('\', '\\')
    try { $x = [wmi]"root\wmi:AMD_ACPI.InstanceName=`"$nome`""; if ($null -ne $x.QueryVersion().version) { $o = $x } } catch {}
  }
  if ($o) { break }
}
if (-not $o) { try { $o = Get-WmiObject -Namespace root\wmi -Class AMD_ACPI -ErrorAction Stop | Select-Object -First 1 } catch {} }
"""

_PS_LISTAR = _PS_ACHAR + r"""
if (-not $o) { @{ ok = $false } | ConvertTo-Json -Compress; return }
$r = @{ ok = $true; versao = [int]$o.QueryVersion().version; instancia = [string]$o.InstanceName; funcoes = @() }
foreach ($m in 'GetObjectID', 'GetObjectID2') {
  $p = $null; try { $p = $o.$m().pack } catch {}
  if (-not $p) { continue }
  for ($i = 0; $i -lt @($p.ID).Count; $i++) {
    if (-not $p.IDString[$i]) { continue }
    $id = [uint32]$p.ID[$i]; $f = @{ id = [int64]$id; nome = [string]$p.IDString[$i] }
    try { $q = $o.GetMinMaxSteppingValues($id).pack
          $f.atual = [int64]$q.Current; $f.min = [int64]$q.Min; $f.max = [int64]$q.Max; $f.passo = [int64]$q.Stepping } catch {}
    if (-not $f.max -and $f.nome -match 'Mem Clock|FCLK Freq|UCLK DIV1') {
      try { $d = $o.Getdvalues($id).pack; $n = [int]$d.Length
            if ($n -gt 0) { $f.atual = [int64]$d.Current; $f.opcoes = @(@($d.DValuesBuffer)[0..($n - 1)] | ForEach-Object { [int64]$_ }) } } catch {}
    }
    $r.funcoes += $f
  }
}
$r | ConvertTo-Json -Compress -Depth 4
"""

_PS_RODAR = _PS_ACHAR + r"""
if (-not $o) { @{ ok = $false; erro = 'A interface AOD da BIOS não respondeu.' } | ConvertTo-Json -Compress; return }
$cmds = @()
{CMDS}
$passos = @(); $ok = $true
foreach ($c in $cmds) {
  $b = New-Object byte[] 8
  [BitConverter]::GetBytes([uint32]$c[0]).CopyTo($b, 0); [BitConverter]::GetBytes([uint32]$c[1]).CopyTo($b, 4)
  try {
    $out = $o.RunCommand($b)
    $volta = @(); try { $volta = @($out.Outbuf.Result) | Select-Object -First 16 } catch {}
    $passos += @{ id = [int64]$c[0]; valor = [int64]$c[1]; ok = $true; volta = ($volta -join ',') }
  } catch {
    $passos += @{ id = [int64]$c[0]; valor = [int64]$c[1]; ok = $false; erro = $_.Exception.Message }
    $ok = $false; break
  }
}
@{ ok = $ok; passos = $passos } | ConvertTo-Json -Compress -Depth 4
"""


def _powershell(script: str, timeout: int = 60) -> dict:
    """Roda o script pelo -EncodedCommand (sem problema de aspas) e lê o JSON do fim."""
    if not windows.EH_WINDOWS:
        return {"ok": False, "erro": "Só no Windows."}
    prefixo = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';"
    codigo = base64.b64encode((prefixo + script).encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", codigo], capture_output=True, timeout=timeout,
                           creationflags=windows.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "erro": f"PowerShell não respondeu: {e}"}
    saida = r.stdout.decode("utf-8", errors="replace").strip().splitlines()
    for linha in reversed(saida):
        linha = linha.strip()
        if linha.startswith("{"):
            try:
                return json.loads(linha)
            except ValueError:
                break
    log.warning("AOD: saída inesperada: %.300s / %.300s", "\n".join(saida), r.stderr.decode("utf-8", "replace"))
    return {"ok": False, "erro": "Resposta inesperada da interface da BIOS."}


# ------------------------------------------------------------------ leitura
_trava = threading.Lock()
_cache: tuple[float, dict] | None = None
_rodar_ps = _powershell  # trocado nos testes


def listar(forcar: bool = False) -> dict:
    """Funções de memória que a BIOS aceita: {ok, versao, itens:[{chave, rotulo, grupo, id, atual, min, max, passo}]}."""
    global _cache
    if _cache and not forcar and time.time() - _cache[0] < 120:
        return _cache[1]
    if not windows.EH_WINDOWS:
        return {"ok": False, "motivo": "nao_windows"}
    if not windows.eh_admin():
        return {"ok": False, "motivo": "sem_admin"}
    with _trava:
        bruto = _rodar_ps(_PS_LISTAR)
    saida = interpretar(bruto)
    _cache = (time.time(), saida)
    return saida


def interpretar(bruto: dict) -> dict:
    if not bruto or not bruto.get("ok"):
        return {"ok": False, "motivo": "sem_interface", "erro": (bruto or {}).get("erro")}
    funcoes = bruto.get("funcoes") or []
    if isinstance(funcoes, dict):
        funcoes = [funcoes]
    especiais, itens = {}, []
    mclk = next((f.get("atual") for f in funcoes if _norm(f.get("nome")) == "setmemclock"), None)
    tcl = next((f.get("max") for f in funcoes if _norm(f.get("nome")) == "settcl"), None)
    ddr5 = (mclk or 0) >= 2000 or (not mclk and (tcl or 0) > 40)
    for f in funcoes:
        n = _norm(f.get("nome"))
        if n in (INICIO, FIM, RESET):
            especiais[n] = int(f["id"])
        elif n in MAPA and MAPA[n][0] in LISTAS:
            chave, rotulo, grupo = MAPA[n]
            todas = [int(x) for x in (f.get("opcoes") or []) if isinstance(x, (int, float))]
            atual = f.get("atual")
            lo, hi = faixa_segura(chave, ddr5)
            seguras = sorted({x for x in todas if lo <= x <= hi})
            if chave == "fclk" and atual == 0:
                seguras = [0] + seguras  # 0 = automático
            if len(seguras) < 2:
                continue
            itens.append({"chave": chave, "rotulo": rotulo, "grupo": grupo, "id": int(f["id"]), "nome": f.get("nome"),
                          "atual": atual, "tipo": "lista", "opcoes": seguras, "opcoes_bios": todas,
                          "min": min(x for x in seguras if x) if any(seguras) else 0, "max": max(todas), "teto": max(seguras), "passo": None})
        elif n in MAPA:
            chave, rotulo, grupo = MAPA[n]
            mn, mx, passo = f.get("min"), f.get("max"), f.get("passo")
            if mn is None or mx is None or not passo or mx <= mn or mx > 100000:
                continue  # sem faixa informada (lista de opções): o monitor não grava
            itens.append({"chave": chave, "rotulo": rotulo, "grupo": grupo, "id": int(f["id"]), "nome": f.get("nome"),
                          "atual": f.get("atual"), "min": mn, "max": mx, "passo": passo,
                          "teto": min(mx, TETO_MV[chave]) if chave in TENSOES else mx})
    pode = INICIO in especiais and FIM in especiais and bool(itens)
    itens.sort(key=lambda i: (GRUPOS.index(i["grupo"]), list(MAPA.values()).index((i["chave"], i["rotulo"], i["grupo"]))))
    return {"ok": pode, "motivo": None if pode else "sem_funcoes", "versao": bruto.get("versao"),
            "instancia": bruto.get("instancia"), "itens": itens, "pode_resetar": RESET in especiais, "_ids": especiais}


# ------------------------------------------------------------------ validação
def validar(lista: dict, mudancas: dict, atual_umc: dict | None = None, livre: bool = False) -> tuple[dict, list[str], list[str]]:
    """(valores aceitos {chave: valor}, erros que impedem, avisos). Valores iguais aos de agora são ignorados.
    livre=True (para desfazer) aceita até o máximo da BIOS, sem o teto de tensão do monitor."""
    por_chave = {i["chave"]: i for i in lista.get("itens") or []}
    aceitos, erros, avisos = {}, [], []
    for chave, v in (mudancas or {}).items():
        item = por_chave.get(chave)
        if not item:
            erros.append(f"{chave}: a sua BIOS não deixa mudar isso pelo Windows.")
            continue
        try:
            v = int(round(float(v)))
        except (TypeError, ValueError):
            erros.append(f"{item['rotulo']}: valor inválido.")
            continue
        if v == item.get("atual"):
            continue
        if item.get("tipo") == "lista":
            if v in item["opcoes"] or (livre and (v in item["opcoes_bios"] or v == 0)):
                aceitos[chave] = v
            else:
                erros.append(f"{item['rotulo']}: {_mostrar(chave, v)} não está entre as opções que o monitor deixa escolher "
                             f"({_mostrar(chave, item['min'])} a {_mostrar(chave, item['teto'])}).")
            continue
        teto = item["max"] if livre else item["teto"]
        if v < item["min"] or v > teto:
            limite = f"{item['min']} a {teto}" + (" mV" if chave in TENSOES else "")
            erros.append(f"{item['rotulo']}: {v} fora da faixa ({limite}).")
            continue
        if (v - item["min"]) % item["passo"]:
            erros.append(f"{item['rotulo']}: a BIOS só aceita de {item['passo']} em {item['passo']} a partir de {item['min']}.")
            continue
        aceitos[chave] = v

    def valor(c):  # como fica depois de gravar
        if c in aceitos:
            return aceitos[c]
        if c in por_chave and por_chave[c].get("atual"):
            return por_chave[c]["atual"]
        return (atual_umc or {}).get(c)

    tras, trp, trc = valor("tras"), valor("trp"), valor("trc")
    if aceitos.keys() & {"tras", "trp", "trc"} and tras and trp and trc and trc < tras + trp:
        erros.append(f"tRC ({trc}) tem que ser pelo menos tRAS + tRP ({tras + trp}).")
    tfaw, trrds = valor("tfaw"), valor("trrds")
    if aceitos.keys() & {"tfaw", "trrds"} and tfaw and trrds and tfaw < 4 * trrds:
        avisos.append(f"tFAW ({tfaw}) abaixo de 4 × tRRD_S ({4 * trrds}) não ganha nada.")
    vdd, vddq = valor("vdd"), valor("vddq")
    if aceitos.keys() & {"vdd", "vddq"} and vdd and vddq and abs(vdd - vddq) > 100:
        avisos.append("VDD e VDDQ muito diferentes (mais de 0,1 V): o normal é andarem juntos.")
    if any(c in aceitos for c in ("vdd", "vddq")) and max(vdd or 0, vddq or 0) > 1400:
        avisos.append("Acima de 1,40 V os pentes esquentam bastante: deixe um ventilador soprando neles.")
    if "mclk" in aceitos:
        avisos.append("Com a frequência nova a placa treina a memória do zero: o primeiro boot pode ficar 1 a 2 minutos com a tela "
                      "preta. Não desligue no meio.")
        if aceitos["mclk"] >= 3000 and vdd and vdd < 1300:
            avisos.append(f"{aceitos['mclk'] * 2} MT/s costuma pedir 1,35 V nos pentes (veja na caixa do kit); a BIOS está em {vdd / 1000:.2f} V.".replace(".", ",", 1))
    if aceitos.get("fclk", 0) > 2100:
        avisos.append("FCLK acima de 2100 MHz nem todo processador aguenta: rode o teste de estabilidade depois.")
    if aceitos.get("uclk1") == 0:
        avisos.append("Desligar o 1:1 deixa o controlador na metade da velocidade: mais latência. Só vale acima de 6400 MT/s.")
    return aceitos, erros, avisos


def _mostrar(chave: str, v) -> str:
    if chave == "mclk":
        return f"{v * 2} MT/s"
    if chave == "fclk":
        return f"{v} MHz" if v else "automático"
    if chave == "uclk1":
        return "1:1" if v else "1:2"
    return str(v)


# ------------------------------------------------------------------ gravação
def _arquivo(nome: str):
    return config.pasta_dados() / nome


def _ler_json(nome: str):
    try:
        return json.loads(_arquivo(nome).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gravar_json(nome: str, dados) -> None:
    try:
        _arquivo(nome).write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.exception("não consegui gravar %s", nome)


def _apagar(nome: str) -> None:
    try:
        _arquivo(nome).unlink()
    except OSError:
        pass


def _executar(comandos: list[tuple[int, int]]) -> dict:
    linhas = "\n".join(f"$cmds += ,@({int(i)}, {int(v)})" for i, v in comandos)
    with _trava:
        return _rodar_ps(_PS_RODAR.replace("{CMDS}", linhas), timeout=90)


def aplicar(mudancas: dict, atual_umc: dict | None = None, origem: str = "", livre: bool = False) -> dict:
    """Grava para o próximo boot. Guarda os valores de antes (para desfazer) e o pedido (para conferir depois)."""
    global _cache
    lista = listar(True)
    if not lista.get("ok"):
        return {"ok": False, "mensagem": "A BIOS não aceitou a conexão para gravar.", "lista": lista}
    aceitos, erros, avisos = validar(lista, mudancas, atual_umc, livre)
    if erros:
        return {"ok": False, "mensagem": " ".join(erros), "erros": erros, "avisos": avisos}
    if not aceitos:
        return {"ok": False, "mensagem": "Nada mudou: os valores são os que já estão na BIOS."}
    por_chave = {i["chave"]: i for i in lista["itens"]}
    ids = lista["_ids"]
    comandos = [(ids[INICIO], 0)] + [(por_chave[c]["id"], v) for c, v in aceitos.items()] + [(ids[FIM], 0)]

    pendente = _ler_json("aod_pendente.json") or {}
    antes = pendente.get("antes") or {i["chave"]: i["atual"] for i in lista["itens"] if i.get("atual") is not None}
    if not _ler_json("aod_original.json"):  # a primeira configuração vista fica guardada para sempre
        _gravar_json("aod_original.json", {"quando": time.time(), "valores": antes})
    r = _executar(comandos)
    _cache = None
    if not r.get("ok"):
        passos = r.get("passos") or []
        falha = next((p for p in passos if not p.get("ok")), None)
        por_id = {i["id"]: c for c, i in por_chave.items()}
        meio = {por_id[p["id"]]: p["valor"] for p in passos if p.get("ok") and p.get("id") in por_id}
        nome = por_chave[por_id[falha["id"]]]["rotulo"] if falha and falha.get("id") in por_id else None
        texto = "A BIOS recusou a gravação" + (f" no {nome}" if nome else "") + (f" ({falha.get('erro')})" if falha and falha.get("erro") else "") + "."
        if meio:  # parte já foi: guarda para dar para desfazer e conferir depois
            _gravar_json("aod_pendente.json", {"quando": time.time(), "boot": psutil.boot_time(),
                                               "pedido": {**pendente.get("pedido", {}), **meio}, "antes": antes,
                                               "origem": origem or pendente.get("origem", ""), "incompleto": True})
            texto += " Parte dos valores pode ter ficado gravada: toque em Voltar ao que estava antes de reiniciar."
        else:
            texto += " Nada foi gravado."
        return {"ok": False, "mensagem": texto, "passos": passos}
    depois = listar(True)
    agora = {i["chave"]: i.get("atual") for i in depois.get("itens") or []}
    registrado = {c: agora.get(c) == v for c, v in aceitos.items()}
    _gravar_json("aod_pendente.json", {"quando": time.time(), "boot": psutil.boot_time(), "pedido": {**pendente.get("pedido", {}), **aceitos},
                                       "antes": antes, "origem": origem or pendente.get("origem", "")})
    log.info("AOD: gravado para o próximo boot %s (registrado %s)", aceitos, registrado)
    return {"ok": True, "gravados": aceitos, "registrado": registrado, "avisos": avisos, "passos": r.get("passos"),
            "mensagem": "Gravado na BIOS. Vale depois de reiniciar o PC."}


def restaurar(modo: str = "antes") -> dict:
    """"antes": volta os valores de antes da última gravação; "padrao": a BIOS volta tudo da memória ao padrão dela."""
    global _cache
    lista = listar(True)
    if not lista.get("ok"):
        return {"ok": False, "mensagem": "A BIOS não respondeu."}
    if modo == "padrao":
        if not lista.get("pode_resetar"):
            return {"ok": False, "mensagem": "A sua BIOS não tem a volta ao padrão pelo Windows. Use \"Load Optimized Defaults\" na BIOS."}
        ids = lista["_ids"]
        r = _executar([(ids[INICIO], 0), (ids[RESET], 0), (ids[FIM], 0)])
        _cache = None
        if not r.get("ok"):
            return {"ok": False, "mensagem": "A BIOS recusou a volta ao padrão."}
        _gravar_json("aod_pendente.json", {"quando": time.time(), "boot": psutil.boot_time(), "pedido": {}, "antes": {},
                                           "origem": "padrão da BIOS"})
        return {"ok": True, "mensagem": "A memória volta ao padrão da BIOS depois de reiniciar."}
    pendente = _ler_json("aod_pendente.json") or {}
    antes = pendente.get("antes") or (_ler_json("aod_ultimo.json") or {}).get("antes")
    if not antes:
        return {"ok": False, "mensagem": "Não há valores de antes guardados."}
    _apagar("aod_pendente.json")
    r = aplicar(antes, origem="desfazer", livre=True)
    if r.get("ok"):
        r["mensagem"] = "Os valores de antes foram gravados de novo. Vale depois de reiniciar."
    elif "Nada mudou" in r.get("mensagem", ""):
        r = {"ok": True, "mensagem": "A BIOS já está com os valores de antes."}
    return r


def conferir_depois_do_boot(atual_umc: dict) -> dict | None:
    """Depois de reiniciar, compara o que foi pedido com o que o controlador de memória está usando."""
    pendente = _ler_json("aod_pendente.json")
    if not pendente:
        return _ler_json("aod_ultimo.json")
    if psutil.boot_time() <= pendente.get("boot", 0) + 30:
        return {"pendente": True, **pendente}  # ainda não reiniciou
    if not atual_umc or not atual_umc.get("ok"):
        return {"pendente": True, "sem_leitura": True, **pendente}
    detalhes = []
    for chave, v in (pendente.get("pedido") or {}).items():
        if chave in TENSOES or (chave == "fclk" and not v):
            continue  # o controlador não mostra a tensão; FCLK automático não tem número para comparar
        agora = _rodando(chave, atual_umc)
        if agora is not None:
            entrou = abs(agora - v) <= max(10, v * 0.01) if chave in ("mclk", "fclk") else agora == v
            detalhes.append({"chave": chave, "pedido": v, "agora": agora, "entrou": entrou})
    if not detalhes:
        resultado = "padrao" if pendente.get("origem") == "padrão da BIOS" else "sem_conferir"
    elif all(d["entrou"] for d in detalhes):
        resultado = "entrou"
    elif any(d["entrou"] for d in detalhes):
        resultado = "parcial"
    else:
        resultado = "nao_entrou"
    ultimo = {**pendente, "resultado": resultado, "detalhes": detalhes, "conferido_em": time.time(),
              "quedas": _contar(oc_cpu.quedas_desde, pendente.get("quando")), "whea": _contar(oc_cpu.whea_desde, pendente.get("quando"))}
    _gravar_json("aod_ultimo.json", ultimo)
    _apagar("aod_pendente.json")
    return ultimo


def _contar(funcao, desde) -> int:
    try:
        return int(funcao(float(desde))) if desde else 0
    except Exception:
        log.exception("AOD: não consegui contar os erros")
        return 0


_quedas_cache: tuple[float, float, dict] | None = None  # (lido em, conferido_em, contagem)


def _quedas_depois(conferido_em: float) -> dict:
    """Desligamentos sem aviso e erros de hardware depois do boot com os valores novos (lido a cada 10 min)."""
    global _quedas_cache
    if _quedas_cache and _quedas_cache[1] == conferido_em and time.time() - _quedas_cache[0] < 600:
        return _quedas_cache[2]
    contagem = {"quedas_depois": _contar(oc_cpu.quedas_desde, conferido_em), "whea_depois": _contar(oc_cpu.whea_desde, conferido_em)}
    _quedas_cache = (time.time(), conferido_em, contagem)
    return contagem


def _rodando(chave: str, umc: dict):
    """O valor que o controlador de memória está usando agora, na mesma unidade da BIOS."""
    t, c = umc.get("timings") or {}, umc.get("clocks") or {}
    if chave == "mclk":
        v = c.get("mclk") or ((umc.get("mts") or 0) / 2)
        return round(v) if v else None
    if chave == "fclk":
        return round(c["fclk"]) if c.get("fclk") else None
    if chave == "uclk1":
        m = c.get("modo_uclk")
        return None if not m else (1 if m == "1:1" else 0)
    return t.get(chave)


def estado(atual_umc: dict | None, forcar: bool = False) -> dict:
    lista = listar(forcar)
    publico = {k: v for k, v in lista.items() if not k.startswith("_")}
    conf = conferir_depois_do_boot(atual_umc or {})
    if conf and conf.get("resultado") in ("entrou", "parcial") and time.time() - conf.get("conferido_em", 0) < 3 * 86400:
        conf = {**conf, **_quedas_depois(conf["conferido_em"])}
    return {**publico, "conferencia": conf, "tem_original": bool(_ler_json("aod_original.json"))}


def reiniciar_pc() -> dict:
    if not windows.EH_WINDOWS:
        return {"ok": False, "mensagem": "Só no Windows."}
    windows.executar(["shutdown.exe", "/r", "/t", "10", "/c", "Destini: reiniciando para aplicar os timings da memória."])
    return {"ok": True, "mensagem": "O PC reinicia em 10 segundos. Salve o que estiver aberto."}
