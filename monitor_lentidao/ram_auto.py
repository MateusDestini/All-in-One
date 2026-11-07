"""Auto-tune da RAM (AMD AM4/AM5): acha o limite real do kit, timing por timing, gravando na BIOS pela AOD (aod.py).

Como funciona
1. Análise (`analisar`): junta o que se sabe do kit — fabricante e densidade do chip (SPD), ranks, quantidade de
   pentes, perfil de fábrica, timings que o controlador está usando, o que a BIOS deixa gravar, tensão atual e se há
   sensor de temperatura — e monta um plano: para cada timing, o valor de agora (conhecido como bom) e o piso que
   aquele chip costuma aguentar na tensão escolhida. O piso não é o alvo: é só até onde a busca vai.
2. Busca (uma etapa por vez: tensão, tRFC, CL, tRCD/tRP, tRAS/tRC, secundários, tREFI, FCLK): busca binária entre o
   valor bom e o piso. Cada tentativa grava na BIOS a melhor configuração conhecida mais o valor da tentativa,
   reinicia, confere se o PC caiu e se o controlador está com os valores pedidos e roda o teste de estabilidade.
   Passou: aquele valor vira o novo "bom". Falhou: a busca recua. Com 3 a 5 tentativas por timing chega ao limite.
3. Final: grava a melhor configuração, roda o teste longo. Se falhar, afrouxa um passo do último timing apertado e
   testa de novo (até 4 vezes); se nada passar, volta a BIOS ao que era antes.

O estado fica num diário em JSON (gravado com fsync) que sobrevive ao reinício. Para continuar sozinho depois do boot
como administrador, o auto-tune cria uma tarefa agendada de logon (apagada no fim). `tick()` é chamado pela bandeja e
pelo painel e avança o loop quando dá; uma trava impede dois passos ao mesmo tempo.

Proteções: só valores que a BIOS lista, no passo e dentro da faixa dela; tensão com teto do monitor (aod.TETO_MV) e
mais baixa para chips que não escalam com tensão; nunca afrouxa o que já estava rodando; para depois de 3 falhas de
boot seguidas; temperatura dos pentes acima do limite conta como falha.
"""

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

from . import aod, config, oc_cpu, ram_timings, windows

log = logging.getLogger(__name__)

DIARIO = "ram_auto_estado.json"
RELATORIO = "ram_auto_relatorio.json"
VERSAO = 3
NOME_TAREFA = r"Destini\Continuar auto-tune da RAM"

# minutos de teste por tentativa e do teste final
MODOS = {"rapido": {"tentativa": 4, "final": 20, "nome": "Rápido"},
         "completo": {"tentativa": 8, "final": 60, "nome": "Completo"}}
TEMP_MAX_PADRAO = {"DDR5": 55, "DDR4": 60}
MAX_FALHAS_BOOT = 3          # falhas de boot seguidas (caiu / não entrou) antes de parar
MAX_RECUOS_FINAL = 3         # quantas vezes o teste final pode afrouxar um passo (em todas as etapas)
ESPERA_REINICIO_S = 600      # pediu para reiniciar e o PC não reiniciou: pausa

ETAPAS = {
    "base": "Configuração base",
    "tensao": "Tensão dos pentes",
    "trfc": "tRFC (refresh)",
    "cl": "CL",
    "trcd": "tRCD / tRP",
    "tras": "tRAS / tRC",
    "sec": "Secundários",
    "trefi": "tREFI",
    "fclk": "Infinity Fabric (FCLK)",
    "final": "Teste final",
}

# ------------------------------------------------------------------ o que cada chip aguenta
# Pisos da busca (o mais apertado que se tenta) por família de chip, na tensão "vdd" (mV). Valores em ns valem para
# qualquer velocidade. Vêm do que a comunidade de overclock de memória costuma conseguir com cada die; a busca para
# antes se o kit não aguentar. "?" é chip desconhecido: pisos de Micron, o mais fraco.
PISOS = {
    "DDR5": {
        "hynix": {"vdd": 1400, "cl_ns": 9.3, "rcd_ns": 11.3, "tras": 30, "trfc_ns": 120, "trefi": 65535,
                  "nome": "SK Hynix 16 Gbit (A-die ou M-die)", "escala": "sobe bem com tensão"},
        "24g": {"vdd": 1400, "cl_ns": 10.0, "rcd_ns": 12.0, "tras": 32, "trfc_ns": 160, "trefi": 65535,
                "nome": "chip de 24 Gbit", "escala": "sobe com tensão, tRFC maior por ser denso"},
        "samsung": {"vdd": 1380, "cl_ns": 10.0, "rcd_ns": 12.7, "tras": 32, "trfc_ns": 200, "trefi": 49152,
                    "nome": "Samsung 16 Gbit", "escala": "sobe pouco com tensão"},
        "micron": {"vdd": 1350, "cl_ns": 11.3, "rcd_ns": 13.3, "tras": 36, "trfc_ns": 240, "trefi": 49152,
                   "nome": "Micron 16 Gbit", "escala": "quase não sobe com tensão"},
        "?": {"vdd": 1350, "cl_ns": 11.3, "rcd_ns": 13.3, "tras": 36, "trfc_ns": 240, "trefi": 32768,
              "nome": "chip não identificado", "escala": "sem dados: pisos de chip fraco"},
    },
    "DDR4": {
        "bdie": {"vdd": 1450, "cl_ns": 7.8, "rcd_ns": 8.3, "tras": 28, "trfc_ns": 140, "trefi": 65535,
                 "nome": "Samsung B-die", "escala": "sobe bem com tensão"},
        "hynix": {"vdd": 1400, "cl_ns": 8.9, "rcd_ns": 10.5, "tras": 32, "trfc_ns": 260, "trefi": 65535,
                  "nome": "SK Hynix (CJR/DJR)", "escala": "sobe pouco com tensão"},
        "micron": {"vdd": 1400, "cl_ns": 8.9, "rcd_ns": 10.5, "tras": 32, "trfc_ns": 280, "trefi": 65535,
                   "nome": "Micron (Rev.E/B)", "escala": "sobe pouco com tensão"},
        "?": {"vdd": 1350, "cl_ns": 9.4, "rcd_ns": 11.1, "tras": 36, "trfc_ns": 300, "trefi": 32768,
              "nome": "chip não identificado", "escala": "sem dados: pisos de chip fraco"},
    },
}
# Secundários: escadas do mais folgado ao mais apertado. Um degrau só aperta (nunca afrouxa o que já está).
ESCADA_SEC = {
    "DDR5": {
        "base": [{"trrds": 8, "trrdl": 12, "tfaw": 32, "twtrs": 4, "twtrl": 16, "twr": 72, "trtp": 18}],
        "hynix": [{"twr": 48, "trtp": 12}, {"trrdl": 8}],
        "24g": [{"twr": 48, "trtp": 12}],
        "samsung": [{"twr": 60, "trtp": 15}],
    },
    "DDR4": {
        "base": [{"trrds": 4, "trrdl": 6, "tfaw": 16, "twtrs": 4, "twtrl": 12, "twr": 12, "trtp": 6}],
        "bdie": [{"trrdl": 4, "twtrl": 8, "twr": 10, "trtp": 5}],
    },
}

_trava = threading.RLock()
_teste_auto = None  # o TesteRam iniciado pelo auto-tune neste processo (some se o programa reiniciar)


# ------------------------------------------------------------------ diário
def _arquivo(nome: str) -> Path:
    return config.pasta_dados() / nome


def _ler_diario() -> dict | None:
    try:
        d = json.loads(_arquivo(DIARIO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("versao") != VERSAO:
        return None  # diário de uma versão anterior do auto-tune: ignora
    return d


def _gravar_diario(d: dict) -> None:
    """Escrita atômica com fsync: o PC reinicia logo depois de gravar."""
    caminho = _arquivo(DIARIO)
    tmp = caminho.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
    except OSError:
        log.exception("falha ao gravar o diário do auto-tune")


def _apagar_diario() -> None:
    try:
        _arquivo(DIARIO).unlink(missing_ok=True)
    except OSError:
        pass


def ativo() -> bool:
    """Há um auto-tune em andamento (inclui pausado)?"""
    d = _ler_diario()
    return bool(d and d.get("fase") not in (None, "ocioso", "final"))


# ------------------------------------------------------------------ análise do kit
def _familia(chip: dict | None, tipo: str, gbit: int | None) -> str:
    fam = ram_timings._familia_chip(chip or {}, tipo)
    if tipo == "DDR5" and gbit and gbit >= 24:
        return "24g"
    return fam if fam in PISOS[tipo] else "?"


def _ranks(r: dict, pentes: list[dict]) -> int | None:
    for p in pentes:
        s = p.get("spd") or {}
        if s.get("ranks"):
            return int(s["ranks"])
    v = r.get("ranks")
    return int(v) if v else None


def _passo(item: dict) -> int:
    return int(item.get("passo") or 1)


def _ajustar(item: dict, v: float, para: str = "perto") -> int:
    """Valor no passo e dentro da faixa da BIOS (e do teto do monitor). para="cima" arredonda para o mais folgado."""
    passo = _passo(item)
    base = item["min"]
    n = (v - base) / passo
    n = math.ceil(n - 1e-9) if para == "cima" else math.floor(n + 1e-9) if para == "baixo" else round(n)
    return int(min(item["teto"], max(item["min"], base + n * passo)))


def _candidatos(atual: int, piso: int, item: dict, n_max: int = 10) -> list[int]:
    """Do atual (índice 0, conhecido como bom) até o piso, descendo no passo da BIOS. Com muitos valores possíveis
    (tRFC) espaça para no máximo n_max+1, sempre com o piso no fim."""
    passo = _passo(item)
    piso = max(piso, item["min"])
    if atual <= piso:
        return [atual]
    valores = list(range(atual, piso - 1, -passo))
    if valores[-1] != piso and (piso - item["min"]) % passo == 0:
        valores.append(piso)
    if len(valores) > n_max + 1:
        idx = sorted({round(i * (len(valores) - 1) / n_max) for i in range(n_max + 1)})
        valores = [valores[i] for i in idx]
    return valores


def _buscas_estimadas(n: int) -> int:
    return 0 if n <= 1 else math.ceil(math.log2(n))


def analisar(forcar: bool = False) -> dict:
    """O plano do auto-tune para este PC: o que foi lido do kit, a família do chip, os pisos e as etapas.
    {ok, motivo, kit:{...}, etapas:[{id, nome, chave(s), atual, piso, candidatos, texto}], ...}"""
    plat = ram_timings.plataforma()
    k = ram_timings.kit(forcar)
    r = k.get("resumo") or {}
    atual = ram_timings.leitura_atual(forcar)
    lista = aod.listar(forcar)
    saida = {"ok": False, "plataforma": {k2: plat.get(k2) for k2 in ("nome", "geracao", "soquete", "ddr")}}

    if plat.get("marca") != "amd" or plat.get("soquete") not in ("AM4", "AM5"):
        return {**saida, "motivo": "O auto-tune grava pela interface AOD das placas AMD AM4/AM5. Este processador não é um deles."}
    if not windows.EH_WINDOWS:
        return {**saida, "motivo": "O auto-tune só funciona no Windows."}
    if not windows.eh_admin():
        return {**saida, "motivo": "Abra o monitor como administrador para o auto-tune ler a BIOS e o controlador de memória."}
    if not lista.get("ok"):
        return {**saida, "motivo": "A interface AOD da BIOS não respondeu: esta placa não deixa gravar a memória pelo Windows."}
    tipo = r.get("tipo") or plat.get("ddr")
    if tipo not in PISOS:
        return {**saida, "motivo": "Não reconheci o tipo de memória."}

    itens = {i["chave"]: i for i in lista.get("itens") or []}
    lido = bool(atual.get("ok"))
    t = (atual.get("timings") or {}) if lido else {}
    pentes = k.get("pentes") or []
    spd = r.get("spd") or {}
    gbit = spd.get("gbit")
    fam = _familia(r.get("chip"), tipo, gbit)
    pisos = PISOS[tipo][fam]
    ranks = _ranks(r, pentes)
    qtd = r.get("quantidade") or len(pentes) or 0
    dois_por_canal = qtd >= 4
    largura = spd.get("largura")
    folga = 1.0 + (0.05 if (ranks or 1) >= 2 else 0) + (0.08 if dois_por_canal else 0) + (0.05 if largura == 16 else 0)
    avisos = []
    if largura == 16:
        avisos.append("Chips x16 (pentes de 8 GB): eles apertam menos que os x8, os pisos ficam mais folgados.")
    if not lido:
        avisos.append("Sem leitura do controlador de memória (PawnIO): o auto-tune não consegue conferir se cada valor entrou "
                      "depois do boot e confia só no teste de estabilidade.")
    if fam == "?":
        avisos.append("Não deu para saber o fabricante do chip: os pisos são os de um chip fraco. A busca para antes se "
                      "o kit não aguentar, então é seguro, só pode deixar desempenho na mesa.")
    if dois_por_canal:
        avisos.append("Quatro pentes: o controlador trabalha mais. Pisos mais folgados e o FCLK não sobe.")
    if (ranks or 1) >= 2:
        avisos.append("Pentes dual rank: pisos um pouco mais folgados.")
    if r.get("quantidade") and not r.get("iguais"):
        avisos.append("Pentes de modelos diferentes: o limite é o do pior pente.")
    sensor = bool(ram_timings.temperatura_pentes())
    if not sensor and tipo == "DDR5":
        avisos.append("Os pentes não informam temperatura: o tREFI fica de fora (ele é o timing que mais erra com calor).")

    # a velocidade e o perfil de base são os do nível "Perfil do fabricante" das sugestões
    sug = ram_timings.sugestoes(k, atual, plat)
    nivel1 = next((n for n in sug.get("niveis") or [] if n["id"] == "perfil"), None) or {}
    base_vals = dict(nivel1.get("valores") or {})
    alvo_mts = sug.get("alvo_mts") or atual.get("mts") or r.get("perfil_mts") or (6000 if tipo == "DDR5" else 3600)
    if lido and abs((atual.get("mts") or 0) - alvo_mts) <= 20:
        # já roda na velocidade alvo: não afrouxa nada do que já está valendo
        for c in ("cl", "trcd", "trp"):
            if c in base_vals and t.get(c):
                base_vals[c] = min(base_vals[c], t[c])
    ck = 2000 / alvo_mts

    def agora(chave):
        """Valor bom de partida: o do controlador quando ele roda na velocidade alvo, senão o da BIOS."""
        if chave in base_vals:
            return base_vals[chave]
        if lido and atual.get("mts") and abs(atual["mts"] - alvo_mts) <= 20 and t.get(chave):
            return int(t[chave])
        v = (itens.get(chave) or {}).get("atual")
        return int(v) if v else None

    etapas = []

    def etapa(id_, chaves, cand, texto, ns=False, extra=None):
        if len(cand) < 2:
            return
        item = itens.get(chaves[0]) or {}
        etapas.append({"id": id_, "nome": ETAPAS[id_], "chaves": chaves, "candidatos": cand, "texto": texto,
                       "atual": cand[0], "piso": cand[-1], "ns": ns, "passo": _passo(item) if item.get("passo") else None,
                       "min_bios": item.get("min"), "tentativas_est": _buscas_estimadas(len(cand)), **(extra or {})})

    # tensão: sobe direto ao valor do chip (uma tentativa), só se estiver abaixo
    vdd_alvo = None
    if "vdd" in itens:
        vdd_agora = itens["vdd"].get("atual") or 0
        vdd_alvo = _ajustar(itens["vdd"], min(pisos["vdd"], aod.TETO_MV["vdd"]), "baixo")
        if vdd_agora and vdd_alvo > vdd_agora:
            cand = [{"vdd": vdd_agora}, {"vdd": vdd_alvo}]
            if "vddq" in itens:
                vq = itens["vddq"].get("atual") or vdd_agora
                cand = [{"vdd": vdd_agora, "vddq": vq}, {"vdd": vdd_alvo, "vddq": _ajustar(itens["vddq"], vdd_alvo, "baixo")}]
            etapas.append({"id": "tensao", "nome": ETAPAS["tensao"], "chaves": list(cand[1]), "candidatos": cand,
                           "atual": cand[0], "piso": cand[1], "tentativas_est": 1, "grupo": True,
                           "texto": f"{vdd_agora / 1000:.2f} V → {vdd_alvo / 1000:.2f} V. Apertar CL e tRCD pede tensão; "
                                    f"{pisos['nome']} {pisos['escala']}.".replace(".", ",", 2)})
        else:
            vdd_alvo = vdd_agora or None

    def simples(id_, chave, piso_clk, texto, n_max=10, ns=True):
        item, v = itens.get(chave), agora(chave)
        if not item or not v:
            return
        piso = _ajustar(item, piso_clk, "cima")
        etapa(id_, [chave], _candidatos(_ajustar(item, v, "cima"), piso, item, n_max), texto, ns)

    # tRFC: o que mais rende banda
    trfc_piso = math.ceil(pisos["trfc_ns"] * folga / ck)
    simples("trfc", "trfc", trfc_piso, f"Pausa a cada refresh. Piso {round(pisos['trfc_ns'] * folga)} ns para {pisos['nome']}.")
    for e in etapas:
        if e["id"] == "trfc":
            e["refinar"] = True  # os candidatos são espaçados: no fim, busca fina entre o último bom e o que falhou
            e["tentativas_est"] += 2
    # CL: piso em ns convertido para a velocidade alvo
    cl_piso = math.ceil(pisos["cl_ns"] * folga / ck - 0.01)
    if tipo == "DDR5" or (itens.get("cl") and _passo(itens["cl"]) == 2):
        cl_piso += cl_piso & 1  # DDR5 / BIOS de passo 2: CL par
    simples("cl", "cl", cl_piso, f"Latência de coluna. Piso CL{cl_piso} ({fmt(cl_piso * ck)} ns).", ns=True)
    # tRCD e tRP juntos
    if "trcd" in itens and "trp" in itens and agora("trcd") and agora("trp"):
        rcd_piso = math.ceil(pisos["rcd_ns"] * folga / ck - 0.01)
        it = itens["trcd"]
        partida = max(_ajustar(it, agora("trcd"), "cima"), _ajustar(itens["trp"], agora("trp"), "cima"))
        cand = _candidatos(partida, _ajustar(it, rcd_piso, "cima"), it, 10)
        etapa("trcd", ["trcd", "trp"], cand, f"Abrir e fechar linha. Piso {rcd_piso} ({fmt(rcd_piso * ck)} ns).", True)
    # tRAS (o tRC acompanha: tRAS + tRP)
    if "tras" in itens and agora("tras"):
        tras_piso = math.ceil(pisos["tras"] * folga)
        etapa("tras", ["tras", "trc"] if "trc" in itens else ["tras"],
              _candidatos(_ajustar(itens["tras"], agora("tras"), "cima"), _ajustar(itens["tras"], tras_piso, "cima"), itens["tras"], 4),
              f"Pesa pouco; o tRC vai junto (tRAS + tRP). Piso {tras_piso}.")
    # secundários: escada de degraus
    degraus = ESCADA_SEC[tipo]["base"] + ESCADA_SEC[tipo].get(fam, [])
    if any(c in itens for d in degraus for c in d):
        escada, acumulado = [{}], {}
        for d in degraus:
            acumulado = {**acumulado, **{c: v for c, v in d.items() if c in itens}}
            passo_ = {}
            for c, v in acumulado.items():
                partida = agora(c)
                if partida and v < partida:
                    passo_[c] = _ajustar(itens[c], v, "cima")
            if passo_ and passo_ != escada[-1]:
                escada.append(passo_)
        if len(escada) > 1:
            etapas.append({"id": "sec", "nome": ETAPAS["sec"], "chaves": sorted({c for d in escada for c in d}),
                           "candidatos": escada, "atual": {}, "piso": escada[-1], "grupo": True,
                           "tentativas_est": _buscas_estimadas(len(escada)),
                           "texto": "tRRD, tFAW, tWTR, tWR e tRTP em degraus: primeiro os que quase todo kit aguenta, "
                                    "depois os do chip."})
    # tREFI: maior é melhor (sobe); só com sensor de temperatura
    if "trefi" in itens and agora("trefi") and (sensor or tipo == "DDR4"):
        it, v = itens["trefi"], agora("trefi")
        teto = min(it["teto"], pisos["trefi"])
        cand = [v] + [x for x in (16384, 32768, 49152, 65535) if v < x <= teto]
        cand = [_ajustar(it, x, "baixo") for x in cand]
        cand = sorted(set(cand), key=cand.index)
        etapa("trefi", ["trefi"], cand, f"Refresh menos frequente: mais banda, mas erra com calor. Teto {teto}.")
    # FCLK (AM5): sobe até 2100 MHz, com dois pentes
    if plat.get("soquete") == "AM5" and "fclk" in itens and not dois_por_canal:
        it = itens["fclk"]
        v = agora("fclk") if "fclk" in base_vals else it.get("atual")
        efetivo = v or 2000
        acima = [o for o in it.get("opcoes") or [] if efetivo < o <= 2100]
        if acima:
            etapa("fclk", ["fclk"], [v if v is not None else 0] + sorted(acima),
                  "Infinity Fabric: cada 33 MHz tira perto de 1 ns de latência. Erros aparecem como WHEA.")

    for e in etapas:
        e["tentativas_est"] = max(1, e.get("tentativas_est") or 1)
    tentativas = 1 + sum(e["tentativas_est"] for e in etapas)
    chip = r.get("chip") or {}
    return {
        "ok": True, "tipo": tipo, "familia": fam, "pisos_nome": pisos["nome"], "escala": pisos["escala"],
        "alvo_mts": alvo_mts, "base": base_vals, "vdd_alvo": vdd_alvo, "etapas": etapas, "avisos": avisos,
        "tentativas_estimadas": tentativas, "plataforma": saida["plataforma"],
        "kit": {"marca": r.get("marca"), "modelo": r.get("modelo"), "quantidade": qtd, "gb_pente": r.get("gb_pente"),
                "perfil": f"{r['perfil_mts']} CL{r['perfil_cl']}" if r.get("perfil_mts") and r.get("perfil_cl") else None,
                "chip": chip.get("nome"), "chip_confianca": chip.get("confianca"), "chip_spd": bool(chip.get("spd")),
                "gbit": gbit, "ranks": ranks, "largura": largura, "sensor_temperatura": sensor,
                "rodando": (f"{atual['mts']} CL{t.get('cl')}-{t.get('trcd')}-{t.get('trp')}-{t.get('tras')}"
                            if lido else None),
                "trfc_ns": t.get("trfc_ns"), "vdd": (itens.get("vdd") or {}).get("atual")},
        "itens_bios": sorted(itens),
    }


def fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def estimativa_minutos(plano: dict, modo: str) -> int:
    m = MODOS.get(modo, MODOS["rapido"])
    boot = 3  # reinício + treino da memória + o monitor subir
    return round(plano.get("tentativas_estimadas", 1) * (m["tentativa"] + boot + 1) + m["final"] + boot)


# ------------------------------------------------------------------ tarefa de logon (continuar como administrador)
def _comando_tarefa() -> tuple[str, str, str]:
    if getattr(sys, "frozen", False):
        return sys.executable, "--bandeja --ram-auto", str(Path(sys.executable).parent)
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return (str(pyw if pyw.exists() else py), "-m monitor_lentidao --bandeja --ram-auto",
            str(Path(__file__).resolve().parent.parent))


def _xml_tarefa() -> str:
    from xml.sax.saxutils import escape
    usuario = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    exe, args, pasta = _comando_tarefa()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Continua o auto-tune da memória do Destini · Monitor de Lentidão depois de reiniciar. Some sozinha quando o auto-tune acaba.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(usuario)}</UserId><Delay>PT15S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(usuario)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>6</Priority>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(exe)}</Command><Arguments>{escape(args)}</Arguments><WorkingDirectory>{escape(pasta)}</WorkingDirectory></Exec></Actions>
</Task>
"""


def _schtasks(*args: str) -> bool:
    if not windows.EH_WINDOWS:
        return False
    try:
        return subprocess.run(["schtasks.exe", *args], capture_output=True, timeout=30,
                              creationflags=windows.CREATE_NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def tarefa_existe() -> bool:
    return _schtasks("/Query", "/TN", NOME_TAREFA)


def _criar_tarefa() -> bool:
    arquivo = _arquivo("tarefa-ram-auto.xml")
    try:
        arquivo.write_text(_xml_tarefa(), encoding="utf-16")
    except OSError:
        return False
    return _schtasks("/Create", "/F", "/TN", NOME_TAREFA, "/XML", str(arquivo))


def _apagar_tarefa() -> None:
    if windows.EH_WINDOWS and windows.eh_admin():
        _schtasks("/Delete", "/F", "/TN", NOME_TAREFA)


def deve_ceder_ao_iniciar() -> bool:
    """Chamado pelo __main__ ao abrir sem administrador junto com o Windows: se há auto-tune em andamento e a tarefa
    de logon existe, esta cópia sai e deixa a da tarefa (administrador) continuar."""
    try:
        return windows.EH_WINDOWS and not windows.eh_admin() and ativo() and tarefa_existe()
    except Exception:
        return False


# ------------------------------------------------------------------ máquina de estados
def iniciar(opcoes: dict | None = None) -> dict:
    """Começa o auto-tune. opcoes = {modo: rapido|completo, margem: bool}. {ok, mensagem}"""
    opcoes = opcoes if isinstance(opcoes, dict) else {}
    modo = opcoes.get("modo") if opcoes.get("modo") in MODOS else "rapido"
    margem = bool(opcoes.get("margem"))
    with _trava:
        if ativo():
            return {"ok": False, "mensagem": "O auto-tune já está em andamento. Cancele antes de começar outro."}
        if ram_timings.em_uso():
            return {"ok": False, "mensagem": "Um teste de RAM já está rodando. Espere terminar."}
        if aod._ler_json("aod_pendente.json"):
            return {"ok": False, "mensagem": "Há uma gravação na BIOS esperando o reinício. Reinicie ou desfaça antes de começar."}
        plano = analisar(True)
        if not plano.get("ok"):
            return {"ok": False, "mensagem": plano.get("motivo") or "Não deu para planejar o auto-tune."}
        lista = aod.listar(True)
        # tudo o que a BIOS deixa gravar, como está agora: é para isso que o cancelar volta
        original = {i["chave"]: i["atual"] for i in lista.get("itens") or [] if i.get("atual") is not None}
        if not _criar_tarefa():
            return {"ok": False, "mensagem": "O Windows não criou a tarefa que continua o auto-tune depois de reiniciar."}
        etapas = [{"id": "base", "nome": ETAPAS["base"], "chaves": sorted(plano["base"]), "candidatos": [plano["base"]],
                   "grupo": True, "lo": 0, "hi": 0, "unica": True}]
        for e in plano["etapas"]:
            etapas.append({**e, "lo": 0, "hi": len(e["candidatos"]) - 1})
        d = {"versao": VERSAO, "inicio": time.time(), "modo": modo, "margem": margem, "fase": "aplicar",
             "plano": {k: plano[k] for k in ("tipo", "familia", "pisos_nome", "alvo_mts", "kit", "avisos")},
             "etapas": etapas, "i_etapa": 0, "melhor": dict(original), "original": original, "tentativa": None,
             "tentativas": [], "pausa": None, "reboots": 0, "falhas_boot": 0, "recuos_final": 0, "inicial": None}
        _gravar_diario(d)
        _proxima_tentativa(d)
        if d["fase"] == "pausado":
            return {"ok": False, "mensagem": (d.get("pausa") or {}).get("motivo") or "O auto-tune parou no primeiro passo."}
        return {"ok": True, "mensagem": "Auto-tune iniciado. O PC reinicia em 10 segundos se algo foi gravado; "
                                        "depois do boot ele continua sozinho."}


def _valores_candidato(e: dict, i: int, melhor: dict) -> dict:
    c = e["candidatos"][i]
    if e.get("grupo"):
        return dict(c)
    v = {ch: c for ch in e["chaves"] if ch != "trc"}
    if "trc" in e["chaves"]:  # tRC = tRAS + tRP
        v["trc"] = c + int(melhor.get("trp") or 0) if melhor.get("trp") else None
        if v["trc"] is None:
            v.pop("trc")
    return v


def _proximo_indice(e: dict) -> int | None:
    """Busca binária: lo é o melhor que passou, hi o mais apertado ainda possível."""
    if e.get("unica"):
        return 0 if not e.get("feita") else None
    if e["lo"] >= e["hi"]:
        return None
    return (e["lo"] + e["hi"] + 1) // 2


def _proxima_tentativa(d: dict) -> None:
    """Escolhe a próxima tentativa (ou a próxima etapa, ou o final) e grava."""
    while d["i_etapa"] < len(d["etapas"]):
        e = d["etapas"][d["i_etapa"]]
        i = _proximo_indice(e)
        if i is not None:
            valores = _valores_candidato(e, i, d["melhor"])
            _tentar(d, {"etapa": e["id"], "indice": i, "valores": valores, "minutos": MODOS[d["modo"]]["tentativa"]})
            return
        d["i_etapa"] += 1
    _comecar_final(d)


def _comecar_final(d: dict) -> None:
    if d.get("margem") and not d.get("margem_aplicada"):
        d["margem_aplicada"] = True
        for e in d["etapas"]:
            if not e.get("unica") and e["lo"] > 0 and e["id"] != "tensao":
                e["lo"] -= 1
                d["melhor"].update(_valores_candidato(e, e["lo"], d["melhor"]))
    _tentar(d, {"etapa": "final", "indice": 0, "valores": {}, "minutos": MODOS[d["modo"]]["final"]})


def _ajustar_valores(valores: dict, lista: dict) -> dict:
    """Só o que a BIOS lista, no passo e dentro da faixa dela. Opções de lista (frequência, FCLK, UCLK) vão para a
    opção mais próxima; o FCLK 0 (automático) só fica se a BIOS estiver nele."""
    por_chave = {i["chave"]: i for i in lista.get("itens") or []}
    saida = {}
    for k, v in (valores or {}).items():
        i = por_chave.get(k)
        if not i or v is None:
            continue
        if i.get("tipo") == "lista":
            opcoes = list(i.get("opcoes_bios") or i.get("opcoes") or [])
            if v in opcoes or (k == "fclk" and v == 0) or (k == "uclk1" and v in (0, 1)):
                saida[k] = v
            elif opcoes:
                saida[k] = min((o for o in opcoes if o or k == "uclk1"), key=lambda o: abs(o - v))
        else:
            teto = max(i["teto"], v) if k in aod.TENSOES and v <= i["max"] else i["teto"]  # tensão de antes: mantém
            passo = _passo(i)
            x = i["min"] + round((v - i["min"]) / passo) * passo
            saida[k] = int(min(teto, max(i["min"], x)))
    return saida


def _tentar(d: dict, tentativa: dict) -> None:
    """Grava melhor + valores da tentativa e reinicia (ou vai direto ao teste se nada mudou)."""
    lista = aod.listar(True)
    if not lista.get("ok"):
        d["tentativa"] = tentativa
        _pausar(d, "A BIOS parou de responder pela interface AOD.")
        return
    desejado = _ajustar_valores({**d["melhor"], **tentativa["valores"]}, lista)
    tras, trp, trc = desejado.get("tras"), desejado.get("trp"), desejado.get("trc")
    if tras and trp and trc and trc < tras + trp:  # tRC nunca abaixo de tRAS + tRP (a BIOS e o controlador exigem)
        desejado["trc"] = _ajustar_valores({"trc": tras + trp}, lista).get("trc", tras + trp)
        if desejado["trc"] < tras + trp:
            desejado["trc"] += next((_passo(i) for i in lista.get("itens") or [] if i["chave"] == "trc"), 1)
    tentativa["valores"] = {k: desejado[k] for k in tentativa["valores"] if k in desejado}
    tentativa.update(quando=time.time(), desejado=desejado)
    d["tentativa"] = tentativa
    d["fase"] = "aplicar"
    _gravar_diario(d)
    atual = ram_timings.leitura_atual(True)
    # livre: os valores já vêm presos ao teto do monitor; o que passa dele é o que já estava na BIOS antes
    r = aod.aplicar(desejado, atual.get("timings") if atual.get("ok") else None,
                    origem=f"auto-tune: {ETAPAS.get(tentativa['etapa'], tentativa['etapa'])}", livre=True)
    if not r.get("ok"):
        if "Nada mudou" in (r.get("mensagem") or ""):
            d["fase"] = "teste"
            _gravar_diario(d)
            _iniciar_teste(d)
            return
        if tentativa["etapa"] not in ("base", "final"):
            # a BIOS recusou este valor (fora do que ela aceita): conta como falha sem reiniciar
            _resultado(d, False, {"motivo": "bios_recusou", "texto": r.get("mensagem")}, reboot=False)
            return
        _pausar(d, f"A BIOS não aceitou a gravação: {r.get('mensagem', '')}")
        return
    d["reboots"] = d.get("reboots", 0) + 1
    d["fase"] = "aguardando_boot"
    tentativa["pedido_reinicio"] = time.time()
    _gravar_diario(d)
    aod.reiniciar_pc()


def _pausar(d: dict, motivo: str) -> None:
    d["pausa"] = {"motivo": motivo, "quando": time.time()}
    d["fase"] = "pausado"
    _gravar_diario(d)


def _registrar(d: dict, ok: bool, info: dict) -> dict:
    t = d.get("tentativa") or {}
    item = {"etapa": t.get("etapa"), "valores": t.get("valores"), "ok": ok, "quando": time.time(),
            "minutos": t.get("minutos"), **{k: v for k, v in info.items() if k in
                                            ("motivo", "texto", "latencia", "leitura", "erros", "whea", "temp_max",
                                             "quedas", "detalhes")}}
    d.setdefault("tentativas", []).append(item)
    d["tentativas"] = d["tentativas"][-80:]
    return item


def _resultado(d: dict, ok: bool, info: dict, reboot: bool = True) -> str | None:
    """Fecha a tentativa: atualiza a busca e segue."""
    t = d.get("tentativa") or {}
    _registrar(d, ok, info)
    etapa_id = t.get("etapa")
    if info.get("motivo") in ("crash_ou_no_post", "timings_nao_entrou", "timings_parcial"):
        d["falhas_boot"] = d.get("falhas_boot", 0) + 1
    elif ok:
        d["falhas_boot"] = 0

    if etapa_id == "final":
        if ok:
            _finalizar(d, True)
            return f"Auto-tune concluído: {resumo_final(d)}"
        if d.get("recuos_final", 0) < MAX_RECUOS_FINAL and _recuar_um_passo(d):
            d["recuos_final"] = d.get("recuos_final", 0) + 1
            _gravar_diario(d)
            _comecar_final(d)
            return "Auto-tune: o teste longo achou erro. Afrouxei um passo em cada timing e vou testar de novo."
        # nada passou no teste longo: volta ao que era antes
        d["melhor"] = dict(d["original"])
        _finalizar(d, False)
        r = aod.aplicar(d["original"], origem="auto-tune: voltar ao original", livre=True)
        if r.get("ok"):
            aod.reiniciar_pc()
        return "Auto-tune: a configuração não passou no teste longo. A BIOS voltou ao que era antes."

    e = next((x for x in d["etapas"] if x["id"] == etapa_id), None)
    if e is None:
        _pausar(d, "Diário do auto-tune inconsistente.")
        return None
    if etapa_id == "base":
        e["feita"] = True
        if not ok:
            d["melhor"] = dict(d["original"])
            _finalizar(d, False)
            if reboot:
                r = aod.aplicar(d["original"], origem="auto-tune: voltar ao original", livre=True)
                if r.get("ok"):
                    aod.reiniciar_pc()
            return ("Auto-tune: nem a configuração base (o perfil do kit) passou no teste. Não dá para apertar a partir "
                    "dela: confira os pentes e a BIOS. Os valores de antes foram regravados.")
        d["melhor"].update(t.get("valores") or {})
        e["candidatos"] = [t.get("valores") or {}]  # o que de fato foi gravado (no passo e nas opções da BIOS)
        d["inicial"] = {k: info.get(k) for k in ("latencia", "leitura")}
        _replanejar(d)
    elif ok:
        e["lo"] = t["indice"]
        d["melhor"].update(t.get("valores") or {})
    else:
        e["hi"] = t["indice"] - 1
        if not e.get("grupo"):
            e["ruim"] = e["candidatos"][t["indice"]] if e.get("ruim") is None else max(e["ruim"], e["candidatos"][t["indice"]])
    _refinar(e)
    d["tentativa"] = None
    if d.get("falhas_boot", 0) >= MAX_FALHAS_BOOT:
        _pausar(d, f"{MAX_FALHAS_BOOT} boots seguidos falharam. Parei por segurança: confira se a placa voltou ao "
                   "normal e toque em Continuar.")
        return "Auto-tune pausado: falhas de boot seguidas."
    _gravar_diario(d)
    _proxima_tentativa(d)
    nome = ETAPAS.get(etapa_id, etapa_id)
    return None if ok else f"Auto-tune: {nome} não aguentou {_texto_valores(t.get('valores'))}. Seguindo com o último bom."


def _replanejar(d: dict) -> None:
    """Depois que a base passou, o controlador já roda na velocidade alvo: o plano é refeito a partir dos timings que
    a placa escolheu nessa velocidade (com o perfil desligado antes, os de partida eram só estimativa)."""
    try:
        novo = analisar(True)
    except Exception:
        log.exception("auto-tune: não consegui refazer o plano; sigo com o primeiro")
        return
    if not novo.get("ok"):
        return
    base = d["etapas"][0]
    d["etapas"] = [base] + [{**e, "lo": 0, "hi": len(e["candidatos"]) - 1} for e in novo["etapas"]]
    d["i_etapa"] = 1
    for c, v in (novo.get("base") or {}).items():
        d["melhor"].setdefault(c, v)


def _refinar(e: dict) -> None:
    """Depois da busca grossa, se entre o último bom e o primeiro que falhou ainda cabem vários passos da BIOS, troca
    os candidatos por esse intervalo, em passos finos, e continua a busca (uma vez por etapa)."""
    if not e.get("refinar") or e.get("refinado") or e["lo"] < e["hi"] or e.get("ruim") is None:
        return
    bom, ruim, passo = e["candidatos"][e["lo"]], e["ruim"], e.get("passo") or 1
    if bom - ruim <= 2 * passo:
        return
    item = {"passo": passo, "min": e.get("min_bios") or 0}
    novos = _candidatos(bom, ruim + passo, item, 4)
    if len(novos) < 2:
        return
    e["refinado"] = True
    e["grossos"] = e["candidatos"]
    e["candidatos"], e["lo"], e["hi"] = novos, 0, len(novos) - 1


def _recuar_um_passo(d: dict) -> bool:
    """O teste longo achou erro, mas não diz em qual timing: afrouxa um passo em todas as etapas que apertaram algo
    (o resultado fica um pouco menos apertado, mas estável). False se não há mais o que afrouxar."""
    mexeu = False
    for e in d["etapas"]:
        if e.get("unica") or e["lo"] <= 0 or e["id"] == "tensao":  # a tensão é o que deixa apertar: fica
            continue
        e["lo"] -= 1
        e["hi"] = e["lo"]
        mexeu = True
        if e["lo"] == 0 and e.get("grupo") and e["id"] == "sec":
            # voltou ao degrau "como estava": devolve os valores originais dessas chaves
            for c in e["chaves"]:
                if c in d["original"]:
                    d["melhor"][c] = d["original"][c]
        else:
            d["melhor"].update(_valores_candidato(e, e["lo"], d["melhor"]))
    return mexeu


def _texto_valores(v: dict | None) -> str:
    if not v:
        return ""
    nomes = {"cl": "CL", "trcd": "tRCD", "trp": "tRP", "tras": "tRAS", "trc": "tRC", "trfc": "tRFC", "trefi": "tREFI",
             "vdd": "VDD", "vddq": "VDDQ", "fclk": "FCLK", "trrds": "tRRDS", "trrdl": "tRRDL", "tfaw": "tFAW",
             "twtrs": "tWTRS", "twtrl": "tWTRL", "twr": "tWR", "trtp": "tRTP", "mclk": "MCLK", "uclk1": "UCLK"}
    partes = []
    for k2, x in v.items():
        if k2 in ("vdd", "vddq"):
            partes.append(f"{nomes[k2]} {x / 1000:.2f} V".replace(".", ","))
        else:
            partes.append(f"{nomes.get(k2, k2)} {x}")
    return ", ".join(partes)


def tick() -> str | None:
    """Avança o loop quando dá. Chamado pela bandeja e pelo painel; devolve um texto para notificar, ou None."""
    if not _trava.acquire(blocking=False):
        return None
    try:
        d = _ler_diario()
        if not d or d.get("fase") in (None, "ocioso", "final", "pausado"):
            return None
        if not windows.eh_admin():
            return None  # a cópia administradora (tarefa de logon) é quem continua
        fase = d["fase"]
        if fase == "aguardando_boot":
            return _depois_do_boot(d)
        if fase == "teste":
            return _verificar_teste(d)
        if fase == "aplicar":
            # o programa fechou entre gravar e reiniciar
            _pausar(d, "O auto-tune foi interrompido antes de reiniciar. Toque em Continuar para gravar de novo.")
        return None
    except Exception:
        log.exception("auto-tune da RAM: falha num passo")
        return None
    finally:
        _trava.release()


def verificar_estado() -> str | None:
    """Compatibilidade: chamado na inicialização."""
    return tick()


def _depois_do_boot(d: dict) -> str | None:
    t = d.get("tentativa") or {}
    desde = float(t.get("pedido_reinicio") or t.get("quando") or d.get("inicio") or 0)
    if psutil.boot_time() <= desde:
        if time.time() - desde > ESPERA_REINICIO_S:
            _pausar(d, "O PC não reiniciou depois da gravação (o reinício foi cancelado?). Toque em Continuar.")
            return "Auto-tune pausado: o PC não reiniciou."
        return None
    atual = ram_timings.leitura_atual(True)
    if not atual.get("ok") and time.time() - psutil.boot_time() < 180:
        return None  # PawnIO e sensores ainda subindo: tenta na próxima volta
    quedas = oc_cpu.quedas_desde(desde)
    if quedas:
        aod.conferir_depois_do_boot(atual if atual.get("ok") else {})
        return _resultado(d, False, {"motivo": "crash_ou_no_post", "quedas": quedas,
                                     "texto": "o PC travou ou desligou sozinho"})
    if atual.get("ok"):
        conf = aod.conferir_depois_do_boot(atual) or {}
        if conf.get("pendente"):
            return None
        resultado = conf.get("resultado")
        if resultado in ("parcial", "nao_entrou"):
            return _resultado(d, False, {"motivo": f"timings_{resultado}", "detalhes": conf.get("detalhes"),
                                         "texto": "a placa não aceitou os valores no treino da memória"})
    else:
        # sem leitura do controlador: não dá para conferir, mas o teste de estabilidade ainda vale
        p = aod._ler_json("aod_pendente.json")
        if p:
            aod._gravar_json("aod_ultimo.json", {**p, "resultado": "sem_conferir", "detalhes": [], "conferido_em": time.time()})
            aod._apagar("aod_pendente.json")
    d["fase"] = "teste"
    _gravar_diario(d)
    _iniciar_teste(d)
    return None


def _iniciar_teste(d: dict) -> None:
    global _teste_auto
    t = d.get("tentativa") or {}
    minutos = float(t.get("minutos") or MODOS[d["modo"]]["tentativa"])
    rotulo = f"auto-tune: {ETAPAS.get(t.get('etapa'), t.get('etapa'))} {_texto_valores(t.get('valores'))}".strip()
    r = ram_timings.iniciar_teste(minutos, rotulo=rotulo[:60], medir=True)
    if not r.get("ok"):
        _pausar(d, f"Não consegui iniciar o teste de estabilidade: {r.get('mensagem', '')}")
        return
    _teste_auto = ram_timings._teste
    t["inicio_teste"] = time.time()
    d["tentativa"] = t
    d["fase"] = "teste"
    _gravar_diario(d)


def _verificar_teste(d: dict) -> str | None:
    t = ram_timings._teste
    tentativa = d.get("tentativa") or {}
    if t is None or t is not _teste_auto:
        if t is not None and t.rodando:
            return None  # um teste manual está rodando: espera ele acabar
        # o programa reiniciou no meio do teste: se o PC caiu, a tentativa falhou; senão testa de novo
        desde = float(tentativa.get("inicio_teste") or tentativa.get("quando") or time.time())
        quedas = oc_cpu.quedas_desde(desde)
        if quedas:
            return _resultado(d, False, {"motivo": "travou_no_teste", "quedas": quedas, "texto": "o PC travou no teste"})
        _iniciar_teste(d)
        return None
    if t.rodando:
        return None

    res = t.resultado
    if not res:
        _pausar(d, f"O teste de estabilidade não terminou: {t.erro or 'sem resultado'}.")
        return None
    if res.get("interrompido"):
        _pausar(d, "O teste de estabilidade foi parado. Toque em Continuar para testar de novo.")
        return None
    if res.get("curto") and not res.get("erros") and not res.get("whea") and not tentativa.get("repetido"):
        # o teste não chegou a conferir toda a memória (PC lento ou muita RAM): repete com o dobro do tempo
        tentativa["repetido"] = True
        tentativa["minutos"] = float(tentativa.get("minutos") or 4) * 2
        d["tentativa"] = tentativa
        _gravar_diario(d)
        _iniciar_teste(d)
        return None

    tipo = (d.get("plano") or {}).get("tipo") or "DDR5"
    limite = TEMP_MAX_PADRAO.get(tipo, 55)
    temp = res.get("temp_max")
    quente = temp is not None and temp > limite
    erros_ok = not res.get("erros") and not res.get("whea")
    ok = erros_ok and not quente and (res.get("estavel") or res.get("curto"))
    info = {"latencia": res.get("latencia"), "leitura": res.get("leitura"), "erros": res.get("erros"),
            "whea": res.get("whea"), "temp_max": temp}
    if not ok:
        info["motivo"] = "temperatura_alta" if quente and erros_ok else "instavel"
        info["texto"] = (f"os pentes chegaram a {temp} °C" if quente and erros_ok else
                         f"{res.get('erros') or 0} erro(s) de memória, {res.get('whea') or 0} erro(s) de hardware")
    return _resultado(d, ok, info)


def resumo_final(d: dict) -> str:
    ini = d.get("inicial") or {}
    fim = d.get("final_medida") or {}
    if ini.get("latencia") and fim.get("latencia"):
        return f"latência de {fmt(ini['latencia'])} para {fmt(fim['latencia'])} ns."
    return "a melhor configuração estável ficou gravada na BIOS."


def _finalizar(d: dict, ok: bool) -> None:
    ultimo = (d.get("tentativas") or [{}])[-1]
    if ok:
        d["final_medida"] = {k: ultimo.get(k) for k in ("latencia", "leitura")}
    mudou = {k: v for k, v in d["melhor"].items() if d["original"].get(k) != v}
    relatorio = {"quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "ok": ok, "original": d.get("original"),
                 "final": d.get("melhor") if ok else d.get("original"), "mudou": mudou if ok else {},
                 "inicial": d.get("inicial"), "final_medida": d.get("final_medida"), "tentativas": d.get("tentativas"),
                 "reboots": d.get("reboots", 0), "duracao_min": round((time.time() - d.get("inicio", time.time())) / 60, 1),
                 "plano": d.get("plano")}
    try:
        _arquivo(RELATORIO).write_text(json.dumps(relatorio, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.exception("falha ao gravar o relatório do auto-tune")
    d["fase"] = "final"
    d["ok_final"] = ok
    d["mudou"] = relatorio["mudou"]
    d["tentativa"] = None
    _gravar_diario(d)
    _apagar_tarefa()


# ------------------------------------------------------------------ controle pelo painel
def estado() -> dict:
    tick()  # com o painel aberto, o loop anda mesmo sem a bandeja
    d = _ler_diario()
    if not d:
        return {"fase": "ocioso"}
    d = dict(d)
    d["teste"] = ram_timings.estado_teste() if _teste_auto is not None and ram_timings._teste is _teste_auto else None
    feitas = sum(1 for x in d.get("tentativas") or [])
    restantes = 0
    for e in d.get("etapas") or []:
        if e.get("unica"):
            restantes += 0 if e.get("feita") else 1
        else:
            restantes += _buscas_estimadas(e["hi"] - e["lo"] + 1)
    d["progresso"] = round(feitas / max(1, feitas + restantes + (0 if d.get("fase") == "final" else 1)), 3)
    d["etapas_resumo"] = [{"id": e["id"], "nome": e["nome"], "inicio": _texto_cand(e, 0),
                           "melhor": _texto_cand(e, e.get("lo", 0)),
                           "estado": ("feita" if (e.get("unica") and e.get("feita")) or (not e.get("unica") and e["lo"] >= e["hi"])
                                      else "atual" if i == d.get("i_etapa") else "espera")}
                          for i, e in enumerate(d.get("etapas") or [])]
    d.pop("original", None)
    d.pop("etapas", None)
    return d


def _texto_cand(e: dict, i: int) -> str:
    c = e["candidatos"][i] if e.get("candidatos") else {}
    if isinstance(c, dict):
        return _texto_valores(c) or "como estava"
    return str(c)


def plano(forcar: bool = False) -> dict:
    p = analisar(forcar)
    if p.get("ok"):
        p["minutos"] = {m: estimativa_minutos(p, m) for m in MODOS}
    return p


def pausar() -> dict:
    with _trava:
        d = _ler_diario()
        if not d or d.get("fase") in (None, "ocioso", "final", "pausado"):
            return {"ok": False, "mensagem": "O auto-tune não está rodando."}
        if _teste_auto is not None and _teste_auto.rodando:
            _teste_auto.cancelar()
        _pausar(d, "Pausado por você.")
        return {"ok": True, "mensagem": "Auto-tune pausado. Os valores gravados continuam na BIOS."}


def continuar() -> dict:
    with _trava:
        d = _ler_diario()
        if not d or d.get("fase") != "pausado":
            return {"ok": False, "mensagem": "O auto-tune não está pausado."}
        if ram_timings.em_uso():
            return {"ok": False, "mensagem": "Um teste de RAM já está rodando."}
        if not tarefa_existe():
            _criar_tarefa()
        d["pausa"] = None
        d["falhas_boot"] = 0
        t = d.get("tentativa")
        if t:
            _tentar(d, {k: t[k] for k in ("etapa", "indice", "valores", "minutos") if k in t})
        else:
            _proxima_tentativa(d)
        return {"ok": True, "mensagem": "Auto-tune retomado."}


def pular() -> dict:
    """Encerra a etapa atual com o melhor valor já achado e segue para a próxima."""
    with _trava:
        d = _ler_diario()
        if not d or d.get("fase") in (None, "ocioso", "final"):
            return {"ok": False, "mensagem": "O auto-tune não está rodando."}
        t = d.get("tentativa") or {}
        if t.get("etapa") in (None, "final"):
            return {"ok": False, "mensagem": "Não há etapa para pular agora."}
        if _teste_auto is not None and _teste_auto.rodando:
            _teste_auto.cancelar()
            _teste_auto.aguardar(5)
        e = next((x for x in d["etapas"] if x["id"] == t["etapa"]), None)
        if e:
            e["hi"] = e.get("lo", 0)
            e["feita"] = True
        d["tentativa"] = None
        _proxima_tentativa(d)
        return {"ok": True, "mensagem": f"Etapa {ETAPAS.get(t['etapa'], t['etapa'])} encerrada com o último valor bom."}


def cancelar() -> dict:
    """Para tudo e grava de volta na BIOS os valores de antes do auto-tune."""
    with _trava:
        d = _ler_diario()
        if not d or d.get("fase") in (None, "ocioso"):
            return {"ok": False, "mensagem": "O auto-tune não está rodando."}
        if _teste_auto is not None and _teste_auto.rodando:
            _teste_auto.cancelar()
        original = d.get("original") or {}
        _apagar_diario()
        _apagar_tarefa()
        if not original:
            return {"ok": True, "mensagem": "Auto-tune cancelado. Não havia valores de antes guardados; use \"Voltar ao que estava\"."}
        r = aod.aplicar(original, origem="auto-tune cancelado", livre=True)
        if r.get("ok"):
            return {"ok": True, "mensagem": "Auto-tune cancelado. Os valores de antes foram gravados: reinicie o PC para valerem."}
        if "Nada mudou" in (r.get("mensagem") or ""):
            return {"ok": True, "mensagem": "Auto-tune cancelado. A BIOS já está com os valores de antes."}
        return {"ok": False, "mensagem": f"Auto-tune cancelado, mas a BIOS não aceitou voltar: {r.get('mensagem', '')}"}


def dispensar() -> dict:
    """Esconde o resultado de um auto-tune que já terminou (não mexe na BIOS)."""
    with _trava:
        d = _ler_diario()
        if d and d.get("fase") == "final":
            _apagar_diario()
        return {"ok": True, "mensagem": "Pronto."}


def relatorio() -> dict | None:
    try:
        return json.loads(_arquivo(RELATORIO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def encerrar() -> None:
    """Ao fechar o programa: o auto-tune continua na próxima vez (o diário fica no disco)."""
    pass
