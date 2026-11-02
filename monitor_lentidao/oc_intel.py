"""Ajustes dos processadores Intel Core, como o Intel XTU.

Usa o módulo IntelMSR do PawnIO, que só deixa escrever os registradores de limite de energia (PL1/PL2), da
caixa de overclock (undervolt) e do limite de corrente (IccMax). Tudo é temporário: a BIOS reaplica os valores
dela quando o PC reinicia.

- PL1 / PL2 / Tau: energia sustentada, energia de turbo e por quanto tempo o turbo dura.
- IccMax: corrente máxima do processador.
- Undervolt (offset de tensão do núcleo e do cache): negativo = menos calor e mais clock sustentado.
  Em muitos notebooks e em placas com "Undervolt Protection" ligada a BIOS bloqueia; o monitor confere lendo de volta.
"""

import logging
import math
import re

import psutil

from . import config, pawnio

log = logging.getLogger(__name__)

MSR_PLATFORM_INFO, MSR_RAPL_UNIDADE, MSR_PL, MSR_POWER_INFO = 0xCE, 0x606, 0x610, 0x614
MSR_ICCMAX, MSR_OC_MAILBOX, MSR_TURBO, MSR_TJMAX = 0x601, 0x150, 0x1AD, 0x1A2
PLANO_NUCLEO, PLANO_CACHE = 0, 2
UV_MIN, UV_MAX = -200, 0

# Perfil padrão da Intel para 13ª e 14ª geração desbloqueadas (orientação de 2024): (PL1, PL2, IccMax)
PADRAO_INTEL = {"i9": (253, 253, 307), "i7": (253, 253, 307), "i5": (181, 181, 200)}


class Msr:
    """Transporte real: PawnIO. Os testes trocam por um falso."""

    def __init__(self):
        self.mod = pawnio.modulo("IntelMSR")

    def ler(self, msr: int) -> int:
        return self.mod.executar("ioctl_read_msr", [msr], 1)[0]

    def escrever(self, msr: int, valor: int) -> None:
        self.mod.executar("ioctl_write_msr", [msr, valor & 0xFFFFFFFFFFFFFFFF], 0)


def _uv_codigo(mv: float) -> int:
    return (round(mv * 1.024) & 0x7FF) << 21


def _uv_mv(dados: int) -> int:
    x = (dados >> 21) & 0x7FF
    if x & 0x400:
        x -= 0x800
    return round(x / 1.024)


class ProcessadorIntel:
    fabricante = "cpu"
    marca = "intel"

    def __init__(self, nome: str, msr=None):
        self.nome = nome
        self.msr = msr or Msr()
        u = self.msr.ler(MSR_RAPL_UNIDADE)
        self.u_energia = 1 / (1 << (u & 0xF))
        self.u_tempo = 1 / (1 << ((u >> 16) & 0xF))
        self.nucleos = psutil.cpu_count(logical=False) or 1
        self._uv_ok: bool | None = None

    # ------------------------------------------------------------ limites de energia
    def _pl(self) -> dict:
        v = self.msr.ler(MSR_PL)
        return {"bruto": v, "pl1": round((v & 0x7FFF) * self.u_energia), "pl2": round(((v >> 32) & 0x7FFF) * self.u_energia),
                "tau": round(self._tau(v >> 17), 1), "travado": bool(v >> 63 & 1)}

    def _tau(self, campo: int) -> float:
        y, z = campo & 0x1F, (campo >> 5) & 0x3
        return (2 ** y) * (1 + z / 4) * self.u_tempo

    def _tau_campo(self, segundos: float) -> int:
        melhor, erro = 0, 1e9
        for y in range(32):
            for z in range(4):
                t = (2 ** y) * (1 + z / 4) * self.u_tempo
                if abs(t - segundos) < erro:
                    melhor, erro = y | (z << 5), abs(t - segundos)
        return melhor

    def _tdp(self) -> int | None:
        try:
            v = self.msr.ler(MSR_POWER_INFO)
        except Exception:
            return None
        tdp = round((v & 0x7FFF) * self.u_energia)
        return tdp if 5 <= tdp <= 400 else None

    def _iccmax(self) -> dict | None:
        try:
            v = self.msr.ler(MSR_ICCMAX)
        except Exception:
            return None
        return {"bruto": v, "a": round((v & 0x1FFF) / 8), "travado": bool(v >> 31 & 1)}

    # ------------------------------------------------------------ undervolt (caixa de overclock)
    def _caixa(self, plano: int, comando: int, dados: int = 0) -> tuple[int, int]:
        self.msr.escrever(MSR_OC_MAILBOX, (1 << 63) | (plano << 40) | (comando << 32) | (dados & 0xFFFFFFFF))
        for _ in range(100):  # o bit 63 fica ligado enquanto o processador trabalha no pedido
            r = self.msr.ler(MSR_OC_MAILBOX)
            if not r >> 63 & 1:
                break
        else:
            raise TimeoutError("a caixa de overclock não respondeu")
        return (r >> 32) & 0xFF, r & 0xFFFFFFFF

    def _uv(self, plano: int) -> int | None:
        try:
            erro, dados = self._caixa(plano, 0x10)
        except Exception:
            return None
        return None if erro else _uv_mv(dados)

    def _definir_uv(self, plano: int, mv: int) -> str | None:
        try:
            erro, _ = self._caixa(plano, 0x11, _uv_codigo(mv))
        except Exception as e:
            return getattr(e, "texto", str(e))
        if erro:
            return "a BIOS bloqueou o undervolt (Undervolt Protection ou CFG Lock)"
        lido = self._uv(plano)
        if lido is not None and abs(lido - mv) > 2:
            return "a BIOS bloqueou o undervolt (o valor não ficou)"
        return None

    # ------------------------------------------------------------ painel
    def _orig(self) -> dict:
        """Valores da BIOS nesta sessão do Windows (para "Voltar ao padrão")."""
        mem = (config.preferencias().get("cpu_original") or {}).get(self.nome) or {}
        if mem.get("boot") and abs(mem["boot"] - psutil.boot_time()) < 5:
            return mem
        pl = self._pl()
        icc = self._iccmax()
        mem = {"boot": psutil.boot_time(), "pl1": pl["pl1"], "pl2": pl["pl2"], "tau": pl["tau"],
               "iccmax": icc["a"] if icc else None, "uv": self._uv(PLANO_NUCLEO) or 0}
        tudo = config.preferencias().get("cpu_original") or {}
        tudo[self.nome] = mem
        config.definir_preferencia("cpu_original", tudo)
        return mem

    def controles(self) -> list[dict]:
        orig = self._orig()
        pl = self._pl()
        tdp = self._tdp() or 65
        c = []
        if not pl["travado"]:
            teto = max(300, tdp * 4)
            c += [{"id": "pl1", "rotulo": "PL1 · energia sustentada", "unidade": "W", "min": 10, "max": teto, "passo": 1,
                   "padrao": orig["pl1"], "atual": pl["pl1"], "grupo": "energia",
                   "dica": f"Energia que o processador sustenta sem limite de tempo (TDP de fábrica: {tdp} W)."},
                  {"id": "pl2", "rotulo": "PL2 · energia de turbo", "unidade": "W", "min": 10, "max": teto + 100, "passo": 1,
                   "padrao": orig["pl2"], "atual": pl["pl2"], "grupo": "energia",
                   "dica": "Energia permitida durante o turbo curto."},
                  {"id": "tau", "rotulo": "Tau · duração do turbo", "unidade": "s", "min": 1, "max": 128, "passo": 1,
                   "padrao": round(orig["tau"]), "atual": round(pl["tau"]), "grupo": "energia",
                   "dica": "Por quanto tempo (em média) o processador pode ficar no PL2 antes de voltar ao PL1."}]
        icc = self._iccmax()
        if icc and not icc["travado"] and icc["a"]:
            c.append({"id": "iccmax", "rotulo": "IccMax · corrente máxima", "unidade": "A", "min": 50, "max": 511, "passo": 1,
                      "padrao": orig.get("iccmax") or icc["a"], "atual": icc["a"], "grupo": "energia",
                      "dica": "Limite de corrente. Em i7/i9 de 13ª e 14ª geração a Intel recomenda até 307 A."})
        uv = self._uv(PLANO_NUCLEO)
        if uv is not None:
            c.append({"id": "uv", "rotulo": "Undervolt · núcleo e cache", "unidade": "mV", "min": UV_MIN, "max": UV_MAX,
                      "passo": 5, "padrao": orig.get("uv") or 0, "atual": uv, "sinal": True, "grupo": "tensao",
                      "dica": "Tira tensão do processador: menos calor e mais clock sustentado. Vá de 10 em 10 mV e teste."})
        return c

    def leituras(self) -> list[list[str]]:
        saida = []
        pl = self._pl()
        if pl["travado"]:
            saida.append(["Limites de energia", "travados pela BIOS"])
        try:
            t = self.msr.ler(MSR_TURBO) & 0xFF
            if t:
                saida.append(["Turbo máximo", f"{t * 100} MHz"])
            tj = (self.msr.ler(MSR_TJMAX) >> 16) & 0xFF
            if tj:
                saida.append(["Temperatura máxima (TjMax)", f"{tj} °C"])
        except Exception:
            pass
        return saida

    def info(self) -> dict:
        presets = []
        m = re.search(r"\b(i[579])-1[34]\d{3}K", self.nome, re.I)
        if m:
            pl1, pl2, icc = PADRAO_INTEL[m.group(1).lower()]
            presets.append({"nome": "Padrão da Intel (13ª/14ª)", "valores": {"pl1": pl1, "pl2": pl2, "iccmax": icc}})
        tdp = self._tdp()
        if tdp:
            presets.append({"nome": f"Só o TDP ({tdp} W)", "valores": {"pl1": tdp, "pl2": tdp}})
        return {"marca": "intel", "nucleos": self.nucleos, "por_nucleo": False, "presets": presets,
                "uv_min": UV_MIN, "uv_max": UV_MAX}

    # ------------------------------------------------------------ escrita
    def aplicar(self, valores: dict, so_mudancas: bool = True) -> list[dict]:
        ctrl = {c["id"]: c for c in self.controles()}
        saida = []
        valores = {k: int(round(float(v))) for k, v in valores.items()}
        mudar_pl = {}
        for ident, valor in valores.items():
            c = ctrl.get(ident)
            if c is None:
                saida.append({"id": ident, "ok": False, "mensagem": "não disponível neste processador (ou travado pela BIOS)"})
                continue
            if so_mudancas and valor == c["atual"]:
                continue
            if not c["min"] <= valor <= c["max"]:
                saida.append({"id": ident, "ok": False, "mensagem": f"fora da faixa ({c['min']} a {c['max']})"})
                continue
            if ident in ("pl1", "pl2", "tau"):
                mudar_pl[ident] = valor
            elif ident == "iccmax":
                saida.append(self._aplicar_icc(valor))
            elif ident == "uv":
                erro = self._definir_uv(PLANO_NUCLEO, valor) or self._definir_uv(PLANO_CACHE, valor)
                saida.append({"id": ident, "ok": erro is None, "mensagem": erro or "aplicado"})
        if mudar_pl:
            saida += self._aplicar_pl(mudar_pl)
        return saida

    def _aplicar_pl(self, mudar: dict) -> list[dict]:
        v = self.msr.ler(MSR_PL)
        if v >> 63 & 1:
            return [{"id": k, "ok": False, "mensagem": "a BIOS travou os limites de energia"} for k in mudar]
        if "pl1" in mudar:
            v = (v & ~0x7FFF) | (min(0x7FFF, math.floor(mudar["pl1"] / self.u_energia))) | (1 << 15)
        if "pl2" in mudar:
            v = (v & ~(0x7FFF << 32)) | (min(0x7FFF, math.floor(mudar["pl2"] / self.u_energia)) << 32) | (1 << 47)
        if "tau" in mudar:
            v = (v & ~(0x7F << 17)) | (self._tau_campo(mudar["tau"]) << 17)
        try:
            self.msr.escrever(MSR_PL, v)
        except Exception as e:
            return [{"id": k, "ok": False, "mensagem": getattr(e, "texto", str(e))} for k in mudar]
        lido = self._pl()
        saida = []
        for k, alvo in mudar.items():
            ok = abs(lido[k] - alvo) <= (2 if k != "tau" else max(1, alvo * 0.2))
            saida.append({"id": k, "ok": ok, "mensagem": "aplicado" if ok else "o processador não aceitou o valor"})
        return saida

    def _aplicar_icc(self, a: int) -> dict:
        icc = self._iccmax()
        if not icc or icc["travado"]:
            return {"id": "iccmax", "ok": False, "mensagem": "a BIOS travou o IccMax"}
        try:
            self.msr.escrever(MSR_ICCMAX, (icc["bruto"] & ~0x1FFF) | min(0x1FFF, a * 8))
        except Exception as e:
            return {"id": "iccmax", "ok": False, "mensagem": getattr(e, "texto", str(e))}
        return {"id": "iccmax", "ok": True, "mensagem": "aplicado"}

    def padrao(self) -> list[dict]:
        return self.aplicar({c["id"]: c["padrao"] for c in self.controles()}, so_mudancas=False)


def detectar(nome: str, msr=None) -> ProcessadorIntel | None:
    try:
        return ProcessadorIntel(nome, msr)
    except Exception as e:
        log.info("IntelMSR indisponível: %s", e)
        return None
