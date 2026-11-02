"""Overclock da placa AMD Radeon pela ADL (atiadlxx.dll, a biblioteca que vem com o driver Adrenalin).

Usa o Overdrive 8 (Radeon RX 5000 em diante): o driver informa cada ajuste com mínimo, máximo e padrão, e o
monitor só oferece o que a placa aceita. Mudar exige o monitor como administrador; "Voltar ao padrão" pede ao
driver os valores de fábrica. O Adrenalin pode reaplicar o perfil dele quando abre; nesse caso, aplique de novo.
Estruturas e números conferidos com o ADL SDK da AMD (licença MIT).
"""

import ctypes
import logging
import sys

log = logging.getLogger(__name__)
EH_WINDOWS = sys.platform == "win32"

ADL_OK = 0
OD8_COUNT = 77
ADL_MAX_PATH = 256

# ADLOD8FeatureControl (bits de capacidade) e ADLOD8SettingId (índices da tabela)
CAP = {"GFXCLK_LIMITS": 1 << 0, "GFXCLK_CURVE": 1 << 1, "UCLK_MAX": 1 << 2, "POWER_LIMIT": 1 << 3,
       "ACOUSTIC_LIMIT_SCLK": 1 << 4, "FAN_SPEED_MIN": 1 << 5, "TEMPERATURE_FAN": 1 << 6,
       "TEMPERATURE_SYSTEM": 1 << 7, "FAN_ZERO_RPM_CONTROL": 1 << 9, "OPTIMIZED_GPU_POWER_MODE": 1 << 16,
       "ODVOLTAGE_LIMIT": 1 << 17, "GFX_VOLTAGE_LIMIT": 1 << 21, "TDC_LIMIT": 1 << 22}
GFXCLK_FMAX, GFXCLK_FMIN, GFXCLK_FREQ1, GFXCLK_FREQ3 = 0, 1, 2, 6
UCLK_FMAX, POWER_PERCENTAGE, FAN_MIN_SPEED, FAN_ACOUSTIC_LIMIT, FAN_TARGET_TEMP, OPERATING_TEMP_MAX = 8, 9, 10, 11, 12, 13
FAN_ZERORPM_CONTROL, UCLK_FMIN, OPTIMIZED_POWER_MODE, OD_VOLTAGE = 15, 34, 36, 37
GFX_VOLTAGE_LIMIT_SETTING, TDC_PERCENTAGE = 46, 47

# (índice, capacidade exigida, id no monitor, rótulo, unidade, passo, dica)
AJUSTES = [
    (GFXCLK_FMAX, "GFXCLK_LIMITS", "clock_max", "Clock máximo do núcleo", "MHz", 5,
     "Frequência máxima do núcleo. Suba de 25 em 25 e teste."),
    (GFXCLK_FMIN, "GFXCLK_LIMITS", "clock_min", "Clock mínimo do núcleo", "MHz", 5,
     "Frequência mínima sob carga. Normalmente fica no padrão."),
    (OD_VOLTAGE, "ODVOLTAGE_LIMIT", "tensao", "Tensão (offset)", "mV", 5,
     "Valor negativo é undervolt: menos calor e consumo. Desça de 10 em 10 e teste."),
    (GFX_VOLTAGE_LIMIT_SETTING, "GFX_VOLTAGE_LIMIT", "tensao_max", "Tensão máxima", "mV", 5,
     "Limite de tensão do núcleo."),
    (UCLK_FMAX, "UCLK_MAX", "memoria", "Clock da memória", "MHz", 10,
     "Frequência da memória de vídeo. Suba de 25 em 25; erro aparece como artefatos ou queda do driver."),
    (POWER_PERCENTAGE, "POWER_LIMIT", "energia", "Limite de energia", "%", 1,
     "Mais energia sustenta clocks maiores; menos deixa a placa mais fria."),
    (TDC_PERCENTAGE, "TDC_LIMIT", "tdc", "Limite de corrente (TDC)", "%", 1, "Corrente máxima do núcleo."),
    (FAN_TARGET_TEMP, "TEMPERATURE_FAN", "temp_alvo", "Temperatura alvo da ventoinha", "°C", 1,
     "A ventoinha acelera para manter a placa perto desta temperatura."),
    (FAN_MIN_SPEED, "FAN_SPEED_MIN", "ventoinha_min", "Ventoinha mínima", "%", 1, "Velocidade mínima da ventoinha."),
    (FAN_ACOUSTIC_LIMIT, "ACOUSTIC_LIMIT_SCLK", "ventoinha_max", "Limite de ruído da ventoinha", "RPM", 10,
     "Velocidade máxima que a ventoinha pode atingir."),
    (OPERATING_TEMP_MAX, "TEMPERATURE_SYSTEM", "temp_max", "Temperatura máxima de operação", "°C", 1,
     "A placa reduz o clock acima desta temperatura."),
    (FAN_ZERORPM_CONTROL, "FAN_ZERO_RPM_CONTROL", "zero_rpm", "Ventoinha parada em baixa carga", "", 1,
     "Liga ou desliga o modo em que a ventoinha fica parada no uso leve."),
]


class AdapterInfo(ctypes.Structure):
    _fields_ = [("iSize", ctypes.c_int), ("iAdapterIndex", ctypes.c_int), ("strUDID", ctypes.c_char * ADL_MAX_PATH),
                ("iBusNumber", ctypes.c_int), ("iDeviceNumber", ctypes.c_int), ("iFunctionNumber", ctypes.c_int),
                ("iVendorID", ctypes.c_int), ("strAdapterName", ctypes.c_char * ADL_MAX_PATH),
                ("strDisplayName", ctypes.c_char * ADL_MAX_PATH), ("iPresent", ctypes.c_int), ("iExist", ctypes.c_int),
                ("strDriverPath", ctypes.c_char * ADL_MAX_PATH), ("strDriverPathExt", ctypes.c_char * ADL_MAX_PATH),
                ("strPNPString", ctypes.c_char * ADL_MAX_PATH), ("iOSDisplayIndex", ctypes.c_int)]


class SingleInit(ctypes.Structure):
    _fields_ = [("featureID", ctypes.c_int), ("minValue", ctypes.c_int), ("maxValue", ctypes.c_int),
                ("defaultValue", ctypes.c_int)]


class InitSetting(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("overdrive8Capabilities", ctypes.c_int), ("tabela", SingleInit * OD8_COUNT)]


class CurrentSetting(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("tabela", ctypes.c_int * OD8_COUNT)]


class SingleSet(ctypes.Structure):
    _fields_ = [("value", ctypes.c_int), ("requested", ctypes.c_int), ("reset", ctypes.c_int)]


class SetSetting(ctypes.Structure):
    _fields_ = [("count", ctypes.c_int), ("tabela", SingleSet * OD8_COUNT)]


class _Adl:
    """Contexto ADL2. A memória que o driver pede pelo callback fica viva enquanto o contexto existir."""

    def __init__(self):
        self.lib = None
        self.ctx = ctypes.c_void_p()
        self._blocos: list = []
        if not EH_WINDOWS:
            return
        for nome in ("atiadlxx", "atiadlxy"):
            try:
                self.lib = ctypes.WinDLL(nome)
                break
            except OSError:
                continue
        if not self.lib:
            return
        self._malloc = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_int)(self._alocar)
        try:
            if self.lib.ADL2_Main_Control_Create(self._malloc, 1, ctypes.byref(self.ctx)) != ADL_OK:
                self.lib = None
        except (AttributeError, OSError):
            self.lib = None

    def _alocar(self, tamanho: int) -> int:
        bloco = ctypes.create_string_buffer(max(1, tamanho))
        self._blocos.append(bloco)
        return ctypes.addressof(bloco)

    def f(self, nome):
        return getattr(self.lib, nome, None) if self.lib else None

    def adaptadores(self) -> list[dict]:
        n = ctypes.c_int()
        if not self.lib or self.lib.ADL2_Adapter_NumberOfAdapters_Get(self.ctx, ctypes.byref(n)) != ADL_OK or n.value <= 0:
            return []
        lista = (AdapterInfo * n.value)()
        if self.lib.ADL2_Adapter_AdapterInfo_Get(self.ctx, lista, ctypes.sizeof(lista)) != ADL_OK:
            return []
        vistos, saida = set(), []
        for a in lista:
            if a.iBusNumber < 0 or a.iVendorID not in (1002, 0x1002) or a.iBusNumber in vistos:
                continue  # ADL lista uma entrada por saída de vídeo; uma por placa basta
            ativo = ctypes.c_int()
            f = self.f("ADL2_Adapter_Active_Get")
            if f and f(self.ctx, a.iAdapterIndex, ctypes.byref(ativo)) == ADL_OK and not ativo.value:
                continue
            vistos.add(a.iBusNumber)
            saida.append({"indice": a.iAdapterIndex, "nome": a.strAdapterName.decode(errors="replace").strip(),
                          "barramento": a.iBusNumber})
        return saida

    def versao_overdrive(self, indice: int) -> int:
        sup, lig, ver = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        f = self.f("ADL2_Overdrive_Caps")
        if not f or f(self.ctx, indice, ctypes.byref(sup), ctypes.byref(lig), ctypes.byref(ver)) != ADL_OK:
            return 0
        return ver.value if sup.value else 0


class PlacaAmd:
    fabricante = "amd"

    def __init__(self, adl: _Adl, indice: int, nome: str):
        self.adl, self.indice, self.nome = adl, indice, nome
        self.handle = indice

    def _iniciais(self) -> InitSetting | None:
        ini = InitSetting(count=OD8_COUNT)
        f2 = self.adl.f("ADL2_Overdrive8_Init_SettingX2_Get")
        if f2:
            caps, n, lista = ctypes.c_int(), ctypes.c_int(OD8_COUNT), ctypes.POINTER(SingleInit)()
            if f2(self.adl.ctx, self.indice, ctypes.byref(caps), ctypes.byref(n), ctypes.byref(lista)) == ADL_OK:
                ini.count = min(n.value, OD8_COUNT)
                ini.overdrive8Capabilities = caps.value
                for i in range(ini.count):
                    ini.tabela[i] = lista[i]
                return ini
        f = self.adl.f("ADL2_Overdrive8_Init_Setting_Get")
        return ini if f and f(self.adl.ctx, self.indice, ctypes.byref(ini)) == ADL_OK else None

    def _atuais(self) -> CurrentSetting | None:
        cur = CurrentSetting(count=OD8_COUNT)
        f2 = self.adl.f("ADL2_Overdrive8_Current_SettingX2_Get")
        if f2:
            n, lista = ctypes.c_int(OD8_COUNT), ctypes.POINTER(ctypes.c_int)()
            if f2(self.adl.ctx, self.indice, ctypes.byref(n), ctypes.byref(lista)) == ADL_OK:
                cur.count = min(n.value, OD8_COUNT)
                for i in range(cur.count):
                    cur.tabela[i] = lista[i]
                return cur
        f = self.adl.f("ADL2_Overdrive8_Current_Setting_Get")
        return cur if f and f(self.adl.ctx, self.indice, ctypes.byref(cur)) == ADL_OK else None

    def controles(self) -> list[dict]:
        ini, cur = self._iniciais(), self._atuais()
        if not ini or not cur:
            return []
        caps = ini.overdrive8Capabilities
        saida = []
        for indice, cap, ident, rotulo, unidade, passo, dica in AJUSTES:
            if indice >= ini.count or not caps & CAP[cap]:
                continue
            s = ini.tabela[indice]
            if s.maxValue <= s.minValue:
                continue
            c = {"id": ident, "rotulo": rotulo, "unidade": unidade, "min": s.minValue, "max": s.maxValue, "passo": passo,
                 "padrao": s.defaultValue, "atual": cur.tabela[indice], "dica": dica, "indice": indice,
                 "sinal": ident in ("tensao", "energia", "tdc")}
            if ident == "zero_rpm":
                c["liga_desliga"] = True
            saida.append(c)
        return saida

    def leituras(self) -> list[list[str]]:
        return []

    def _definir(self, mudancas: dict[int, int], resetar: bool = False) -> int:
        ini, cur = self._iniciais(), self._atuais()
        if not ini or not cur:
            return -1
        caps = ini.overdrive8Capabilities
        f = self.adl.f("ADL2_Overdrive8_Setting_Set")
        if not f:
            return -1
        if caps & CAP["OPTIMIZED_GPU_POWER_MODE"] and not resetar:
            # Placas com modos prontos (RX 6000/7000): passa para o modo manual antes de mexer.
            modo = SetSetting(count=OD8_COUNT)
            modo.tabela[OPTIMIZED_POWER_MODE].requested = 1
            modo.tabela[OPTIMIZED_POWER_MODE].value = 3
            f(self.adl.ctx, self.indice, ctypes.byref(modo), ctypes.byref(cur))
        s = SetSetting(count=OD8_COUNT)
        pares = {GFXCLK_FMAX: (GFXCLK_FMAX, GFXCLK_FMIN), GFXCLK_FMIN: (GFXCLK_FMAX, GFXCLK_FMIN),
                 UCLK_FMAX: (UCLK_FMAX, UCLK_FMIN), UCLK_FMIN: (UCLK_FMAX, UCLK_FMIN)}
        curva = caps & CAP["GFXCLK_CURVE"]  # placas antigas (RX 5000) usam a curva de 3 pontos
        for indice, valor in mudancas.items():
            for p in pares.get(indice, (indice,)):  # o driver quer o par mínimo/máximo junto
                s.tabela[p].requested = 1
                s.tabela[p].value = cur.tabela[p]
            s.tabela[indice].requested = 1
            if resetar:
                s.tabela[indice].reset = 1
                s.tabela[indice].value = ini.tabela[indice].defaultValue
            else:
                s.tabela[indice].value = int(valor)
                if curva and indice == GFXCLK_FMAX:
                    s.tabela[GFXCLK_FREQ3].requested, s.tabela[GFXCLK_FREQ3].value = 1, int(valor)
                elif curva and indice == GFXCLK_FMIN:
                    s.tabela[GFXCLK_FREQ1].requested, s.tabela[GFXCLK_FREQ1].value = 1, int(valor)
        return f(self.adl.ctx, self.indice, ctypes.byref(s), ctypes.byref(cur))

    def aplicar(self, valores: dict, so_mudancas: bool = True) -> list[dict]:
        atuais = {c["id"]: c for c in self.controles()}
        mudancas, saida = {}, []
        for ident, valor in valores.items():
            c = atuais.get(ident)
            if c is None:
                saida.append({"id": ident, "ok": False, "mensagem": "não disponível nesta placa"})
                continue
            valor = int(round(float(valor)))
            if so_mudancas and valor == c["atual"]:
                continue
            if not c["min"] <= valor <= c["max"]:
                saida.append({"id": ident, "ok": False, "mensagem": f"fora da faixa ({c['min']} a {c['max']})"})
                continue
            mudancas[c["indice"]] = valor
        if mudancas:
            r = self._definir(mudancas)
            erro = None if r >= ADL_OK else explicar(r)
            ids = {c["indice"]: c["id"] for c in atuais.values()}
            saida += [{"id": ids[i], "ok": erro is None, "mensagem": erro or "aplicado"} for i in mudancas]
        return saida

    def padrao(self) -> list[dict]:
        controles = self.controles()
        if not controles:
            return []
        r = self._definir({c["indice"]: c["padrao"] for c in controles}, resetar=True)
        erro = None if r >= ADL_OK else explicar(r)
        return [{"id": c["id"], "ok": erro is None, "mensagem": erro or "padrão"} for c in controles]


def explicar(codigo: int) -> str:
    return {-23: "precisa do monitor aberto como administrador", -8: "o driver não deixa mudar isso nesta placa",
            -3: "valor recusado pelo driver", -20: "o driver aplicou só parte dos ajustes",
            -26: "o driver está ocupado; tente de novo", -27: "a placa está em uso por outro programa de ajuste"}.get(
        codigo, f"o driver recusou (código {codigo})")


_adl: _Adl | None = None


def placas() -> list[PlacaAmd]:
    global _adl
    if _adl is None:
        _adl = _Adl()
    if not _adl.lib:
        return []
    return [PlacaAmd(_adl, a["indice"], a["nome"]) for a in _adl.adaptadores() if _adl.versao_overdrive(a["indice"]) >= 8]
