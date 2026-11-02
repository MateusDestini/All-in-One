"""Overclock e undervolt dos Ryzen 7000 e 9000 (Zen 4 e Zen 5 de desktop), como o Ryzen Master.

Fala com o SMU (o microcontrolador que gerencia energia e clock dentro do Ryzen) pelo módulo RyzenSMU do PawnIO.
Os comandos e endereços das caixas de correio do SMU são os documentados pela comunidade (ZenStates, ryzen_smu)
para Raphael e Granite Ridge. Tudo o que é mudado aqui é temporário: o SMU volta ao que a BIOS manda quando o
PC reinicia. Por isso um valor instável nunca "prende" o PC: basta reiniciar.

Ajustes:
- Limites do PBO: PPT (energia do soquete, W), TDC (corrente sustentada, A), EDC (corrente de pico, A).
- Limite de temperatura (Tctl).
- Boost override: até +200 MHz acima do boost de fábrica (exige PBO ligado na BIOS).
- Curve Optimizer: de -50 a +30 por núcleo (negativo = menos tensão para o mesmo clock = mais boost e menos calor).
"""

import logging
import re
import struct

import psutil

from . import config, pawnio

log = logging.getLogger(__name__)

# Caixas de correio (endereços SMN) e comandos do Zen 4 / Zen 5 de desktop
RSMU = (0x03B10524, 0x03B10570, 0x03B10A40)  # comando, resposta, argumentos
MP1 = (0x03B10530, 0x03B1057C, 0x03B109C4)
CMD = {
    "ppt": (0x56, 0x3E), "tdc": (0x57, 0x3C), "edc": (0x58, 0x3D), "temp": (0x59, 0x3F),  # (RSMU, MP1)
    "co_nucleo": (0x06, 0x35), "co_todos": (0x07, 0x36),
}
CMD_LER_CO = 0xD5
CMD_LER_BOOST, CMD_BOOST = 0x6E, 0x70
CMD_FUSIVEL_PPT, CMD_FUSIVEL_TDC = 0xDC, 0xDB
CMD_VERSAO_SMU = 0x02

RESPOSTAS = {0x01: None, 0xFC: "o processador está ocupado", 0xFD: "o processador recusou (confira se o PBO está ligado na BIOS)",
             0xFE: "comando não existe neste processador", 0xFF: "o processador recusou o comando"}

# família 0x19 modelo 0x61 = Raphael (Ryzen 7000); 0x1A/0x44 = Granite Ridge (Ryzen 9000)
SUPORTADOS = {(0x19, 0x61): "Raphael", (0x1A, 0x44): "GraniteRidge"}
FUSIVEL_NUCLEOS = {"Raphael": 0x30081CD0, "GraniteRidge": 0x304A03DC}
PASSO_FUSIVEL_CCD = 1 << 25

# Padrões da AMD por TDP: (PPT W, TDC A, EDC A)
PADRAO_TDP = {65: (88, 75, 150), 105: (142, 110, 170), 120: (162, 120, 180), 170: (230, 160, 225)}
TDP_MODELOS = {
    "9950X3D": 170, "9900X3D": 120, "9800X3D": 120, "9950X": 170, "9900X": 120, "9700X": 65, "9600X": 65, "9600": 65,
    "7950X3D": 120, "7900X3D": 120, "7800X3D": 120, "7600X3D": 65, "7950X": 170, "7900X": 170, "7700X": 105,
    "7600X": 105, "7900": 65, "7700": 65, "7600": 65, "7500F": 65, "7400F": 65,
}
CO_MIN, CO_MAX = -50, 30
BOOST_MAX = 200


def _tdp(nome: str) -> int:
    for modelo in sorted(TDP_MODELOS, key=len, reverse=True):
        if re.search(rf"\b{modelo}\b", nome, re.I):
            return TDP_MODELOS[modelo]
    return 105


def _arg_co(margem: int) -> int:
    return ((0x100000 + margem) if margem < 0 else margem) & 0xFFFF


def _co_lido(v: int) -> int | None:
    v &= 0xFFFF
    if v & 0x8000:
        v -= 0x10000
    return v if -60 <= v <= 60 else None


class ErroSmu(Exception):
    pass


class Smu:
    """Transporte real: PawnIO. Os testes trocam por um falso com a mesma interface."""

    def __init__(self):
        self.mod = pawnio.modulo("RyzenSMU")
        self._smn = None

    def rsmu(self, cmd: int, args: list[int]) -> list[int]:
        a = (list(args) + [0] * 6)[:6]
        with pawnio.trava_pci():
            return self.mod.executar("ioctl_send_smu_command", [cmd] + a, 6)

    def mp1(self, cmd: int, args: list[int]) -> list[int]:
        """Caixa MP1, feita à mão pelos registradores (o módulo só expõe a RSMU pronta)."""
        c, r, a = MP1
        args = (list(args) + [0] * 6)[:6]
        ler = lambda end: self.mod.executar("ioctl_read_smu_register", [end], 1)[0] & 0xFFFFFFFF  # noqa: E731
        escrever = lambda end, v: self.mod.executar("ioctl_write_smu_register", [end, v & 0xFFFFFFFF], 0)  # noqa: E731
        with pawnio.trava_pci():
            for _ in range(8000):
                if ler(r):
                    break
            else:
                raise ErroSmu(RESPOSTAS[0xFC])
            escrever(r, 0)
            for i, v in enumerate(args):
                escrever(a + 4 * i, v)
            escrever(c, cmd)
            for _ in range(8000):
                st = ler(r)
                if st:
                    break
            else:
                raise ErroSmu("o processador não respondeu")
            if st != 0x01:
                raise ErroSmu(RESPOSTAS.get(st, f"resposta 0x{st:02X}"))
            return [ler(a + 4 * i) for i in range(6)]

    def smn(self, endereco: int) -> int:
        if self._smn is None:
            self._smn = pawnio.modulo("AMDFamily17")
        with pawnio.trava_pci():
            return self._smn.executar("ioctl_read_smn", [endereco], 1)[0] & 0xFFFFFFFF

    def tabela(self, n: int = 64) -> tuple[int, list[float]]:
        with pawnio.trava_pci():
            versao, _base = self.mod.executar("ioctl_resolve_pm_table", [], 2)
            self.mod.executar("ioctl_update_pm_table", [], 0)
        dados = self.mod.executar("ioctl_read_pm_table", [], n)
        floats = []
        for q in dados:
            floats += list(struct.unpack("<ff", struct.pack("<Q", q & 0xFFFFFFFFFFFFFFFF)))
        return versao, floats


def _erro_pawnio(e: Exception) -> str:
    if isinstance(e, pawnio.ErroPawnIO):
        if e.codigo == 0xD00000E5:
            return RESPOSTAS[0xFD]
        if e.codigo == 0xD0000003:
            return RESPOSTAS[0xFE]
        return e.texto
    return str(e)


class ProcessadorRyzen:
    fabricante = "cpu"
    marca = "amd"

    def __init__(self, nome: str, codinome: str, smu=None):
        self.nome = nome
        self.codinome = codinome
        self.smu = smu or Smu()
        self.nucleos = psutil.cpu_count(logical=False) or 1
        self._mascaras: list[int] | None = None
        self._mascaras_lidas = False
        tdp = _tdp(nome)
        fusivel = self._fusiveis()
        ppt, tdc, edc = PADRAO_TDP.get(tdp, PADRAO_TDP[105])
        self.padroes = {"ppt": fusivel.get("ppt") or ppt, "tdc": fusivel.get("tdc") or tdc, "edc": edc, "temp": 95,
                        "boost": 0}
        self.tdp = tdp

    # ------------------------------------------------------------ chamadas
    def _comando(self, chave: str, args: list[int]) -> list[int]:
        """Tenta a RSMU e, se o comando não existir lá, a MP1 (como o ZenStates)."""
        rsmu, mp1 = CMD[chave]
        primeiro, segundo = ((self.smu.mp1, mp1), (self.smu.rsmu, rsmu)) if chave.startswith("co_") else \
            ((self.smu.rsmu, rsmu), (self.smu.mp1, mp1))
        try:
            return primeiro[0](primeiro[1], args)
        except Exception as e:
            log.info("comando %s pela primeira caixa falhou (%s); tentando a outra", chave, e)
            return segundo[0](segundo[1], args)

    def _fusiveis(self) -> dict:
        saida = {}
        for chave, cmd, faixa in (("ppt", CMD_FUSIVEL_PPT, (35, 400)), ("tdc", CMD_FUSIVEL_TDC, (30, 300))):
            try:
                v = self.smu.rsmu(cmd, [])[0]
            except Exception:
                continue
            for escala in (1, 1000):
                if faixa[0] <= v / escala <= faixa[1]:
                    saida[chave] = round(v / escala)
                    break
        return saida

    # ------------------------------------------------------------ topologia (para o Curve Optimizer por núcleo)
    def mascaras(self) -> list[int] | None:
        """Máscara do SMU de cada núcleo, na ordem do Windows. None = só dá para ajustar todos juntos."""
        if self._mascaras_lidas:
            return self._mascaras
        self._mascaras_lidas = True
        ccds = 1 if self.nucleos <= 8 else 2
        base = FUSIVEL_NUCLEOS.get(self.codinome)
        mascaras = []
        try:
            for ccd in range(ccds):
                desligados = self.smu.smn(base + PASSO_FUSIVEL_CCD * ccd) & 0xFF
                for slot in range(8):
                    if not desligados & (1 << slot):
                        mascaras.append((ccd << 28) | (slot << 20))
        except Exception as e:
            log.info("não li o mapa de núcleos (%s); Curve Optimizer só para todos", e)
            return None
        if len(mascaras) != self.nucleos:
            log.info("mapa de núcleos com %d núcleos, Windows diz %d; Curve Optimizer só para todos", len(mascaras),
                     self.nucleos)
            return None
        self._mascaras = mascaras
        return mascaras

    def co_atual(self) -> list[int | None]:
        m = self.mascaras()
        if not m:
            return []
        saida = []
        for mascara in m:
            try:
                saida.append(_co_lido(self.smu.rsmu(CMD_LER_CO, [mascara])[0]))
            except Exception:
                saida.append(None)
        return saida

    def _boost_atual(self) -> int | None:
        try:
            v = self.smu.rsmu(CMD_LER_BOOST, [])[0] & 0xFFFFF
        except Exception:
            return None
        return v if 2000 <= v <= 7000 else None

    def _boost_fabrica(self) -> int | None:
        orig = _memoria(self.nome).get("boost_fabrica")
        if orig:
            return orig
        atual = self._boost_atual()
        if atual and not _aplicado(self.nome).get("boost"):
            _guardar(self.nome, "boost_fabrica", atual)
        return atual

    def _tabela(self) -> dict:
        """Limites e uso atuais pela tabela de energia do SMU, quando os números fazem sentido."""
        try:
            versao, f = self.smu.tabela(8)
        except Exception:
            return {}
        saida = {"versao_tabela": versao}
        if len(f) >= 2 and 20 <= f[0] <= 1000 and 0 <= f[1] <= f[0] * 1.6:
            saida["ppt"], saida["ppt_uso"] = round(f[0]), f[1]
        return saida

    # ------------------------------------------------------------ painel
    def controles(self) -> list[dict]:
        aplicado = _aplicado(self.nome)
        tab = self._tabela()
        p = self.padroes
        c = [
            {"id": "ppt", "rotulo": "PPT · limite de energia", "unidade": "W", "min": 45, "max": 300, "passo": 1,
             "padrao": p["ppt"], "atual": tab.get("ppt") or aplicado.get("ppt", p["ppt"]), "grupo": "pbo",
             "dica": "Energia máxima que o processador pode puxar do soquete. Menos = mais frio e silencioso."},
            {"id": "tdc", "rotulo": "TDC · corrente sustentada", "unidade": "A", "min": 40, "max": 250, "passo": 1,
             "padrao": p["tdc"], "atual": aplicado.get("tdc", p["tdc"]), "grupo": "pbo",
             "dica": "Corrente que os reguladores da placa-mãe sustentam por tempo longo."},
            {"id": "edc", "rotulo": "EDC · corrente de pico", "unidade": "A", "min": 60, "max": 300, "passo": 1,
             "padrao": p["edc"], "atual": aplicado.get("edc", p["edc"]), "grupo": "pbo",
             "dica": "Corrente em picos curtos. Subir demais pode até baixar o boost em carga leve."},
            {"id": "temp", "rotulo": "Limite de temperatura", "unidade": "°C", "min": 60, "max": 95, "passo": 1,
             "padrao": 95, "atual": aplicado.get("temp", 95), "grupo": "pbo",
             "dica": "O processador reduz o clock para não passar disso. 85 °C deixa mais silencioso com pouca perda."},
        ]
        fab = self._boost_fabrica()
        if fab:
            atual = self._boost_atual()
            c.append({"id": "boost", "rotulo": "Boost override", "unidade": "MHz", "min": 0, "max": BOOST_MAX, "passo": 25,
                      "padrao": 0, "atual": max(0, (atual or fab) - fab), "sinal": True, "grupo": "pbo",
                      "dica": f"Soma até +200 MHz ao boost de fábrica ({fab} MHz). Só funciona com o PBO ligado na BIOS."})
        m = self.mascaras()
        if m:
            lidos = self.co_atual()
            for i in range(len(m)):
                atual = lidos[i] if i < len(lidos) and lidos[i] is not None else aplicado.get(f"co_{i}", 0)
                c.append({"id": f"co_{i}", "rotulo": f"Núcleo {i}", "unidade": "", "min": CO_MIN, "max": CO_MAX, "passo": 1,
                          "padrao": 0, "atual": atual, "sinal": True, "grupo": "co"})
        else:
            c.append({"id": "co", "rotulo": "Curve Optimizer · todos os núcleos", "unidade": "", "min": CO_MIN,
                      "max": CO_MAX, "passo": 1, "padrao": 0, "atual": aplicado.get("co", 0), "sinal": True, "grupo": "co",
                      "dica": "Negativo tira tensão de todos os núcleos: mais boost e menos calor, até ficar instável."})
        return c

    def leituras(self) -> list[list[str]]:
        saida = []
        tab = self._tabela()
        if tab.get("ppt_uso") is not None:
            saida.append(["PPT em uso", f"{tab['ppt_uso']:.0f} de {tab['ppt']} W"])
        b = self._boost_atual()
        if b:
            saida.append(["Boost máximo agora", f"{b} MHz"])
        v = tentar_versao_smu(self.smu)
        if v:
            saida.append(["Firmware do SMU", v])
        return saida

    def info(self) -> dict:
        m = self.mascaras()
        return {"marca": "amd", "codinome": self.codinome, "nucleos": self.nucleos, "por_nucleo": bool(m),
                "co_min": CO_MIN, "co_max": CO_MAX, "tdp": self.tdp,
                "presets": [{"nome": "Eco 65 W", "valores": dict(zip(("ppt", "tdc", "edc"), PADRAO_TDP[65]))},
                            {"nome": "Eco 105 W", "valores": dict(zip(("ppt", "tdc", "edc"), PADRAO_TDP[105]))},
                            {"nome": "Padrão da AMD", "valores": {k: self.padroes[k] for k in ("ppt", "tdc", "edc")}}]}

    # ------------------------------------------------------------ escrita
    def aplicar(self, valores: dict, so_mudancas: bool = True) -> list[dict]:
        ctrl = {c["id"]: c for c in self.controles()}
        saida, mudou = [], {}
        valores = {k: int(round(float(v))) for k, v in valores.items()}
        # Curve Optimizer: todos iguais vira um comando só
        cos = {k: v for k, v in valores.items() if re.fullmatch(r"co_\d+", k)}
        if cos and len(set(cos.values())) == 1 and len(cos) == len([k for k in ctrl if re.fullmatch(r"co_\d+", k)]):
            v = next(iter(cos.values()))
            if not so_mudancas or any(ctrl[k]["atual"] != v for k in cos):
                r = self._um("co", v, ctrl.get("co_0"), todos=True)
                for k in cos:
                    saida.append(dict(r, id=k))
                    if r["ok"]:
                        mudou[k] = v
            valores = {k: v for k, v in valores.items() if k not in cos}
        for ident, valor in valores.items():
            c = ctrl.get(ident)
            if c is None:
                saida.append({"id": ident, "ok": False, "mensagem": "não disponível neste processador"})
                continue
            if so_mudancas and valor == c["atual"]:
                continue
            if not c["min"] <= valor <= c["max"]:
                saida.append({"id": ident, "ok": False, "mensagem": f"fora da faixa ({c['min']} a {c['max']})"})
                continue
            r = self._um(ident, valor, c)
            saida.append(r)
            if r["ok"]:
                mudou[ident] = valor
        if mudou:
            _registrar_aplicado(self.nome, mudou)
        return saida

    def _um(self, ident: str, valor: int, c: dict | None, todos: bool = False) -> dict:
        try:
            if ident in ("ppt", "tdc", "edc"):
                self._comando(ident, [valor * 1000])
            elif ident == "temp":
                self._comando("temp", [valor])
            elif ident == "boost":
                fab = self._boost_fabrica()
                if not fab:
                    return {"id": ident, "ok": False, "mensagem": "não consegui ler o boost de fábrica"}
                self.smu.rsmu(CMD_BOOST, [(fab + valor) & 0xFFFFF])
                lido = self._boost_atual()
                if lido is not None and abs(lido - (fab + valor)) > 5:
                    return {"id": ident, "ok": False, "mensagem": "o processador não aceitou (ligue o PBO na BIOS)"}
            elif ident == "co" or todos:
                self._comando("co_todos", [_arg_co(valor)])
            elif ident.startswith("co_"):
                m = self.mascaras()
                i = int(ident[3:])
                self._comando("co_nucleo", [(m[i] & 0xFFF00000) | _arg_co(valor)])
                lido = _co_lido(self.smu.rsmu(CMD_LER_CO, [m[i]])[0]) if m else None
                if lido is not None and lido != valor:
                    return {"id": ident, "ok": False, "mensagem": f"o processador ficou em {lido} (ligue o PBO na BIOS)"}
            else:
                return {"id": ident, "ok": False, "mensagem": "ajuste desconhecido"}
        except Exception as e:
            return {"id": ident, "ok": False, "mensagem": _erro_pawnio(e)}
        return {"id": ident, "ok": True, "mensagem": "aplicado"}

    def padrao(self) -> list[dict]:
        valores = {c["id"]: c["padrao"] for c in self.controles()}
        r = self.aplicar(valores, so_mudancas=False)
        _limpar_aplicado(self.nome)
        return r


# ------------------------------------------------------------------ o que foi aplicado nesta sessão do Windows
def _boot() -> float:
    return psutil.boot_time()


def _aplicado(nome: str) -> dict:
    r = (config.preferencias().get("cpu_aplicado") or {}).get(nome)
    if r and abs(r.get("boot", 0) - _boot()) < 5:
        return dict(r.get("valores") or {})
    return {}


def _registrar_aplicado(nome: str, valores: dict) -> None:
    tudo = config.preferencias().get("cpu_aplicado") or {}
    atual = _aplicado(nome)
    atual.update(valores)
    tudo[nome] = {"boot": _boot(), "valores": atual}
    config.definir_preferencia("cpu_aplicado", tudo)


def _limpar_aplicado(nome: str) -> None:
    tudo = config.preferencias().get("cpu_aplicado") or {}
    tudo.pop(nome, None)
    config.definir_preferencia("cpu_aplicado", tudo)


def _memoria(nome: str) -> dict:
    return dict((config.preferencias().get("cpu_original") or {}).get(nome) or {})


def _guardar(nome: str, chave: str, valor) -> None:
    tudo = config.preferencias().get("cpu_original") or {}
    tudo.setdefault(nome, {})[chave] = valor
    config.definir_preferencia("cpu_original", tudo)


def detectar(nome: str, familia: int, modelo: int, smu=None) -> ProcessadorRyzen | None:
    codinome = SUPORTADOS.get((familia, modelo))
    if not codinome:
        return None
    return ProcessadorRyzen(nome, codinome, smu)


def tentar_versao_smu(smu) -> str | None:
    try:
        v = smu.rsmu(CMD_VERSAO_SMU, [1])[0]
        return f"{(v >> 16) & 0xFF}.{(v >> 8) & 0xFF}.{v & 0xFF}"
    except Exception:
        return None
