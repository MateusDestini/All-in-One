"""Overclock da placa NVIDIA pela NVML, a biblioteca oficial que vem com o driver.

Tudo fica dentro dos limites que o próprio driver informa (ele recusa valores fora da faixa) e volta ao padrão
ao reiniciar o PC ou pelo botão "Voltar ao padrão". Mudar qualquer coisa exige o monitor como administrador.
Cada controle só aparece se o driver disser que a placa aceita: placas e drivers antigos expõem menos coisas.
"""

import ctypes
import logging

import psutil

from . import gpu

log = logging.getLogger(__name__)

NVML_SUCCESS, NVML_ERROR_INVALID_ARGUMENT, NVML_ERROR_NOT_SUPPORTED, NVML_ERROR_NO_PERMISSION = 0, 2, 3, 4
NVML_ERROR_FUNCTION_NOT_FOUND = 13
CLOCK_GRAFICO, CLOCK_MEMORIA = 0, 2
LIMIAR_ACUSTICO_MIN, LIMIAR_ACUSTICO_ATUAL, LIMIAR_ACUSTICO_MAX = 4, 5, 6
POLITICA_AUTO = 0


class _OffsetClock(ctypes.Structure):  # nvmlClockOffset_v1_t
    _fields_ = [("version", ctypes.c_uint), ("type", ctypes.c_int), ("pstate", ctypes.c_int),
                ("clockOffsetMHz", ctypes.c_int), ("minClockOffsetMHz", ctypes.c_int),
                ("maxClockOffsetMHz", ctypes.c_int)]


VERSAO_OFFSET = ctypes.sizeof(_OffsetClock) | (1 << 24)


def explicar(codigo: int | None) -> str | None:
    """Texto para o código de retorno da NVML (None = deu certo)."""
    if codigo in (None, NVML_SUCCESS):
        return None
    return {NVML_ERROR_NO_PERMISSION: "precisa do monitor aberto como administrador",
            NVML_ERROR_NOT_SUPPORTED: "o driver não deixa mudar isso nesta placa",
            NVML_ERROR_INVALID_ARGUMENT: "valor fora da faixa aceita pela placa",
            NVML_ERROR_FUNCTION_NOT_FOUND: "o driver instalado é antigo demais para isso"}.get(
        codigo, f"o driver recusou (código {codigo})")


class PlacaNvidia:
    fabricante = "nvidia"

    def __init__(self, handle: int, nome: str):
        self.h = ctypes.c_void_p(handle)
        self.handle = handle
        self.nome = nome
        self.n = gpu.nvml()

    # ------------------------------------------------------------ chamadas cruas
    def _f(self, nome):
        return getattr(self.n.lib, nome, None) if self.n.lib else None

    def _chamar(self, nome, *args) -> int:
        f = self._f(nome)
        if f is None:
            return NVML_ERROR_FUNCTION_NOT_FOUND
        try:
            return int(f(*args))
        except OSError:
            return NVML_ERROR_FUNCTION_NOT_FOUND

    def _uint(self, nome, *extra):
        v = ctypes.c_uint()
        return v.value if self._chamar(nome, self.h, *extra, ctypes.byref(v)) == 0 else None

    def _offset(self, tipo: int) -> dict | None:
        """Offset atual e faixa (API nova, driver 555+; senão a antiga de VF offset)."""
        o = _OffsetClock(version=VERSAO_OFFSET, type=tipo, pstate=0)
        if self._chamar("nvmlDeviceGetClockOffsets", self.h, ctypes.byref(o)) == 0:
            return {"atual": o.clockOffsetMHz, "min": o.minClockOffsetMHz, "max": o.maxClockOffsetMHz, "nova": True}
        prefixo = "Gpc" if tipo == CLOCK_GRAFICO else "Mem"
        atual, mn, mx = ctypes.c_int(), ctypes.c_int(), ctypes.c_int()
        if self._chamar(f"nvmlDeviceGet{prefixo}ClkVfOffset", self.h, ctypes.byref(atual)) == 0 and \
                self._chamar(f"nvmlDeviceGet{prefixo}ClkMinMaxVfOffset", self.h, ctypes.byref(mn), ctypes.byref(mx)) == 0:
            return {"atual": atual.value, "min": mn.value, "max": mx.value, "nova": False}
        return None

    def _definir_offset(self, tipo: int, mhz: int) -> int:
        o = _OffsetClock(version=VERSAO_OFFSET, type=tipo, pstate=0, clockOffsetMHz=int(mhz))
        r = self._chamar("nvmlDeviceSetClockOffsets", self.h, ctypes.byref(o))
        if r == NVML_ERROR_FUNCTION_NOT_FOUND:
            prefixo = "Gpc" if tipo == CLOCK_GRAFICO else "Mem"
            r = self._chamar(f"nvmlDeviceSet{prefixo}ClkVfOffset", self.h, ctypes.c_int(int(mhz)))
        return r

    def _ventoinhas(self) -> int:
        return self._uint("nvmlDeviceGetNumFans") or 0

    # ------------------------------------------------------------ leitura
    def controles(self) -> list[dict]:
        """Controles que esta placa aceita, com faixa, padrão e valor atual."""
        c = []
        mn, mx = ctypes.c_uint(), ctypes.c_uint()
        if self._chamar("nvmlDeviceGetPowerManagementLimitConstraints", self.h, ctypes.byref(mn), ctypes.byref(mx)) == 0 \
                and mx.value > mn.value:
            padrao = self._uint("nvmlDeviceGetPowerManagementDefaultLimit")
            atual = self._uint("nvmlDeviceGetPowerManagementLimit") or self._uint("nvmlDeviceGetEnforcedPowerLimit")
            c.append({"id": "energia", "rotulo": "Limite de energia", "unidade": "W", "min": round(mn.value / 1000),
                      "max": round(mx.value / 1000), "passo": 1, "padrao": round((padrao or mx.value) / 1000),
                      "atual": round((atual or padrao or 0) / 1000),
                      "dica": "Mais energia deixa a placa sustentar clocks maiores; menos energia deixa mais fria e silenciosa."})
        for ident, tipo, rotulo, dica in (
                ("nucleo", CLOCK_GRAFICO, "Clock do núcleo (offset)",
                 "Soma MHz em toda a curva do núcleo. Suba de 15 em 15 e teste; +100 a +200 é comum."),
                ("memoria", CLOCK_MEMORIA, "Clock da memória (offset)",
                 "Soma MHz na memória de vídeo. Suba de 100 em 100; erro de memória aparece como artefatos ou queda do driver.")):
            o = self._offset(tipo)
            if o and o["max"] > o["min"]:
                c.append({"id": ident, "rotulo": rotulo, "unidade": "MHz", "min": o["min"], "max": o["max"],
                          "passo": 5 if ident == "nucleo" else 25, "padrao": 0, "atual": o["atual"], "dica": dica,
                          "sinal": True})
        t_min = self._uint("nvmlDeviceGetTemperatureThreshold", ctypes.c_int(LIMIAR_ACUSTICO_MIN))
        t_max = self._uint("nvmlDeviceGetTemperatureThreshold", ctypes.c_int(LIMIAR_ACUSTICO_MAX))
        t_atual = self._uint("nvmlDeviceGetTemperatureThreshold", ctypes.c_int(LIMIAR_ACUSTICO_ATUAL))
        if t_min and t_max and t_max > t_min and t_atual:
            original = _original(self.nome, "temperatura")  # a NVML não informa o padrão de fábrica
            c.append({"id": "temperatura", "rotulo": "Alvo de temperatura", "unidade": "°C", "min": t_min, "max": t_max,
                      "passo": 1, "padrao": original if original is not None else t_atual, "atual": t_atual,
                      "dica": "A placa reduz o clock para não passar desta temperatura."})
        n = self._ventoinhas()
        if n:
            vmin, vmax = ctypes.c_uint(), ctypes.c_uint()
            if self._chamar("nvmlDeviceGetMinMaxFanSpeed", self.h, ctypes.byref(vmin), ctypes.byref(vmax)) == 0 \
                    and vmax.value > vmin.value:
                politica = ctypes.c_int()
                manual = self._chamar("nvmlDeviceGetFanControlPolicy_v2", self.h, ctypes.c_uint(0),
                                      ctypes.byref(politica)) == 0 and politica.value != POLITICA_AUTO
                v = ctypes.c_uint()
                atual = v.value if manual and self._chamar("nvmlDeviceGetFanSpeed_v2", self.h, ctypes.c_uint(0),
                                                            ctypes.byref(v)) == 0 else 0
                c.append({"id": "ventoinha", "rotulo": "Ventoinha", "unidade": "%", "min": vmin.value, "max": vmax.value,
                          "passo": 1, "padrao": 0, "atual": atual, "auto": True,
                          "dica": "Automático deixa o driver decidir. Velocidade fixa vale para todas as ventoinhas."})
        clock_max = self.n.clock_max(self.handle, CLOCK_GRAFICO)
        if clock_max and self._f("nvmlDeviceSetGpuLockedClocks"):
            c.append({"id": "teto", "rotulo": "Teto de clock do núcleo", "unidade": "MHz", "min": 600,
                      "max": clock_max, "passo": 15, "padrao": 0, "atual": _teto_registrado(self.nome), "auto": True,
                      "rotulo_auto": "Sem teto",
                      "atalhos": [["Leve", clock_max - 300], ["Equilibrado", clock_max - 500],
                                  ["Econômico", clock_max - 800]],
                      "dica": "Trava o clock máximo: menos calor e consumo com pouca perda de FPS (bom para undervolt simples)."})
        return c

    def leituras(self) -> list[list[str]]:
        return gpu.ao_vivo(self.handle)

    # ------------------------------------------------------------ escrita
    def aplicar(self, valores: dict, so_mudancas: bool = True) -> list[dict]:
        """Aplica {id: valor}. Devolve [{id, ok, mensagem}]."""
        atuais = {c["id"]: c for c in self.controles()}
        saida = []
        for ident, valor in valores.items():
            c = atuais.get(ident)
            if c is None:
                saida.append({"id": ident, "ok": False, "mensagem": "não disponível nesta placa"})
                continue
            valor = int(round(float(valor)))
            if so_mudancas and valor == c["atual"]:
                continue
            if not (c.get("auto") and valor == 0) and not c["min"] <= valor <= c["max"]:
                saida.append({"id": ident, "ok": False, "mensagem": f"fora da faixa ({c['min']} a {c['max']})"})
                continue
            if ident == "temperatura" and _original(self.nome, "temperatura") is None:
                _guardar_original(self.nome, "temperatura", c["atual"])
            r = self._aplicar_um(ident, valor)
            erro = explicar(r)
            if ident == "teto" and not erro:
                _registrar_teto(self.nome, valor)
            saida.append({"id": ident, "ok": erro is None, "mensagem": erro or "aplicado"})
        return saida

    def _aplicar_um(self, ident: str, valor: int) -> int:
        if ident == "energia":
            return self._chamar("nvmlDeviceSetPowerManagementLimit", self.h, ctypes.c_uint(valor * 1000))
        if ident == "nucleo":
            return self._definir_offset(CLOCK_GRAFICO, valor)
        if ident == "memoria":
            return self._definir_offset(CLOCK_MEMORIA, valor)
        if ident == "temperatura":
            t = ctypes.c_int(valor)
            return self._chamar("nvmlDeviceSetTemperatureThreshold", self.h, ctypes.c_int(LIMIAR_ACUSTICO_ATUAL),
                                ctypes.byref(t))
        if ident == "ventoinha":
            r = 0
            for i in range(self._ventoinhas()):
                if valor == 0:
                    r = r or self._chamar("nvmlDeviceSetDefaultFanSpeed_v2", self.h, ctypes.c_uint(i))
                else:
                    r = r or self._chamar("nvmlDeviceSetFanSpeed_v2", self.h, ctypes.c_uint(i), ctypes.c_uint(valor))
            return r
        if ident == "teto":
            if valor == 0:
                return self._chamar("nvmlDeviceResetGpuLockedClocks", self.h)
            return self._chamar("nvmlDeviceSetGpuLockedClocks", self.h, ctypes.c_uint(210), ctypes.c_uint(valor))
        return NVML_ERROR_NOT_SUPPORTED

    def padrao(self) -> list[dict]:
        """Volta tudo ao padrão de fábrica."""
        valores = {c["id"]: c["padrao"] for c in self.controles()}
        return self.aplicar(valores, so_mudancas=False)


# A NVML não informa o teto de clock aplicado; o monitor guarda o último valor enquanto o PC não reinicia.
def _registrar_teto(handle: str, valor: int) -> None:
    from . import config
    pref = config.preferencias().get("oc_teto") or {}
    pref[str(handle)] = {"boot": psutil.boot_time(), "valor": valor}
    config.definir_preferencia("oc_teto", pref)


def _original(handle: str, chave: str):
    from . import config
    return ((config.preferencias().get("oc_original") or {}).get(str(handle)) or {}).get(chave)


def _guardar_original(handle: str, chave: str, valor) -> None:
    from . import config
    tudo = config.preferencias().get("oc_original") or {}
    tudo.setdefault(str(handle), {})[chave] = valor
    config.definir_preferencia("oc_original", tudo)


def _teto_registrado(handle: str) -> int:
    from . import config
    r = (config.preferencias().get("oc_teto") or {}).get(str(handle))
    if r and abs(r.get("boot", 0) - psutil.boot_time()) < 5:
        return int(r.get("valor") or 0)
    return 0


def placas() -> list[PlacaNvidia]:
    n = gpu.nvml()
    if not n.disponivel:
        return []
    saida = []
    for d in n.dispositivos():
        nome = n._texto("nvmlDeviceGetName", ctypes.c_void_p(d["handle"])) or "NVIDIA"
        saida.append(PlacaNvidia(d["handle"], nome))
    return saida
