"""Timings da memória RAM: o que o kit é, como está rodando, o que dá para apertar e o teste que confirma.

Timings de memória só mudam na BIOS (a placa-mãe treina a memória no boot), então o monitor não muda nada
sozinho. Ele:
1. Lê os pentes pelo Windows (fabricante, código, capacidade, velocidade) e decifra o código do kit (G.Skill,
   Corsair, Kingston, Crucial, TeamGroup, XPG, Patriot...) para saber o perfil XMP/EXPO e o chip provável.
2. Nos Ryzen (Zen 2 a Zen 5), lê os timings que estão valendo agora direto do controlador de memória (registradores
   UMC, pelo módulo AMDFamily17 do PawnIO, só leitura) e os clocks FCLK/UCLK/MCLK pela tabela de energia do SMU.
3. Sugere configurações em três níveis (perfil do fabricante, afinado, agressivo) com o nome do campo na BIOS e a
   estimativa de ganho em latência e banda.
4. Testa: mede latência e banda (o mesmo teste da página Teste de memória) e roda um teste de estabilidade feito
   para timings (`ram_testar` em `nativo/estavel.c`), conferindo também os erros de hardware do Windows. Guarda cada
   resultado com os timings daquele momento, para comparar e achar a melhor configuração estável.

Os endereços e campos dos registradores do controlador de memória são fatos técnicos documentados pela comunidade
(ZenTimings / ZenStates-Core); nenhum código desses projetos foi copiado.
"""

import ctypes
import json
import logging
import math
import re
import threading
import time
from datetime import datetime

import psutil

from . import config, memoria, pawnio, windows

log = logging.getLogger(__name__)

TIPOS_DDR = memoria.TIPOS_DDR

_SCRIPT_PENTES = r"""
Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,PartNumber,
  DeviceLocator,BankLabel,SMBIOSMemoryType,FormFactor,Attributes,ConfiguredVoltage,MinVoltage,MaxVoltage | ConvertTo-Json -Compress
"""

# Códigos JEDEC dos fabricantes de chip que o Windows às vezes mostra no lugar do nome
FABRICANTES = {"80CE": "Samsung", "CE00": "Samsung", "80AD": "SK Hynix", "AD00": "SK Hynix", "802C": "Micron",
               "2C00": "Micron", "04CD": "G.Skill", "CD04": "G.Skill", "029E": "Corsair", "9E02": "Corsair",
               "0198": "Kingston", "9801": "Kingston", "059B": "Crucial", "9B05": "Crucial", "04EF": "TeamGroup",
               "EF04": "TeamGroup", "04CB": "ADATA", "CB04": "ADATA", "8502": "Patriot", "0502": "Patriot"}


# ------------------------------------------------------------------ pentes e código do kit
def _nome_fabricante(texto: str) -> str:
    t = (texto or "").strip()
    chave = t.upper().replace("0X", "")[:4]
    if chave in FABRICANTES:
        return FABRICANTES[chave]
    for nome in ("Samsung", "Hynix", "Micron", "G.Skill", "G Skill", "Corsair", "Kingston", "Crucial", "Team", "ADATA",
                 "XPG", "Patriot", "Lexar", "Netac", "Klevv", "PNY", "Thermaltake", "Asgard", "Kingbank", "Fanxiang"):
        if nome.lower() in t.lower():
            return {"Hynix": "SK Hynix", "G Skill": "G.Skill", "Team": "TeamGroup"}.get(nome, nome)
    return t or "Desconhecido"


def pentes() -> list[dict]:
    saida = []
    for m in windows.powershell_json(_SCRIPT_PENTES):
        saida.append({
            "local": (m.get("DeviceLocator") or m.get("BankLabel") or "").strip(),
            "fabricante": _nome_fabricante(m.get("Manufacturer") or ""),
            "modelo": (m.get("PartNumber") or "").strip(),
            "gb": round((m.get("Capacity") or 0) / 2**30),
            "nominal": m.get("Speed") or 0,
            "atual": m.get("ConfiguredClockSpeed") or 0,
            "tipo": TIPOS_DDR.get(m.get("SMBIOSMemoryType"), ""),
            "ranks": (m.get("Attributes") or 0) & 0xF or None,
            "tensao": round((m.get("ConfiguredVoltage") or 0) / 1000, 3) or None,
        })
    return saida


# (regex, função que monta o resultado) — o código do pente guarda velocidade e latência do perfil XMP/EXPO
def _ddr(dig: str) -> str:
    return "DDR5" if dig == "5" else "DDR4"


DECODIFICADORES = [
    # G.Skill: F5-6000J3038F16GX2-TZ5N (DDR5-6000 CL30-38-38, 16 GB, kit de 2) · F4-3600C16D-32GTZNC
    (r"^F5-(\d{4})[A-Z](\d{2})(\d{2})[A-Z](\d+)G(?:X(\d))?", lambda m: dict(marca="G.Skill", tipo="DDR5", mts=int(m[1]), cl=int(m[2]),
                                                                       trcd=int(m[3]), trp=int(m[3]), gb=int(m[4]), kit=int(m[5] or 1))),
    (r"^F4-(\d{4})C(\d{2})[SDQ]-(\d+)G", lambda m: dict(marca="G.Skill", tipo="DDR4", mts=int(m[1]), cl=int(m[2]), gb_kit=int(m[3]))),
    # Corsair: CMK32GX5M2B6000C36 / CMH32GX5M2B6000Z30 (Z = EXPO) / CMK16GX4M2B3200C16
    (r"^CM\w*?(\d+)GX([45])M(\d)[A-Z](\d{4})[CZ](\d{2})", lambda m: dict(marca="Corsair", tipo=_ddr(m[2]), mts=int(m[4]), cl=int(m[5]),
                                                                       gb_kit=int(m[1]), kit=int(m[3]))),
    # Kingston Fury: KF560C36BBEK2-32 (DDR5-6000 CL36) · KF436C18BBK2/32 · ValueRAM KVR56U46BS8-16
    (r"^KF([45])(\d{2})C(\d{2})", lambda m: dict(marca="Kingston", tipo=_ddr(m[1]), mts=int(m[2]) * 100, cl=int(m[3]))),
    (r"^KVR(\d{2})[USNE](\d{2})", lambda m: dict(marca="Kingston", tipo="DDR5" if int(m[1]) >= 44 else "DDR4",
                                                 mts={26: 2666, 29: 2933}.get(int(m[1]), int(m[1]) * 100), cl=int(m[2]))),
    # Crucial: CT16G56C46U5 / CP16G60C36U5B (Pro) / Ballistix BL16G36C16U4B
    (r"^C[TP](\d+)G(\d{2})C(\d{2})[US]([45])", lambda m: dict(marca="Crucial", tipo=_ddr(m[4]), mts=int(m[2]) * 100, cl=int(m[3]), gb=int(m[1]))),
    (r"^BLS?(\d+)G(\d{2})C(\d{2})U4", lambda m: dict(marca="Crucial Ballistix", tipo="DDR4", mts=int(m[2]) * 100, cl=int(m[3]), gb=int(m[1]))),
    # TeamGroup: FF3D532G6000HC38ADC01 / CTCED532G6000HC34BDC01 / TED516G5600C4601
    (r"^\w*?D5(\d+)G(\d{4})HC(\d{2})", lambda m: dict(marca="TeamGroup", tipo="DDR5", mts=int(m[2]), cl=int(m[3]), gb_kit=int(m[1]))),
    (r"^TED5(\d+)G(\d{4})C(\d{2})", lambda m: dict(marca="TeamGroup", tipo="DDR5", mts=int(m[2]), cl=int(m[3]), gb=int(m[1]))),
    (r"^\w*?D4(\d+)G(\d{4})HC(\d{2})", lambda m: dict(marca="TeamGroup", tipo="DDR4", mts=int(m[2]), cl=int(m[3]), gb_kit=int(m[1]))),
    # ADATA XPG: AX5U6000C3016G-DCLARBK / AX4U360016G18A-DT60
    (r"^AX5U(\d{4})C(\d{2})(\d+)G", lambda m: dict(marca="XPG", tipo="DDR5", mts=int(m[1]), cl=int(m[2]), gb=int(m[3]))),
    (r"^AX4U(\d{4})(\d+)G(\d{2})", lambda m: dict(marca="XPG", tipo="DDR4", mts=int(m[1]), cl=int(m[3]), gb=int(m[2]))),
    # Patriot Viper DDR5: PVV532G600C36K
    (r"^PV\w?5(\d+)G(\d{3})C(\d{2})", lambda m: dict(marca="Patriot", tipo="DDR5", mts=int(m[2]) * 10, cl=int(m[3]), gb_kit=int(m[1]))),
    # Lexar ARES: LD5U16G60C32LG-RGD / LD5DU016G-R6000GD0
    (r"^LD5U?(\d+)G(\d{2})C(\d{2})", lambda m: dict(marca="Lexar", tipo="DDR5", mts=int(m[2]) * 100, cl=int(m[3]), gb=int(m[1]))),
    (r"^LD5\w*-R(\d{4})", lambda m: dict(marca="Lexar", tipo="DDR5", mts=int(m[1]))),
]

# Pentes "de fábrica" (sem XMP/EXPO): o fabricante do pente é o fabricante do chip
OEM = [(r"^M3\d{2}R|^M4\d{2}[AB]", "Samsung"), (r"^HMC[GA]|^HMA|^HMAA", "SK Hynix"), (r"^MTC|^MTA|^4ATF", "Micron")]

JEDEC = {"DDR5": {4800: (40, 39), 5200: (42, 42), 5600: (46, 45), 6000: (50, 48), 6400: (52, 52)},
         "DDR4": {2133: (15, 15), 2400: (17, 17), 2666: (19, 19), 2933: (21, 21), 3200: (22, 22)}}


def decodificar(modelo: str, fabricante: str = "", tipo: str = "") -> dict | None:
    """Perfil de fábrica (XMP/EXPO) pelo código do pente. None quando o código não é de um kit conhecido."""
    pn = (modelo or "").strip().upper()
    for regex, montar in DECODIFICADORES:
        m = re.match(regex, pn)
        if m:
            d = montar(m)
            if not (1600 <= d["mts"] <= 10000):
                continue
            d.setdefault("trcd", None)
            d.setdefault("trp", d["trcd"])
            d["fonte"] = "codigo"
            return d
    for regex, marca in OEM:
        if re.match(regex, pn):
            return {"marca": marca, "tipo": tipo, "mts": None, "cl": None, "trcd": None, "trp": None, "fonte": "oem", "oem": True}
    return None


def chip_provavel(kit: dict | None, pente: dict) -> dict:
    """Fabricante do chip mais provável (é ele que decide até onde dá para apertar)."""
    fab = pente.get("fabricante", "")
    if kit and kit.get("oem"):
        return {"nome": kit["marca"], "confianca": "alta", "motivo": "pente de fábrica: o fabricante do pente faz o chip"}
    if fab in ("Samsung", "SK Hynix", "Micron"):
        return {"nome": fab, "confianca": "alta", "motivo": "o próprio fabricante do chip fez o pente"}
    gb = pente.get("gb") or 0
    tipo = (kit or {}).get("tipo") or pente.get("tipo")
    if not kit or not kit.get("cl") or not kit.get("mts"):
        return {"nome": "desconhecido", "confianca": "baixa", "motivo": "o código do pente não diz a velocidade de fábrica"}
    mts, cl, trcd = kit["mts"], kit["cl"], kit.get("trcd")
    if tipo == "DDR5":
        if gb in (24, 48):
            return {"nome": "chip de 24 Gbit (SK Hynix M-die, Micron ou Samsung)", "confianca": "média",
                    "motivo": f"pentes de {gb} GB usam chips de 24 Gbit"}
        ns = cl * 2000 / mts
        if ns <= 10.7 or (mts >= 7000 and cl <= 36):
            return {"nome": "SK Hynix (A-die ou M-die)", "confianca": "alta", "motivo": f"{mts} CL{cl} só sai com chips Hynix"}
        if ns <= 11.0 or (trcd and trcd <= 38 and mts >= 6000):
            return {"nome": "SK Hynix (A-die ou M-die)", "confianca": "média", "motivo": f"{mts} CL{cl} quase sempre é Hynix"}
        if ns <= 12.1:
            return {"nome": "SK Hynix ou Samsung", "confianca": "baixa", "motivo": f"{mts} CL{cl} sai com Hynix e com Samsung"}
        return {"nome": "Micron ou Samsung", "confianca": "baixa", "motivo": f"{mts} CL{cl} é típico de chips Micron e Samsung"}
    if tipo == "DDR4":
        ns = cl * 2000 / mts
        if ns <= 8.0 or (mts >= 4000 and cl <= 16) or (mts == 3200 and cl <= 14) or (mts == 3600 and cl <= 15):
            return {"nome": "Samsung B-die", "confianca": "alta", "motivo": f"{mts} CL{cl} só sai com Samsung B-die"}
        if mts >= 3600 and cl == 16:
            return {"nome": "SK Hynix DJR, Micron Rev.E ou Samsung B-die", "confianca": "baixa",
                    "motivo": f"{mts} CL{cl} sai com vários chips"}
        return {"nome": "Micron, Hynix ou Samsung", "confianca": "baixa", "motivo": f"{mts} CL{cl} é comum a vários chips"}
    return {"nome": "desconhecido", "confianca": "baixa", "motivo": ""}


# ------------------------------------------------------------------ SPD (o que está gravado no pente)
def densidade_gbit(tipo: str, trfc1_ns: float | None) -> int | None:
    """Tamanho de cada chip pelo tRFC1 do SPD, que a JEDEC fixa por densidade."""
    if not trfc1_ns:
        return None
    if tipo == "DDR5":
        return 16 if trfc1_ns < 350 else 24 if trfc1_ns < 450 else 32
    if tipo == "DDR4":
        return 4 if trfc1_ns < 300 else 8 if trfc1_ns < 450 else 16
    return None


# Quem fabrica chips de DRAM (o SPD guarda o código JEDEC; a biblioteca devolve o nome)
FABRICANTES_CHIP = (("hynix", "SK Hynix"), ("samsung", "Samsung"), ("spectek", "Micron"), ("micron", "Micron"),
                    ("nanya", "Nanya"), ("cxmt", "CXMT"), ("changxin", "CXMT"), ("winbond", "Winbond"),
                    ("elpida", "Elpida"), ("powerchip", "Powerchip"))


def fabricante_do_chip(texto: str) -> str | None:
    t = (texto or "").lower()
    if t[:4].upper() in FABRICANTES:  # alguns devolvem o código
        t = FABRICANTES[t[:4].upper()].lower()
    return next((nome for chave, nome in FABRICANTES_CHIP if chave in t), None)


def resumo_spd(item: dict, tipo: str) -> dict:
    """SPD de um pente: fabricante do chip, densidade e o perfil JEDEC base (velocidade e CL-tRCD-tRP)."""
    t = item.get("tempos") or {}
    tipo = tipo or ("DDR5" if "DDR5" in (item.get("tipo") or "") else "DDR4" if "DDR4" in (item.get("tipo") or "") else "")
    saida = {"fabricante_chip": fabricante_do_chip(item.get("fabricante_chip") or ""), "modelo": item.get("modelo") or "",
             "gbit": densidade_gbit(tipo, t.get("trfc1")), "trfc1_ns": t.get("trfc1"), "tipo": tipo}
    tck = t.get("tck")
    if tck and 0.15 <= tck <= 2.0:
        mts = 2000 / tck
        jedec = [4000, 4400, 4800, 5200, 5600, 6000, 6400, 6800, 7200, 8000] if tipo == "DDR5" else \
            [1600, 1866, 2133, 2400, 2666, 2933, 3200]
        mts = min(jedec, key=lambda v: abs(v - mts))
        ck = 2000 / mts
        ciclos = {k: math.ceil(t[k] / ck - 0.01) for k in ("taa", "trcd", "trp") if t.get(k)}
        saida.update(base_mts=mts, base_cl=ciclos.get("taa"), base_trcd=ciclos.get("trcd"), base_trp=ciclos.get("trp"))
    return saida


def chip_do_spd(spd: dict, kit: dict | None, pente: dict) -> dict | None:
    """Fabricante do chip lido do pente, refinado pela densidade e pelo perfil do kit. None se o SPD não disse."""
    fab, gbit, tipo = spd.get("fabricante_chip"), spd.get("gbit"), spd.get("tipo") or pente.get("tipo")
    if not fab:
        return None
    dens = f" de {gbit} Gbit" if gbit else ""
    motivo = f"Chips {fab}{dens}"  # a tela já mostra que veio do SPD
    if tipo == "DDR5":
        if gbit and gbit >= 24:
            return {"nome": f"chip de {gbit} Gbit ({fab})", "confianca": "alta", "motivo": motivo, "spd": True}
        if fab == "SK Hynix":
            return {"nome": "SK Hynix (A-die ou M-die)", "confianca": "alta", "motivo": motivo, "spd": True}
        return {"nome": fab, "confianca": "alta", "motivo": motivo, "spd": True}
    if tipo == "DDR4" and fab == "Samsung":
        palpite = chip_provavel(kit, {**pente, "fabricante": ""})
        if "B-die" in palpite["nome"] and palpite["confianca"] == "alta" and gbit in (None, 8):
            return {"nome": "Samsung B-die", "confianca": "alta", "spd": True,
                    "motivo": f"{motivo}; {kit['mts']} CL{kit['cl']} só sai com B-die"}
        return {"nome": "Samsung (die não identificado)", "confianca": "média", "motivo": motivo + "; o perfil do kit não confirma B-die", "spd": True}
    return {"nome": fab, "confianca": "alta", "motivo": motivo, "spd": True}


def _spd_dos_sensores(espera: float) -> list | None:
    """SPD pelo processo de sensores (que já tem o driver aberto); None se ainda não chegou."""
    if not windows.EH_WINDOWS or not windows.eh_admin():
        return []
    try:
        from . import sensores
        return sensores.instancia().spd(espera)
    except Exception:
        log.exception("SPD indisponível")
        return []


def _casar_spd(lista: list[dict], spd: list[dict]) -> None:
    """Liga cada pente do Windows ao seu SPD: pelo código do pente e, se não der, pela ordem."""
    livres = list(spd)
    for p in lista:
        par = next((x for x in livres if x.get("modelo") and x["modelo"].upper() == (p["modelo"] or "").upper()), None)
        if par:
            livres.remove(par)
            p["_spd"] = par
    sem = [p for p in lista if "_spd" not in p]
    if sem and len(sem) == len(livres):
        for p, x in zip(sem, sorted(livres, key=lambda x: x.get("indice", x.get("endereco", 0)))):
            p["_spd"] = x


_kit_cache: tuple[float, dict, bool] | None = None


def kit(forcar: bool = False) -> dict:
    """Resumo dos pentes instalados: um kit (o mais comum) e a lista completa (guardado por 5 minutos; por 10 s se o
    SPD ainda não tinha chegado)."""
    global _kit_cache
    if _kit_cache and not forcar and time.time() - _kit_cache[0] < (10 if _kit_cache[2] else 300):
        return _kit_cache[1]
    spd = _spd_dos_sensores(6.0 if not _kit_cache else 1.0)
    saida = _kit(spd or [])
    _kit_cache = (time.time(), saida, spd is None)
    return saida


def _kit(spd: list[dict] | None = None) -> dict:
    lista = pentes()
    _casar_spd(lista, spd or [])
    for p in lista:
        s = p.pop("_spd", None)
        p["spd"] = resumo_spd(s, p["tipo"]) if s else None
        if p["spd"] and not p["modelo"] and p["spd"]["modelo"]:
            p["modelo"] = p["spd"]["modelo"]  # placa que não informa o código ao Windows
        p["decodificado"] = decodificar(p["modelo"], p["fabricante"], p["tipo"])
        p["chip"] = (chip_do_spd(p["spd"], p["decodificado"], p) if p["spd"] else None) or chip_provavel(p["decodificado"], p)
    if not lista:
        return {"pentes": [], "resumo": None}
    base = max(lista, key=lambda p: (sum(1 for q in lista if q["modelo"] == p["modelo"]), p["gb"]))
    d = base["decodificado"] or {}
    tipo = d.get("tipo") or base["tipo"]
    perfil_mts = d.get("mts") or (base["nominal"] if base["nominal"] and base["nominal"] > (base["atual"] or 0) else None)
    iguais = all(p["modelo"] == base["modelo"] for p in lista)
    resumo = {"tipo": tipo, "quantidade": len(lista), "gb_pente": base["gb"], "gb_total": sum(p["gb"] for p in lista),
              "marca": d.get("marca") or base["fabricante"], "modelo": base["modelo"], "perfil_mts": perfil_mts,
              "perfil_cl": d.get("cl"), "perfil_trcd": d.get("trcd"), "perfil_trp": d.get("trp"), "oem": bool(d.get("oem")),
              "atual_mts": base["atual"], "ranks": base.get("ranks"), "chip": base["chip"], "iguais": iguais,
              "tensao": base.get("tensao"), "conhecido": bool(d and d.get("fonte") == "codigo"), "spd": base.get("spd")}
    return {"pentes": lista, "resumo": resumo}


# ------------------------------------------------------------------ plataforma
def plataforma() -> dict:
    fab, familia, modelo = pawnio.cpu_fms()
    from . import sensores
    nome = sensores.nome_cpu() or ""
    p = {"marca": fab, "familia": familia, "modelo": modelo, "nome": nome, "geracao": None, "ddr": None,
         "le_timings": False}
    if fab == "amd":
        if familia == 0x1A and modelo in (0x44,):
            p.update(geracao="Zen 5", ddr="DDR5", le_timings=True, soquete="AM5")
        elif familia == 0x19 and modelo in (0x61,):
            p.update(geracao="Zen 4", ddr="DDR5", le_timings=True, soquete="AM5")
        elif familia == 0x19 and modelo in (0x74, 0x75, 0x78):
            p.update(geracao="Zen 4 (APU)", ddr="DDR5", le_timings=True, soquete="AM5")
        elif familia == 0x19 and modelo in (0x21, 0x50, 0x51):
            p.update(geracao="Zen 3", ddr="DDR4", le_timings=True, soquete="AM4")
        elif familia == 0x17 and modelo in (0x71, 0x60, 0x68, 0x08, 0x18):
            p.update(geracao="Zen 2" if modelo >= 0x60 else "Zen+", ddr="DDR4", le_timings=True, soquete="AM4")
    elif fab == "intel":
        m = re.search(r"i[3579]-(\d{2})\d{3}", nome) or re.search(r"Ultra [3579] (\d)\d{2}", nome)
        if m:
            g = int(m.group(1))
            p["geracao"] = f"Core Ultra {g}00" if "Ultra" in nome else f"{g}ª geração"
            p["ddr"] = "DDR5" if "Ultra" in nome else None
    return p


# ------------------------------------------------------------------ leitura dos timings atuais (Ryzen)
# campos: nome -> (registrador, bit alto, bit baixo); DDR5 e DDR4 diferem em alguns larguras
CAMPOS = {
    "DDR5": {"cl": (0x50204, 5, 0), "trcd": (0x50204, 21, 16), "trcdwr": (0x50204, 29, 24), "tras": (0x50204, 14, 8),
             "trp": (0x50208, 21, 16), "trc": (0x50208, 7, 0), "trrds": (0x5020C, 4, 0), "trrdl": (0x5020C, 12, 8),
             "trtp": (0x5020C, 28, 24), "tfaw": (0x50210, 7, 0), "tcwl": (0x50214, 5, 0), "twtrs": (0x50214, 12, 8),
             "twtrl": (0x50214, 22, 16), "twr": (0x50218, 7, 0), "trdrdscl": (0x50220, 29, 24),
             "twrwrscl": (0x50224, 29, 24), "trdwr": (0x50228, 13, 8), "twrrd": (0x50228, 3, 0), "trefi": (0x50230, 15, 0),
             "gdm": (0x50200, 18, 18), "cmd2t": (0x50200, 17, 17)},
    "DDR4": {"cl": (0x50204, 5, 0), "trcd": (0x50204, 21, 16), "trcdwr": (0x50204, 29, 24), "tras": (0x50204, 14, 8),
             "trp": (0x50208, 21, 16), "trc": (0x50208, 7, 0), "trrds": (0x5020C, 4, 0), "trrdl": (0x5020C, 12, 8),
             "trtp": (0x5020C, 28, 24), "tfaw": (0x50210, 6, 0), "tcwl": (0x50214, 5, 0), "twtrs": (0x50214, 12, 8),
             "twtrl": (0x50214, 22, 16), "twr": (0x50218, 6, 0), "trdrdscl": (0x50220, 27, 24),
             "twrwrscl": (0x50224, 29, 24), "trdwr": (0x50228, 12, 8), "twrrd": (0x50228, 3, 0), "trefi": (0x50230, 15, 0),
             "gdm": (0x50200, 11, 11), "cmd2t": (0x50200, 10, 10)},
}
# terciários e ajustes de energia (mesmos registradores no DDR4 e no DDR5, larguras diferentes)
TERCIARIOS = {
    "DDR5": {"trdrdsc": (0x50220, 19, 16), "trdrdsd": (0x50220, 11, 8), "trdrddd": (0x50220, 3, 0),
             "twrwrsc": (0x50224, 19, 16), "twrwrsd": (0x50224, 11, 8), "twrwrdd": (0x50224, 3, 0),
             "tmod": (0x50234, 13, 8), "tmrd": (0x50234, 5, 0), "tstag": (0x50250, 26, 16), "tcke": (0x50254, 28, 24),
             "txp": (0x50254, 5, 0), "tphyrdl": (0x50258, 23, 16), "powerdown": (0x5012C, 28, 28), "ecc": (0x50100, 12, 12)},
    "DDR4": {"trdrdsc": (0x50220, 19, 16), "trdrdsd": (0x50220, 11, 8), "trdrddd": (0x50220, 3, 0),
             "twrwrsc": (0x50224, 19, 16), "twrwrsd": (0x50224, 11, 8), "twrwrdd": (0x50224, 3, 0),
             "tmod": (0x50234, 13, 8), "tmrd": (0x50234, 5, 0), "tstag": (0x50250, 23, 16), "tcke": (0x50254, 28, 24),
             "txp": (0x50254, 5, 0), "tphyrdl": (0x50258, 21, 16), "powerdown": (0x5012C, 28, 28), "ecc": (0x50100, 12, 12)},
}
# o que se compara canal a canal: se o treino da placa deu valores diferentes, aparece aqui
POR_CANAL = ("cl", "trcd", "trp", "tras", "trc", "trfc", "tphyrdl")
# deslocamentos (em bytes) de FCLK, UCLK e MCLK na tabela de energia, conferidos contra o MCLK do controlador
TABELA_CLOCKS = [(0x118, 0x128, 0x138), (0x11C, 0x12C, 0x13C), (0xC0, 0xC8, 0xCC)]


def _bits(v: int, alto: int, baixo: int) -> int:
    return (v >> baixo) & ((1 << (alto - baixo + 1)) - 1)


def decodificar_umc(ler, ddr: str) -> dict | None:
    """Lê o primeiro canal ativo do controlador de memória. `ler(endereco_smn)` devolve 32 bits."""
    canais = []
    for i in range(12):
        base = i << 20
        try:
            ativo = _bits(ler(base | 0x50DF0), 19, 19) == 0
            dimm = (ler(base | 0x50000) & 1) or (ler(base | 0x50008) & 1)
        except Exception:
            continue
        if ativo and dimm:
            canais.append(base)
    if not canais:
        return None
    base = canais[0]
    cache: dict[int, int] = {}

    def reg(end):
        if end not in cache:
            cache[end] = ler(base | end)
        return cache[end]

    r200 = reg(0x50200)
    if ddr == "DDR5":
        mts = round((r200 & 0xFFFF) / 100 * 100 * 2)
    else:
        mts = round((r200 & 0x7F) / 3 * 100 * 2)
    if not 1600 <= mts <= 12000:
        return None
    t = {nome: _bits(reg(end), alto, baixo) for nome, (end, alto, baixo) in CAMPOS[ddr].items()}
    extra = {}
    for nome, (end, alto, baixo) in TERCIARIOS[ddr].items():
        try:
            extra[nome] = _bits(reg(end), alto, baixo)
        except Exception:
            pass
    rfc = _trfc(reg, ddr, t)
    t["trfc"] = rfc
    ns = 2000 / mts  # um clock do controlador (MCLK) em ns
    t["trfc_ns"] = round(rfc * ns) if rfc else None
    t["cl_ns"] = round(t["cl"] * ns, 2)
    t.update({k: v for k, v in extra.items() if k not in ("powerdown", "ecc")})

    # canal a canal: os principais e o atraso de leitura que o treino achou (tPHYRDL)
    por_canal = []
    for n, b in enumerate(canais):
        try:
            c = {"canal": "ABCDEFGHIJKL"[n]}
            for nome in POR_CANAL:
                if nome == "trfc":
                    c[nome] = _trfc(lambda e: ler(b | e), ddr, {})
                elif nome in CAMPOS[ddr]:
                    end, alto, baixo = CAMPOS[ddr][nome]
                    c[nome] = _bits(ler(b | end), alto, baixo)
                else:
                    end, alto, baixo = TERCIARIOS[ddr][nome]
                    c[nome] = _bits(ler(b | end), alto, baixo)
            por_canal.append(c)
        except Exception:
            continue
    return {"ddr": ddr, "mts": mts, "mclk": mts / 2, "canais": len(canais), "timings": t, "por_canal": por_canal,
            "gdm": bool(t.pop("gdm")), "comando": "2T" if t.pop("cmd2t") else "1T",
            "power_down": bool(extra.get("powerdown")) if "powerdown" in extra else None,
            "ecc": bool(extra.get("ecc")) if "ecc" in extra else None}


def _trfc(reg, ddr: str, t: dict) -> int:
    """tRFC em clocks. No DDR5 vale o primeiro registrador preenchido de quatro; no DDR4, um de dois."""
    if ddr == "DDR5":
        rfc = 0
        for end in (0x50260, 0x50264, 0x50268, 0x5026C):
            v = reg(end)
            if v and v != 0x00C00138:
                rfc = v & 0xFFFF
                t["trfc2"] = v >> 16
                break
        try:
            rfcsb = next((reg(e) & 0x7FF for e in (0x502C0, 0x502C4, 0x502C8, 0x502CC) if reg(e) & 0x7FF), 0)
            if rfcsb:
                t["trfcsb"] = rfcsb
        except Exception:
            pass
        return rfc
    a, b = reg(0x50260), reg(0x50264)
    v = (a if a != 0x21060138 else b) if a != b else a
    return v & 0x7FF


def clocks_da_tabela(floats: list[float], mclk: float) -> dict | None:
    for f, u, m in TABELA_CLOCKS:
        if max(f, u, m) // 4 >= len(floats):
            continue
        fclk, uclk, mc = floats[f // 4], floats[u // 4], floats[m // 4]
        if mc and abs(mc - mclk) / mclk < 0.05 and 600 <= fclk <= 3000 and 300 <= uclk <= 4000:
            modo = "1:1" if abs(uclk - mc) / mc < 0.04 else "1:2" if abs(uclk - mc / 2) / mc < 0.04 else None
            return {"fclk": round(fclk), "uclk": round(uclk), "mclk": round(mc), "modo_uclk": modo}
    return None


_leitura_cache: dict | None = None


def leitura_atual(forcar: bool = False) -> dict:
    """Timings valendo agora. {"ok": bool, "motivo": str, ...}"""
    global _leitura_cache
    if _leitura_cache and not forcar and time.time() - _leitura_cache.get("lido_em", 0) < 30:
        return _leitura_cache
    plat = plataforma()
    if not plat["le_timings"]:
        saida = {"ok": False, "motivo": "intel" if plat["marca"] == "intel" else "nao_suportado"}
    elif not windows.EH_WINDOWS:
        saida = {"ok": False, "motivo": "nao_suportado"}
    elif not pawnio.disponivel():
        saida = {"ok": False, "motivo": "sem_pawnio"}
    elif not windows.eh_admin():
        saida = {"ok": False, "motivo": "sem_admin"}
    else:
        try:
            mod = pawnio.modulo("AMDFamily17")

            def ler(end):
                with pawnio.trava_pci():
                    return mod.executar("ioctl_read_smn", [end], 1)[0] & 0xFFFFFFFF

            saida = decodificar_umc(ler, plat["ddr"]) or {"motivo": "O controlador de memória não respondeu."}
            saida["ok"] = "mts" in saida
            if saida["ok"]:
                try:
                    from . import oc_ryzen
                    _, floats = oc_ryzen.Smu().tabela(96)
                    saida["clocks"] = clocks_da_tabela(floats, saida["mclk"])
                except Exception as e:
                    log.info("tabela de energia indisponível para os clocks da memória: %s", e)
        except pawnio.ErroPawnIO as e:
            saida = {"ok": False, "motivo": f"O PawnIO não leu o controlador: {e.texto}."}
        except Exception as e:
            log.exception("falha ao ler os timings")
            saida = {"ok": False, "motivo": f"Não consegui ler os timings: {e}"}
    saida["lido_em"] = time.time()
    _leitura_cache = saida
    return saida


# ------------------------------------------------------------------ diagnóstico e sugestões
def _par(v: int) -> int:
    return v + (v & 1)


NOMES_CURTOS = {"cl": "CL", "trcd": "tRCD", "trp": "tRP", "tras": "tRAS", "trc": "tRC", "trfc": "tRFC"}

# tRFC (ns) por chip: (afinado, agressivo). Chips de 16 Gbit no DDR5 e de 8 Gbit no DDR4; None = não mexer.
TRFC_NS = {
    "DDR5": {"hynix": (160, 130), "samsung": (230, None), "micron": (260, None), "24g": (220, None), "?": (260, None)},
    "DDR4": {"bdie": (180, 150), "hynix": (260, None), "micron": (290, None), "?": (320, None)},
}


def _familia_chip(chip: dict, tipo: str) -> str:
    n = (chip or {}).get("nome", "").lower()
    if chip and chip.get("confianca") == "baixa":
        return "?"
    if tipo == "DDR5":
        if "24 gbit" in n:
            return "24g"
        return "hynix" if n.startswith("sk hynix") else "samsung" if n.startswith("samsung") else "micron" if n.startswith("micron") else "?"
    return "bdie" if "b-die" in n else "hynix" if "hynix" in n else "micron" if n.startswith("micron") else "?"


def diagnostico(k: dict, atual: dict, plat: dict) -> list[dict]:
    """Coisas fora do lugar na configuração atual: {nivel, titulo, texto}."""
    achados = []
    r = k.get("resumo") or {}
    if not r:
        return achados
    mts_agora = (atual.get("mts") if atual.get("ok") else None) or r.get("atual_mts")
    perfil = r.get("perfil_mts")
    if perfil and mts_agora and mts_agora < perfil * 0.92:
        achados.append({"nivel": "aviso", "titulo": "Perfil XMP/EXPO desligado",
                        "texto": f"O kit é de {perfil} MT/s mas está rodando a {mts_agora}. Ligar o perfil na BIOS é o "
                                 "maior ganho possível, sem risco."})
    if r.get("quantidade") == 1:
        achados.append({"nivel": "aviso", "titulo": "Um pente só (canal único)",
                        "texto": "Com um pente a memória entrega metade da banda. Um segundo pente igual faz mais "
                                 "diferença que qualquer timing."})
    if r.get("quantidade", 0) >= 4 and r.get("tipo") == "DDR5":
        achados.append({"nivel": "info", "titulo": "Quatro pentes de DDR5",
                        "texto": "Com 4 pentes a maioria das placas não passa de 5200 a 5600 MT/s estável. "
                                 "Dois pentes maiores sobem bem mais."})
    if not r.get("iguais"):
        achados.append({"nivel": "info", "titulo": "Pentes diferentes misturados",
                        "texto": "Kits diferentes costumam exigir os timings do pior deles. Aperte com cuidado."})
    c = atual.get("clocks") if atual.get("ok") else None
    if c and plat.get("soquete") == "AM5":
        if c.get("modo_uclk") == "1:2" and mts_agora and mts_agora <= 6400:
            achados.append({"nivel": "aviso", "titulo": "Controlador de memória em 1:2",
                            "texto": f"A {mts_agora} MT/s o controlador (UCLK) devia estar igual ao clock da memória. "
                                     "Na BIOS, ponha \"UCLK DIV1 MODE\" em \"UCLK=MEMCLK\": cerca de 8 ns a menos de latência."})
        if c.get("fclk") and c["fclk"] < 1900:
            achados.append({"nivel": "info", "titulo": f"Infinity Fabric em {c['fclk']} MHz",
                            "texto": "Nos Ryzen 7000/9000 o FCLK em 2000 MHz costuma ser estável e tira alguns ns de latência."})
    if c and plat.get("soquete") == "AM4" and c.get("fclk") and mts_agora and abs(c["fclk"] - mts_agora / 2) > 20 and mts_agora <= 3800:
        achados.append({"nivel": "aviso", "titulo": "Infinity Fabric fora do 1:1",
                        "texto": f"No AM4 o FCLK devia ser metade da velocidade da RAM ({mts_agora // 2} MHz). Fora disso a "
                                 "latência sobe bastante."})
    canais = atual.get("por_canal") or [] if atual.get("ok") else []
    if len(canais) > 1:
        def dif(chave):
            return len({c.get(chave) for c in canais if c.get(chave) is not None}) > 1
        primarios = [c for c in ("cl", "trcd", "trp", "tras", "trc", "trfc") if dif(c)]
        if primarios:
            achados.append({"nivel": "aviso", "titulo": "Canais com timings diferentes",
                            "texto": f"{', '.join(NOMES_CURTOS.get(c, c) for c in primarios)} não é igual em todos os canais. "
                                     "A placa ajustou um canal sozinha, o que costuma indicar que ele não aguentou o valor pedido."})
        if dif("tphyrdl"):
            v = " / ".join(f"{c['canal']} {c['tphyrdl']}" for c in canais if c.get("tphyrdl") is not None)
            achados.append({"nivel": "info", "titulo": "Treino diferente entre os canais (tPHYRDL)",
                            "texto": f"O atraso de leitura que a placa achou no treino foi {v}. É comum, mas quando os "
                                     "timings estão no limite é o canal de valor maior que costuma errar primeiro."})
    temps = [h.get("temp_max") for h in historico()[-5:] if h.get("temp_max")]
    if temps and max(temps) >= (55 if r.get("tipo") == "DDR5" else 60):
        achados.append({"nivel": "aviso", "titulo": f"Pentes chegaram a {fmt_ns(max(temps))} °C no teste",
                        "texto": "Acima de 50-55 °C os pentes DDR5 começam a errar, principalmente com tREFI alto. "
                                 "Um ventilador soprando nos pentes ou um tREFI menor resolve."})
    t = atual.get("timings") if atual.get("ok") else None
    if t and t.get("comando") == "2T":
        achados.append({"nivel": "info", "titulo": "Comando em 2T", "texto": "1T é um pouco mais rápido, se for estável."})
    if t and r.get("tipo") == "DDR5" and t.get("trfc_ns") and t["trfc_ns"] >= 280:
        achados.append({"nivel": "info", "titulo": f"tRFC alto ({t['trfc_ns']} ns)",
                        "texto": "É o padrão da JEDEC, folgado. Baixar o tRFC é dos ajustes que mais rendem banda."})
    return achados


# Referência de cada timing: (nome, campo na BIOS, unidade, "bom" até, "apertado" até). Unidade "ns" compara o
# tempo real (vale para qualquer velocidade), "clk" compara clocks e "clk+" é o contrário (maior é mais apertado).
# Os valores em ns são de chips SK Hynix (DDR5) e Samsung B-die (DDR4); outros chips ganham folga (FOLGA_CHIP).
REFERENCIAS = {
    "DDR5": [("cl", "CAS Latency (tCL)", "DRAM CAS# Latency", "ns", 11.0, 10.0),
             ("trcd", "tRCD", "DRAM RAS# to CAS# Delay", "ns", 13.0, 12.0),
             ("trp", "tRP", "DRAM RAS# PRE Time", "ns", 13.0, 12.0),
             ("tras", "tRAS", "DRAM RAS# ACT Time", "ns", 16.5, 10.5),
             ("trc", "tRC", "DRAM RAS# to RAS# Delay (tRC)", "ns", 30.0, 22.5),
             ("trfc", "tRFC", "tRFC1 / DRAM REF Cycle Time", "ns", None, None),
             ("trefi", "tREFI", "tREFI / DRAM Refresh Interval", "clk+", 32768, 65535),
             ("trrds", "tRRD_S", "tRRD_S", "clk", 8, 6), ("trrdl", "tRRD_L", "tRRD_L", "clk", 12, 8),
             ("tfaw", "tFAW", "tFAW", "clk", 32, 24), ("twtrs", "tWTR_S", "tWTR_S", "clk", 4, 3),
             ("twtrl", "tWTR_L", "tWTR_L", "clk", 16, 12), ("twr", "tWR", "tWR", "clk", 48, 36),
             ("trtp", "tRTP", "tRTP", "clk", 12, 9), ("trdrdscl", "tRDRD_SCL", "tRDRDSCL", "clk", 5, 3),
             ("twrwrscl", "tWRWR_SCL", "tWRWRSCL", "clk", 5, 3)],
    "DDR4": [("cl", "CAS Latency (tCL)", "Tcl", "ns", 9.0, 7.8), ("trcd", "tRCD", "TrcdRd", "ns", 10.0, 8.4),
             ("trp", "tRP", "Trp", "ns", 10.0, 8.4), ("tras", "tRAS", "Tras", "ns", 18.0, 15.0),
             ("trc", "tRC", "Trc", "ns", 28.0, 23.0), ("trfc", "tRFC", "Trfc", "ns", None, None),
             ("trrds", "tRRD_S", "TrrdS", "clk", 4, 4), ("trrdl", "tRRD_L", "TrrdL", "clk", 6, 4),
             ("tfaw", "tFAW", "Tfaw", "clk", 16, 16), ("twtrs", "tWTR_S", "TwtrS", "clk", 4, 3),
             ("twtrl", "tWTR_L", "TwtrL", "clk", 12, 8), ("twr", "tWR", "Twr", "clk", 12, 10),
             ("trtp", "tRTP", "Trtp", "clk", 6, 5), ("trdrdscl", "tRDRD_SCL", "TrdrdScl", "clk", 4, 2),
             ("twrwrscl", "tWRWR_SCL", "TwrwrScl", "clk", 4, 2)],
}
FOLGA_CHIP = {"hynix": 1.0, "bdie": 1.0, "samsung": 1.1, "micron": 1.15, "24g": 1.1, "?": 1.12}
O_QUE_FAZ = {
    "cl": "espera entre pedir uma coluna e o dado sair: pesa direto na latência",
    "trcd": "espera para abrir uma linha antes de ler: pesa na latência",
    "trp": "espera para fechar a linha antes de abrir outra: pesa na latência",
    "tras": "tempo mínimo com a linha aberta; quase não muda o desempenho",
    "trc": "ciclo completo de uma linha (o certo é tRAS + tRP)",
    "trfc": "pausa a cada refresh: quanto menor, mais tempo a memória fica livre (banda)",
    "trefi": "intervalo entre refreshes: maior = menos pausas e mais banda, mas sensível a calor",
    "trrds": "espera entre abrir linhas em grupos diferentes", "trrdl": "espera entre abrir linhas no mesmo grupo",
    "tfaw": "janela para 4 linhas abertas (o ideal é 4 × tRRD_S)", "twtrs": "troca de escrita para leitura",
    "twtrl": "troca de escrita para leitura no mesmo grupo", "twr": "tempo para a escrita assentar antes de fechar",
    "trtp": "espera entre ler e fechar a linha", "trdrdscl": "leituras seguidas no mesmo grupo",
    "twrwrscl": "escritas seguidas no mesmo grupo",
}


def _estado_timing(v: float, modo: str, bom, apertado) -> str:
    if modo == "clk+":
        return "apertado" if v >= apertado else "bom" if v >= bom else "folgado"
    return "apertado" if v <= apertado + 1e-9 else "bom" if v <= bom + 1e-9 else "folgado"


def analise(k: dict, atual: dict) -> dict | None:
    """Cada timing que está valendo, em ns, comparado ao que o chip costuma aguentar, e o treino canal a canal."""
    if not atual.get("ok"):
        return None
    r = k.get("resumo") or {}
    tipo = atual.get("ddr") or r.get("tipo")
    t = atual.get("timings") or {}
    if tipo not in REFERENCIAS or not atual.get("mts"):
        return None
    ck = 2000 / atual["mts"]
    fam = _familia_chip(r.get("chip"), tipo)
    folga = FOLGA_CHIP.get(fam, 1.12)
    itens = []
    for chave, nome, bios, modo, bom, apertado in REFERENCIAS[tipo]:
        v = t.get(chave)
        if not v:
            continue
        ns = round(v * ck, 1)
        if chave == "trfc":
            af, ag = TRFC_NS[tipo].get(fam, TRFC_NS[tipo]["?"])
            bom, apertado = af * 1.1, (ag or af * 0.9) * 1.05
            estado = _estado_timing(ns, "ns", bom, apertado)
            ref = f"bom até {round(bom)} ns · apertado até {round(apertado)} ns"
        elif modo == "ns":
            bom, apertado = bom * folga, apertado * folga
            estado = _estado_timing(ns, modo, bom, apertado)
            ref = f"bom até {fmt_ns(bom)} ns ({math.ceil(bom / ck - 0.01)}) · apertado até {fmt_ns(apertado)} ns ({math.ceil(apertado / ck - 0.01)})"
        else:
            estado = _estado_timing(v, modo, bom, apertado)
            ref = f"bom a partir de {bom} · apertado em {apertado}" if modo == "clk+" else f"bom até {bom} · apertado até {apertado}"
        itens.append({"id": chave, "nome": nome, "bios": bios, "valor": v, "ns": ns if modo != "clk+" else round(v * ck / 1000, 1),
                      "unidade": "µs" if modo == "clk+" else "ns", "estado": estado, "referencia": ref,
                      "o_que_faz": O_QUE_FAZ.get(chave, "")})
    contagem = {e: sum(1 for i in itens if i["estado"] == e) for e in ("folgado", "bom", "apertado")}

    canais = atual.get("por_canal") or []
    diferentes = []
    if len(canais) > 1:
        for chave in POR_CANAL:
            valores = {c.get(chave) for c in canais if c.get(chave) is not None}
            if len(valores) > 1:
                diferentes.append(chave)
    return {"itens": itens, "contagem": contagem, "familia_chip": fam, "canais": canais, "diferentes": diferentes}


def fmt_ns(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",")


def coerencia(atual: dict) -> list[dict]:
    """Combinações de timings que não fazem sentido juntas (o controlador ignora ou erra)."""
    if not atual.get("ok"):
        return []
    t, achados = atual.get("timings") or {}, []
    if t.get("trc") and t.get("tras") and t.get("trp") and t["trc"] < t["tras"] + t["trp"]:
        achados.append({"nivel": "aviso", "titulo": f"tRC menor que tRAS + tRP ({t['trc']} < {t['tras'] + t['trp']})",
                        "texto": "O tRC devia ser pelo menos tRAS + tRP. Abaixo disso a memória costuma errar; "
                                 f"ponha tRC = {t['tras'] + t['trp']} ou suba o tRAS/tRP junto."})
    if t.get("tfaw") and t.get("trrds") and t["tfaw"] < 4 * t["trrds"]:
        achados.append({"nivel": "info", "titulo": f"tFAW abaixo de 4 × tRRD_S ({t['tfaw']} < {4 * t['trrds']})",
                        "texto": "Não ganha nada: com 4 aberturas espaçadas pelo tRRD_S a janela já passa do tFAW. "
                                 f"tFAW = {4 * t['trrds']} dá o mesmo desempenho."})
    if t.get("trrdl") and t.get("trrds") and t["trrdl"] < t["trrds"]:
        achados.append({"nivel": "info", "titulo": "tRRD_L menor que tRRD_S",
                        "texto": "O do mesmo grupo (L) costuma ser maior ou igual ao de grupos diferentes (S). Confira os valores."})
    if t.get("twtrl") and t.get("twtrs") and t["twtrl"] < t["twtrs"]:
        achados.append({"nivel": "info", "titulo": "tWTR_L menor que tWTR_S",
                        "texto": "O do mesmo grupo (L) costuma ser maior ou igual ao de grupos diferentes (S). Confira os valores."})
    if atual.get("ddr") == "DDR5" and t.get("tcwl") and t.get("cl") and t["tcwl"] > t["cl"]:
        achados.append({"nivel": "info", "titulo": "tCWL maior que o CL",
                        "texto": "No DDR5 o tCWL costuma ser CL − 2. Deixe em Auto se não tiver certeza."})
    if atual.get("power_down"):
        achados.append({"nivel": "info", "titulo": "Power Down ligado",
                        "texto": "Os pentes cochilam quando ficam parados, o que economiza um pouco de energia mas soma "
                                 "latência quando voltam. Na BIOS, \"Power Down Enable\" em Disabled."})
    return achados


def _estimar(base: dict, alvo: dict, latencia_medida: float | None) -> dict:
    """Ganho estimado em latência (ns) e banda (%) entre duas configurações."""
    def ciclo_acesso(c):  # tempo para abrir uma linha nova e ler: tRP + tRCD + CL
        return (c["trp"] + c["trcd"] + c["cl"]) * 2000 / c["mts"]

    d_ns = ciclo_acesso(alvo) - ciclo_acesso(base)
    if base.get("modo_uclk") == "1:2" and alvo.get("modo_uclk") == "1:1":
        d_ns -= 8
    if base.get("fclk") and alvo.get("fclk") and alvo["fclk"] != base["fclk"]:
        d_ns -= (alvo["fclk"] - base["fclk"]) / 100 * 0.9

    def eficiencia(c):  # parte do tempo em que a memória não está ocupada fazendo refresh
        if not c.get("trefi") or not c.get("trfc"):
            return 1.0
        return 1 - c["trfc"] / c["trefi"]

    d_banda = (alvo["mts"] * eficiencia(alvo)) / (base["mts"] * eficiencia(base)) - 1
    saida = {"latencia_ns": round(d_ns, 1), "banda_pct": round(d_banda * 100, 1)}
    if latencia_medida:
        saida["latencia_estimada"] = round(max(45.0, latencia_medida + d_ns), 1)
    return saida


def sugestoes(k: dict, atual: dict, plat: dict, latencia_medida: float | None = None) -> dict:
    """Três níveis de configuração para a BIOS, do mais seguro ao mais agressivo."""
    r = k.get("resumo") or {}
    tipo = r.get("tipo") or plat.get("ddr")
    if tipo not in ("DDR4", "DDR5"):
        return {"niveis": [], "motivo": "Não reconheci o tipo de memória."}
    t = atual.get("timings") if atual.get("ok") else {}
    c = (atual.get("clocks") or {}) if atual.get("ok") else {}
    mts_kit = r.get("perfil_mts") or r.get("atual_mts") or (6000 if tipo == "DDR5" else 3200)
    jedec_mts = min(JEDEC[tipo], key=lambda v: abs(v - mts_kit))
    cl_kit = r.get("perfil_cl") or JEDEC[tipo][jedec_mts][0]
    trcd_kit = r.get("perfil_trcd") or (t.get("trcd") if t and atual.get("mts") == mts_kit else None) or \
        max(cl_kit, JEDEC[tipo][jedec_mts][1] if not r.get("perfil_cl") else math.ceil(cl_kit * 1.2))
    fam = _familia_chip(r.get("chip"), tipo)
    am5, am4 = plat.get("soquete") == "AM5", plat.get("soquete") == "AM4"

    # velocidade alvo: o ponto doce da plataforma (com controlador 1:1) ou a do kit
    alvo_mts = mts_kit
    if am5 and mts_kit > 6400:
        alvo_mts = 6400 if plat.get("geracao") == "Zen 5" else 6000
    elif am4 and mts_kit > 3800:
        alvo_mts = 3600
    escala = alvo_mts / mts_kit
    cl1 = _par(math.ceil(cl_kit * escala)) if escala < 1 else cl_kit
    trcd1 = math.ceil(trcd_kit * escala) if escala < 1 else trcd_kit

    mts_base = atual.get("mts") or r.get("atual_mts") or alvo_mts
    ck_base = 2000 / mts_base
    spd = r.get("spd") or {}
    trfc_jedec = spd.get("trfc1_ns") or (295 if tipo == "DDR5" else 350)  # o do SPD já vem pela densidade do chip
    if t:
        base = {"mts": mts_base, "cl": t["cl"], "trcd": t["trcd"], "trp": t["trp"], "trfc": t.get("trfc"), "trefi": t.get("trefi")}
    else:  # sem leitura: JEDEC se o perfil está desligado, senão o perfil do kit
        perfil_off = bool(r.get("perfil_mts") and mts_base < r["perfil_mts"] * 0.92)
        jed = JEDEC[tipo][min(JEDEC[tipo], key=lambda v: abs(v - mts_base))]
        if perfil_off and spd.get("base_cl") and spd.get("base_mts") == mts_base:
            jed = (spd["base_cl"], spd.get("base_trcd") or jed[1])  # o perfil base gravado no próprio pente
        cl_b, rcd_b = (jed[0], jed[1]) if perfil_off else (cl_kit, trcd_kit)
        base = {"mts": mts_base, "cl": cl_b, "trcd": rcd_b, "trp": rcd_b,
                "trfc": math.ceil(trfc_jedec / ck_base), "trefi": math.ceil((3900 if tipo == "DDR5" else 7800) / ck_base)}
    base.update(modo_uclk=c.get("modo_uclk"), fclk=c.get("fclk"))
    ck = 2000 / alvo_mts

    def ciclos(ns):
        return int(math.ceil(ns / ck))

    niveis = []
    # 1) perfil do fabricante
    itens = [{"campo": "Perfil de memória", "bios": "AI Overclock Tuner / Extreme Memory Profile (EXPO, XMP ou DOCP)",
              "valor": f"{'EXPO' if plat.get('marca') == 'amd' else 'XMP'} ligado" + (f" ({mts_kit} CL{cl_kit})" if r.get("perfil_cl") else "")}]
    if alvo_mts != mts_kit:
        itens.append({"campo": "Velocidade", "bios": "DRAM Frequency / Memory Frequency", "valor": f"{alvo_mts} MT/s",
                      "nota": f"o kit faz {mts_kit}, mas nesta plataforma {alvo_mts} com o controlador 1:1 tem menos latência"})
        itens.append({"campo": "CAS Latency (tCL)", "bios": "DRAM CAS# Latency", "valor": str(cl1)})
        itens.append({"campo": "tRCD / tRP", "bios": "DRAM RAS# to CAS# Delay / DRAM RAS# PRE Time", "valor": f"{trcd1} / {trcd1}"})
    if am5:
        itens.append({"campo": "Controlador (UCLK)", "bios": "UCLK DIV1 MODE", "valor": "UCLK=MEMCLK"})
        itens.append({"campo": "Infinity Fabric", "bios": "Infinity Fabric Frequency and Dividers (FCLK)",
                      "valor": "2000 MHz" if alvo_mts <= 6400 else "Auto"})
    elif am4:
        itens.append({"campo": "Infinity Fabric", "bios": "Infinity Fabric Frequency and Dividers (FCLK)",
                      "valor": f"{alvo_mts // 2} MHz (1:1)"})
    elif plat.get("marca") == "intel" and tipo == "DDR4":
        itens.append({"campo": "Gear", "bios": "Gear Mode / Memory Controller : DRAM Frequency Ratio", "valor": "Gear 1"})
    n1 = {"mts": alvo_mts, "cl": cl1, "trcd": trcd1, "trp": trcd1, "trfc": ciclos(trfc_jedec),
          "trefi": ciclos(3900 if tipo == "DDR5" else 7800), "modo_uclk": "1:1" if am5 else None,
          "fclk": 2000 if am5 else (alvo_mts // 2 if am4 else None)}
    niveis.append({"id": "perfil", "nome": "Perfil do fabricante", "risco": "seguro", "itens": itens, "valores": {},
                   "texto": "O que o kit promete de fábrica, com os ajustes de plataforma que quase todo PC aguenta.",
                   "estimativa": _estimar(base, n1, latencia_medida)})

    # 2) afinado: tRFC pelo chip e secundários que costumam passar
    trfc_af, trfc_ag = TRFC_NS[tipo].get(fam, TRFC_NS[tipo]["?"])
    itens = [{"campo": "Tudo do nível 1", "bios": "", "valor": "mantém"}]
    n2 = dict(n1)
    valores2 = {"trfc": ciclos(trfc_af)}
    if tipo == "DDR5":
        valores2.update(trefi=32768, trrds=8, trrdl=12, tfaw=32, twtrs=4, twtrl=16, **({"twr": 48, "trtp": 12} if fam == "hynix" else {}))
        n2.update(trfc=ciclos(trfc_af), trefi=32768)
        itens += [{"campo": "tRFC", "bios": "tRFC1 / DRAM REF Cycle Time", "valor": f"{ciclos(trfc_af)} ({trfc_af} ns)"},
                  {"campo": "tREFI", "bios": "tREFI / DRAM Refresh Interval", "valor": "32768",
                   "nota": "refresh menos frequente: mais banda; mantenha o pente ventilado"},
                  {"campo": "tRRDS / tRRDL / tFAW", "bios": "tRRD_S / tRRD_L / tFAW", "valor": "8 / 12 / 32"},
                  {"campo": "tWTRS / tWTRL", "bios": "tWTR_S / tWTR_L", "valor": "4 / 16"}]
        if fam == "hynix":
            itens.append({"campo": "tWR / tRTP", "bios": "tWR / tRTP", "valor": "48 / 12", "nota": "no DDR5 os dois andam juntos"})
    else:
        valores2.update(trrds=4, trrdl=6, tfaw=16, twtrs=4, twtrl=12, twr=12, trtp=6)
        n2.update(trfc=ciclos(trfc_af))
        itens += [{"campo": "tRFC", "bios": "tRFC / Trfc", "valor": f"{ciclos(trfc_af)} ({trfc_af} ns)"},
                  {"campo": "tRRDS / tRRDL / tFAW", "bios": "TrrdS / TrrdL / Tfaw", "valor": "4 / 6 / 16"},
                  {"campo": "tWTRS / tWTRL", "bios": "TwtrS / TwtrL", "valor": "4 / 12"},
                  {"campo": "tWR / tRTP", "bios": "Twr / Trtp", "valor": "12 / 6"}]
    itens.append({"campo": "O resto", "bios": "", "valor": "Auto"})
    niveis.append({"id": "afinado", "nome": "Afinado", "risco": "baixo", "itens": itens, "valores": valores2,
                   "texto": "Aperta os timings que mais pesam (refresh e secundários) sem mexer em tensão. "
                            + ("Valores para chips SK Hynix." if fam == "hynix" else "Valores folgados, porque o chip não é conhecido."
                               if fam == "?" else ""),
                   "estimativa": _estimar(base, n2, latencia_medida)})

    # 3) agressivo: só para chips que aguentam com segurança conhecida
    n3 = dict(n2)
    itens, texto = [{"campo": "Tudo do nível 2", "bios": "", "valor": "mantém"}], ""
    valores3 = dict(valores2)
    if tipo == "DDR5" and fam == "hynix":
        cl3 = max(28, min(cl1, 30)) if alvo_mts <= 6000 else max(30, cl1 - 2)
        n3.update(cl=cl3, trcd=36 if alvo_mts <= 6400 else trcd1, trp=36 if alvo_mts <= 6400 else trcd1,
                  trfc=ciclos(trfc_ag), trefi=65535)
        itens += [{"campo": "CL-tRCD-tRP", "bios": "CAS# / RAS# to CAS# / RAS# PRE", "valor": f"{cl3}-{n3['trcd']}-{n3['trp']}"},
                  {"campo": "tRAS / tRC", "bios": "tRAS / tRC", "valor": f"30 / {30 + n3['trp']}"},
                  {"campo": "Tensão VDD / VDDQ", "bios": "DRAM VDD / DRAM VDDQ Voltage", "valor": "1,40 V",
                   "nota": "não passe de 1,45 V sem ventilação nos pentes"},
                  {"campo": "tRFC", "bios": "tRFC1", "valor": f"{ciclos(trfc_ag)} ({trfc_ag} ns)"},
                  {"campo": "tREFI", "bios": "tREFI", "valor": "65535", "nota": "pentes acima de 50 °C podem dar erro"},
                  {"campo": "tRRDL / tWR / tRTP", "bios": "tRRD_L / tWR / tRTP", "valor": "8 / 48 / 12"}]
        texto = "Os timings da comunidade para Hynix a 6000. Precisa de mais tensão e de teste longo."
        valores3.update(cl=cl3, trcd=n3["trcd"], trp=n3["trp"], tras=30, trc=30 + n3["trp"], trfc=ciclos(trfc_ag), trefi=65535,
                        trrdl=8, twr=48, trtp=12, vdd=1400, vddq=1400)
    elif tipo == "DDR4" and fam == "bdie":
        cl3 = 14 if alvo_mts <= 3600 else 16
        n3.update(cl=cl3, trcd=cl3 + 1, trp=cl3 + 1, trfc=ciclos(trfc_ag))
        itens += [{"campo": "CL-tRCD-tRP", "bios": "Tcl / TrcdRd / Trp", "valor": f"{cl3}-{cl3 + 1}-{cl3 + 1}"},
                  {"campo": "Tensão", "bios": "DRAM Voltage", "valor": "1,45 V", "nota": "B-die aguenta até 1,50 V com ventilação"},
                  {"campo": "tRFC", "bios": "Trfc", "valor": f"{ciclos(trfc_ag)} ({trfc_ag} ns)"},
                  {"campo": "tRRDS / tRRDL / tFAW", "bios": "TrrdS / TrrdL / Tfaw", "valor": "4 / 4 / 16"}]
        texto = "Samsung B-die é o chip que mais aperta. Precisa de tensão e de teste longo."
        valores3.update(cl=cl3, trcd=cl3 + 1, trp=cl3 + 1, trfc=ciclos(trfc_ag), trrds=4, trrdl=4, tfaw=16)
    else:
        n3 = None
        texto = ("Sem valores agressivos confiáveis para esse chip. A partir do nível 2, aperte um timing por vez "
                 "(CL, depois tRCD/tRP, depois tRFC) e teste cada passo aqui.")
    niveis.append({"id": "agressivo", "nome": "Agressivo", "risco": "alto" if n3 else "manual", "itens": itens if n3 else [],
                   "valores": valores3 if n3 else {},
                   "texto": texto, "estimativa": _estimar(base, n3, latencia_medida) if n3 else None})
    return {"niveis": niveis, "familia_chip": fam, "alvo_mts": alvo_mts}


# ------------------------------------------------------------------ temperatura dos pentes
def temperatura_pentes() -> list[float]:
    """Temperatura de cada pente (sensor do SPD, lido pela LHM). Vazio se o pente não tem sensor ou falta admin."""
    if not windows.EH_WINDOWS:
        return []
    try:
        from . import sensores
        linhas, _ = sensores.instancia().linhas()
    except Exception:
        return []
    return [round(v, 1) for _, hw, tipo_hw, tipo, nome, v, *_ in linhas
            if tipo_hw == "Memory" and tipo == "Temperature" and v is not None and 5 < v < 120
            and nome.upper().startswith("DIMM")]


# ------------------------------------------------------------------ teste: latência, banda e estabilidade
def _lib():
    from . import oc_cpu
    d = oc_cpu._estavel()
    if not getattr(d, "_ram_pronta", False):
        d.ram_testar.restype = ctypes.c_int64
        d.ram_testar.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
                                 ctypes.POINTER(ctypes.c_int)]
        d._ram_pronta = True
    return d


_SCRIPT_WHEA_RAM = r"""
$t=[datetime]::Parse('%(desde)s')
@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WHEA-Logger';StartTime=$t} -ErrorAction SilentlyContinue | Where-Object { $_.Id -in 1,18,19,47 }).Count | ConvertTo-Json -Compress
"""


def whea_desde(instante: float) -> int:
    if not windows.EH_WINDOWS:
        return 0
    desde = datetime.fromtimestamp(instante).strftime("%Y-%m-%dT%H:%M:%S")
    saida = windows.executar(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              _SCRIPT_WHEA_RAM % {"desde": desde}], 60).strip()
    try:
        return int(json.loads(saida or "0"))
    except (ValueError, TypeError):
        return 0


def _arquivo():
    return config.pasta_dados() / "ram_timings.json"


def historico() -> list[dict]:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _guardar(item: dict) -> None:
    _arquivo().write_text(json.dumps((historico() + [item])[-40:], ensure_ascii=False), encoding="utf-8")


def apagar_historico() -> None:
    _arquivo().unlink(missing_ok=True)


def melhor(lista: list[dict] | None = None) -> dict | None:
    """A configuração estável de menor latência (com teste de pelo menos 10 minutos)."""
    lista = historico() if lista is None else lista
    bons = [h for h in lista if h.get("estavel") and h.get("minutos", 0) >= 10 and h.get("latencia")]
    return min(bons, key=lambda h: (h["latencia"], -(h.get("leitura") or 0))) if bons else None


def _assinatura(atual: dict, k: dict) -> dict:
    """O que identifica a configuração testada (para comparar depois)."""
    r = k.get("resumo") or {}
    if atual.get("ok"):
        t = atual["timings"]
        return {"mts": atual["mts"], "cl": t["cl"], "trcd": t["trcd"], "trp": t["trp"], "tras": t["tras"],
                "trfc_ns": t.get("trfc_ns"), "trefi": t.get("trefi"), "lido": True,
                "fclk": (atual.get("clocks") or {}).get("fclk"), "modo_uclk": (atual.get("clocks") or {}).get("modo_uclk")}
    return {"mts": r.get("atual_mts"), "lido": False}


class TesteRam:
    """Latência/banda e depois estabilidade por `minutos`, em segundo plano."""

    def __init__(self, minutos: float, rotulo: str = "", medir: bool = True, fracao: float = 0.7,
                 ler_atual=leitura_atual, ler_kit=kit, medir_latencia=None, whea=None, temperaturas=None):
        self.minutos = max(0.05, float(minutos))
        self.rotulo = (rotulo or "").strip()[:60]
        self.medir = medir
        self.fracao = min(0.85, max(0.2, fracao))
        self.fase = "preparando"
        self.etapa = "Preparando..."
        self.progresso = 0.0
        self.erros = 0
        self.conferido_gb = 0.0
        self.alocado_gb = 0.0
        self.whea = 0
        self.resultado: dict | None = None
        self.erro = None
        self.cancelado = False
        self.inicio = time.time()
        self.fim = None
        self._parar = ctypes.c_int(0)
        self._ler_atual, self._ler_kit = ler_atual, ler_kit
        self._medir_latencia = medir_latencia or (lambda prog: memoria.executar(prog))
        self._whea = whea or whea_desde
        self._temperaturas = temperaturas or temperatura_pentes
        self.temp_max: float | None = None
        self.temp_agora: float | None = None
        self._fio = threading.Thread(target=self._rodar, daemon=True, name="teste-ram")
        self._fio.start()

    @property
    def rodando(self) -> bool:
        return self.fim is None

    def cancelar(self) -> None:
        self.cancelado = True
        self._parar.value = 1

    def aguardar(self, timeout=None) -> None:
        self._fio.join(timeout)

    def _rodar(self) -> None:
        try:
            atual = self._ler_atual(True)
            k = self._ler_kit()
            medida = None
            if self.medir and not self.cancelado:
                self.fase, self.etapa = "latencia", "Medindo latência e banda..."
                medida = self._medir_latencia(lambda f, e: setattr(self, "progresso", 0.15 * f))
            if not self.cancelado:
                self._estabilidade()
            if self.cancelado and not self.conferido_gb:
                return
            # estável só se cada byte foi conferido pelo menos uma vez, sem erro
            completo = self.conferido_gb >= self.alocado_gb * 0.99
            estavel = not self.cancelado and completo and self.erros == 0 and self.whea == 0
            minutos = (time.time() - self._inicio_estab) / 60 if hasattr(self, "_inicio_estab") else 0
            self.resultado = {"quando": datetime.now().strftime("%d/%m/%Y %H:%M"), "rotulo": self.rotulo,
                              "config": _assinatura(atual, k), "latencia": medida and medida.get("latencia"),
                              "leitura": medida and medida.get("leitura"), "escrita": medida and medida.get("escrita"),
                              "copia": medida and medida.get("copia"), "minutos": round(minutos, 1),
                              "erros": self.erros, "whea": self.whea, "conferido_gb": round(self.conferido_gb, 1),
                              "gb_testados": self.alocado_gb, "estavel": estavel, "interrompido": self.cancelado,
                              "curto": not completo and not self.cancelado, "temp_max": self.temp_max}
            _guardar(self.resultado)
        except Exception as e:
            log.exception("teste de RAM falhou")
            self.erro = str(e)
        finally:
            self.fase = "fim"
            self.progresso = 1.0
            self.fim = time.time()

    def _estabilidade(self) -> None:
        self.fase, self.etapa = "estabilidade", "Testando estabilidade..."
        lib = _lib()
        livre = psutil.virtual_memory().available
        nucleos = max(1, min(16, psutil.cpu_count() or 4))
        total = int(livre * self.fracao)
        # cada fio testa um bloco de tamanho potência de 2; mais fios cobrem o que sobraria do arredondamento
        por_fio = max(64 << 20, 1 << int(math.log2(max(1, total // nucleos))))
        fios = max(1, min(nucleos * 2, total // por_fio))
        self.alocado_gb = round(por_fio * fios / 2**30, 1)
        self._inicio_estab = time.time()
        ms = int(self.minutos * 60_000)
        contadores = [ctypes.c_uint64(0) for _ in range(fios)]
        erros = [0] * fios

        def trabalho(i):
            r = lib.ram_testar(por_fio, ms, int(time.time() * 1000) ^ (i << 32), ctypes.byref(contadores[i]),
                               ctypes.byref(self._parar))
            erros[i] = r if r > 0 else 0

        threads = [threading.Thread(target=trabalho, args=(i,), daemon=True) for i in range(fios)]
        for t in threads:
            t.start()
        proxima_temp = 0.0
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.5)
            if time.time() >= proxima_temp:  # o pente esquenta com o teste: guarda o pico
                proxima_temp = time.time() + 5
                try:
                    temps = self._temperaturas()
                except Exception:
                    temps = []
                if temps:
                    self.temp_agora = max(temps)
                    self.temp_max = max(self.temp_max or 0, self.temp_agora)
            decorrido = time.time() - self._inicio_estab
            self.conferido_gb = sum(c.value for c in contadores) * 8 / 2**30
            self.progresso = (0.15 if self.medir else 0) + (0.8 if self.medir else 0.95) * min(1.0, decorrido / (ms / 1000))
            self.etapa = f"Testando estabilidade · {int(decorrido // 60)}:{int(decorrido % 60):02d} de {int(self.minutos)}:{int(self.minutos * 60 % 60):02d}"
        self.erros = sum(erros)
        self.conferido_gb = sum(c.value for c in contadores) * 8 / 2**30
        self.fase, self.etapa = "whea", "Conferindo erros de hardware do Windows..."
        self.whea = self._whea(self._inicio_estab - 1)

    def estado(self) -> dict:
        return {"rodando": self.rodando, "fase": self.fase, "etapa": self.etapa, "progresso": round(self.progresso, 3),
                "erros": self.erros, "conferido_gb": round(self.conferido_gb, 1), "gb": self.alocado_gb, "minutos": self.minutos,
                "resultado": self.resultado, "erro": self.erro, "cancelado": self.cancelado,
                "temp_agora": self.temp_agora, "temp_max": self.temp_max}


_teste: TesteRam | None = None


def iniciar_teste(minutos: float, rotulo: str = "", medir: bool = True) -> dict:
    global _teste
    if _teste and _teste.rodando:
        return {"ok": False, "mensagem": "O teste já está rodando."}
    _teste = TesteRam(minutos, rotulo, medir)
    return {"ok": True, "mensagem": "Teste da memória começou. Deixe o PC parado."}


def cancelar_teste() -> dict:
    if _teste and _teste.rodando:
        _teste.cancelar()
    return {"ok": True, "mensagem": "Parando o teste..."}


def estado_teste() -> dict | None:
    return _teste.estado() if _teste else None


def em_uso() -> bool:
    return bool(_teste and _teste.rodando)


def encerrar() -> None:
    if _teste and _teste.rodando:
        _teste.cancelar()


def estado(forcar: bool = False) -> dict:
    """Tudo o que a página precisa."""
    k = kit(forcar)
    plat = plataforma()
    atual = leitura_atual(forcar)
    ultimo = next(iter(reversed(memoria.historico())), None)
    lat = ultimo.get("latencia") if ultimo else None
    hist = historico()
    return {"kit": k, "plataforma": plat, "atual": atual, "diagnostico": diagnostico(k, atual, plat) + coerencia(atual),
            "analise": analise(k, atual), "temperaturas": temperatura_pentes(),
            "sugestoes": sugestoes(k, atual, plat, lat), "latencia_medida": lat, "historico": list(reversed(hist)),
            "melhor": melhor(hist), "teste": estado_teste(), "admin": windows.eh_admin(), "pawnio": pawnio.disponivel()}
