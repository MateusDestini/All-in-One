"""Placa de vídeo no estilo da aba "Graphics Card" do GPU-Z.

Junta quatro fontes: o driver NVIDIA (NVML, nvml.dll), o DirectX (DXGI: VRAM real e nível do DirectX 12),
o Windows (WMI, propriedades PnP e registro do driver) e uma tabela de especificações de referência para
o que o hardware não informa (chip, processo, ROPs...).

Adaptado do ResetExplode (GpuInfoCollector.cs, GpuSpecDb.cs, Nvml.cs e Dxgi.cs; licença MIT, © 2026 Gustavo).
"""

import ctypes
import logging
import math
import os
import re
import threading
import uuid
from pathlib import Path

from . import windows

log = logging.getLogger(__name__)
EH_WINDOWS = windows.EH_WINDOWS

# ------------------------------------------------------------------ especificações de referência
# chave "VVVV:DDDD" (PCI vendor:device) -> variantes (a última coluna desempata pelo nome)
# (chip, arquitetura, processo, die mm², transistores bi, shaders, ROPs, TMUs, memória, base MHz, boost MHz, TDP W, lançamento, nome)
_TSMC4N = "TSMC 4N (5 nm)"
_ESPECS = {
    # NVIDIA Blackwell (RTX 50)
    "10DE:2B85": [("GB202", "Blackwell", _TSMC4N, 750, 92.2, 21760, 176, 680, "GDDR7", 2017, 2407, 575, "30/01/2025", None)],
    "10DE:2C02": [("GB203", "Blackwell", _TSMC4N, 378, 45.6, 10752, 112, 336, "GDDR7", 2295, 2617, 360, "30/01/2025", None)],
    "10DE:2C05": [("GB203", "Blackwell", _TSMC4N, 378, 45.6, 8960, 96, 280, "GDDR7", 2295, 2452, 300, "20/02/2025", None)],
    "10DE:2F04": [("GB205", "Blackwell", _TSMC4N, 263, 31.1, 6144, 80, 192, "GDDR7", 2325, 2512, 250, "05/03/2025", None)],
    "10DE:2D04": [("GB206", "Blackwell", _TSMC4N, 181, 21.9, 4608, 48, 144, "GDDR7", 2407, 2572, 180, "16/04/2025", None)],
    "10DE:2D05": [("GB206", "Blackwell", _TSMC4N, 181, 21.9, 3840, 48, 120, "GDDR7", 2280, 2497, 145, "19/05/2025", None)],
    # NVIDIA Ada Lovelace (RTX 40)
    "10DE:2684": [("AD102", "Ada Lovelace", _TSMC4N, 609, 76.3, 16384, 176, 512, "GDDR6X", 2235, 2520, 450, "12/10/2022", None)],
    "10DE:2702": [("AD103", "Ada Lovelace", _TSMC4N, 379, 45.9, 10240, 112, 320, "GDDR6X", 2295, 2550, 320, "31/01/2024", None)],
    "10DE:2704": [("AD103", "Ada Lovelace", _TSMC4N, 379, 45.9, 9728, 112, 304, "GDDR6X", 2205, 2505, 320, "16/11/2022", None)],
    "10DE:2705": [("AD103", "Ada Lovelace", _TSMC4N, 379, 45.9, 8448, 96, 264, "GDDR6X", 2340, 2610, 285, "24/01/2024", None)],
    "10DE:2782": [("AD104", "Ada Lovelace", _TSMC4N, 295, 35.8, 7680, 80, 240, "GDDR6X", 2310, 2610, 285, "05/01/2023", None)],
    "10DE:2783": [("AD104", "Ada Lovelace", _TSMC4N, 295, 35.8, 7168, 80, 224, "GDDR6X", 1980, 2475, 220, "17/01/2024", None)],
    "10DE:2786": [("AD104", "Ada Lovelace", _TSMC4N, 295, 35.8, 5888, 64, 184, "GDDR6X", 1920, 2475, 200, "13/04/2023", None)],
    "10DE:2803": [("AD106", "Ada Lovelace", _TSMC4N, 190, 22.9, 4352, 48, 136, "GDDR6", 2310, 2535, 160, "24/05/2023", None)],
    "10DE:2805": [("AD106", "Ada Lovelace", _TSMC4N, 190, 22.9, 4352, 48, 136, "GDDR6", 2310, 2535, 165, "18/07/2023", None)],
    "10DE:2882": [("AD107", "Ada Lovelace", _TSMC4N, 159, 18.9, 3072, 48, 96, "GDDR6", 1830, 2460, 115, "29/06/2023", None)],
    # NVIDIA Ampere (RTX 30)
    "10DE:2204": [("GA102", "Ampere", "Samsung 8 nm", 628, 28.3, 10496, 112, 328, "GDDR6X", 1395, 1695, 350, "24/09/2020", None)],
    "10DE:2206": [("GA102", "Ampere", "Samsung 8 nm", 628, 28.3, 8704, 96, 272, "GDDR6X", 1440, 1710, 320, "17/09/2020", None)],
    "10DE:2484": [("GA104", "Ampere", "Samsung 8 nm", 392, 17.4, 5888, 96, 184, "GDDR6", 1500, 1725, 220, "29/10/2020", None)],
    "10DE:2486": [("GA104", "Ampere", "Samsung 8 nm", 392, 17.4, 4864, 80, 152, "GDDR6", 1410, 1665, 200, "02/12/2020", None)],
    "10DE:2503": [("GA106", "Ampere", "Samsung 8 nm", 276, 12.0, 3584, 48, 112, "GDDR6", 1320, 1777, 170, "25/02/2021", None)],
    "10DE:2504": [("GA106", "Ampere", "Samsung 8 nm", 276, 12.0, 3584, 48, 112, "GDDR6", 1320, 1777, 170, "25/02/2021", None)],
    # AMD RDNA 4 / 3 / 2
    "1002:7550": [("Navi 48", "RDNA 4", "TSMC N4P (4 nm)", 357, 53.9, 4096, 128, 256, "GDDR6", 2400, 2970, 304, "06/03/2025", "9070 XT"),
                  ("Navi 48", "RDNA 4", "TSMC N4P (4 nm)", 357, 53.9, 3584, 128, 224, "GDDR6", 2070, 2520, 220, "06/03/2025", "9070")],
    "1002:744C": [("Navi 31", "RDNA 3", "TSMC 5 nm + 6 nm", 529, 57.7, 6144, 192, 384, "GDDR6", 1855, 2499, 355, "13/12/2022", "XTX"),
                  ("Navi 31", "RDNA 3", "TSMC 5 nm + 6 nm", 529, 57.7, 5376, 192, 336, "GDDR6", 1500, 2394, 315, "13/12/2022", "7900 XT")],
    "1002:747E": [("Navi 32", "RDNA 3", "TSMC 5 nm + 6 nm", 346, 28.1, 3840, 96, 240, "GDDR6", 1295, 2430, 263, "06/09/2023", "7800"),
                  ("Navi 32", "RDNA 3", "TSMC 5 nm + 6 nm", 346, 28.1, 3456, 96, 216, "GDDR6", 1435, 2544, 245, "06/09/2023", "7700")],
    "1002:7480": [("Navi 33", "RDNA 3", "TSMC 6 nm", 204, 13.3, 2048, 64, 128, "GDDR6", 1720, 2655, 165, "25/05/2023", None)],
    "1002:164E": [("Raphael (iGPU)", "RDNA 2", "TSMC 6 nm", 122, 3.4, 128, 4, 8, "Memória do sistema (DDR5)", 400, 2200, 0, "27/09/2022", None)],
}
_CAMPOS = ("chip", "arquitetura", "processo", "die", "transistores", "shaders", "rops", "tmus", "memoria",
           "base", "boost", "tdp", "lancamento", "nome")

RECURSOS = {
    "Blackwell": "DLSS 4 (Multi Frame Generation) · Ray Tracing 4ª ger. · Tensor 5ª ger. · Reflex 2 · NVENC 9ª ger.",
    "Ada Lovelace": "DLSS 3 (Frame Generation) · Ray Tracing 3ª ger. · Tensor 4ª ger. · Reflex · NVENC AV1",
    "Ampere": "DLSS 2 · Ray Tracing 2ª ger. · Tensor 3ª ger. · Reflex · Resizable BAR",
    "RDNA 4": "FSR 4 · Ray Accelerators 3ª ger. · AI Accelerators · Anti-Lag 2 · AV1",
    "RDNA 3": "FSR 3 · Ray Accelerators 2ª ger. · AI Accelerators · AV1",
    "RDNA 2": "FSR · Ray Accelerators 1ª ger.",
}

FABRICANTES = {
    "1043": "ASUS", "1458": "Gigabyte", "1462": "MSI", "3842": "EVGA", "19DA": "Zotac", "196E": "PNY",
    "10DE": "NVIDIA", "1002": "AMD", "1DA2": "Sapphire", "1682": "XFX", "148C": "PowerColor", "1849": "ASRock",
    "7377": "Colorful", "1569": "Palit", "10B0": "Gainward", "1B4C": "Galax / KFA2", "8086": "Intel",
    "1028": "Dell", "103C": "HP", "17AA": "Lenovo", "1025": "Acer", "144D": "Samsung", "1D05": "Tongfang",
}
VENDEDORES = {"10DE": "NVIDIA", "1002": "AMD", "8086": "Intel"}


def especificacao(ven: str, dev: str, nome: str) -> dict | None:
    variantes = _ESPECS.get(f"{ven}:{dev}".upper())
    if not variantes:
        return None
    escolhida = next((v for v in variantes if v[-1] and v[-1].lower() in nome.lower()), variantes[0])
    return dict(zip(_CAMPOS, escolhida))


def fabricante_placa(sub_ven: str) -> str:
    return FABRICANTES.get(sub_ven.upper(), f"Desconhecido ({sub_ven})")


def nome_pcie(geracao) -> str:
    return {1: "PCIe 1.1", 2: "PCIe 2.0", 3: "PCIe 3.0", 4: "PCIe 4.0", 5: "PCIe 5.0", 6: "PCIe 6.0"}.get(
        geracao, f"PCIe gen {geracao}")


def tamanho(b) -> str:
    if not b:
        return "—"
    v, i = float(b), 0
    while v >= 1024 and i < 4:
        v, i = v / 1024, i + 1
    return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " " + ("B", "KB", "MB", "GB", "TB")[i]


def gb_comercial(vram: int) -> int:
    """O driver reserva parte da VRAM: 12227 MiB reportados = placa de 12 GB (como mostra o GPU-Z)."""
    return math.ceil(vram / 2**30 - 0.15)


# ------------------------------------------------------------------ NVML (driver NVIDIA)

class _PciInfo(ctypes.Structure):
    _fields_ = [("busIdLegacy", ctypes.c_char * 16), ("domain", ctypes.c_uint), ("bus", ctypes.c_uint),
                ("device", ctypes.c_uint), ("pciDeviceId", ctypes.c_uint), ("pciSubSystemId", ctypes.c_uint),
                ("busId", ctypes.c_char * 32)]


class _Memoria(ctypes.Structure):  # mesmo layout de nvmlBAR1Memory_t
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong), ("used", ctypes.c_ulonglong)]


class _Uso(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


CLOCK_GRAFICO, CLOCK_SM, CLOCK_MEMORIA, CLOCK_VIDEO = 0, 1, 2, 3


class Nvml:
    """Chamadas mínimas da NVML. Cada método devolve None se a função ou a placa não responder."""

    def __init__(self):
        self.lib = None
        if not EH_WINDOWS:
            return
        candidatos = ["nvml.dll", str(Path(os.environ.get("ProgramW6432", r"C:\Program Files"))
                                      / "NVIDIA Corporation" / "NVSMI" / "nvml.dll")]
        for c in candidatos:
            try:
                lib = ctypes.CDLL(c)
            except OSError:
                continue
            try:
                if lib.nvmlInit_v2() == 0:
                    self.lib = lib
                    break
            except AttributeError:
                continue

    @property
    def disponivel(self) -> bool:
        return self.lib is not None

    def _ok(self, funcao: str, *args) -> bool:
        f = getattr(self.lib, funcao, None) if self.lib else None
        if f is None:
            return False
        try:
            return f(*args) == 0
        except OSError:
            return False

    def _uint(self, funcao: str, h, *extra):
        v = ctypes.c_uint()
        return v.value if self._ok(funcao, ctypes.c_void_p(h), *extra, ctypes.byref(v)) else None

    def dispositivos(self) -> list[dict]:
        n = ctypes.c_uint()
        if not self._ok("nvmlDeviceGetCount_v2", ctypes.byref(n)):
            return []
        saida = []
        for i in range(n.value):
            h, pci = ctypes.c_void_p(), _PciInfo()
            if self._ok("nvmlDeviceGetHandleByIndex_v2", ctypes.c_uint(i), ctypes.byref(h)) and \
                    self._ok("nvmlDeviceGetPciInfo_v3", h, ctypes.byref(pci)):
                saida.append({"handle": h.value, "device": pci.pciDeviceId >> 16, "bus": pci.bus,
                              "subsys": pci.pciSubSystemId})
        return saida

    def _texto(self, funcao: str, *args, tam: int = 96):
        buf = ctypes.create_string_buffer(tam)
        return buf.value.decode(errors="replace") if self._ok(funcao, *args, buf, ctypes.c_uint(tam)) else None

    def versao_driver(self):
        return self._texto("nvmlSystemGetDriverVersion")

    def versao_cuda(self):
        v = ctypes.c_int()
        return f"{v.value // 1000}.{v.value % 1000 // 10}" if self._ok("nvmlSystemGetCudaDriverVersion_v2", ctypes.byref(v)) else None

    def vbios(self, h):
        return self._texto("nvmlDeviceGetVbiosVersion", ctypes.c_void_p(h), tam=64)

    def memoria(self, h):
        m = _Memoria()
        return {"total": m.total, "livre": m.free, "usada": m.used} if self._ok(
            "nvmlDeviceGetMemoryInfo", ctypes.c_void_p(h), ctypes.byref(m)) else None

    def bar1(self, h):
        m = _Memoria()
        return m.total if self._ok("nvmlDeviceGetBAR1MemoryInfo", ctypes.c_void_p(h), ctypes.byref(m)) else None

    def barramento(self, h): return self._uint("nvmlDeviceGetMemoryBusWidth", h)
    def nucleos(self, h): return self._uint("nvmlDeviceGetNumGpuCores", h)
    def pcie_max_ger(self, h): return self._uint("nvmlDeviceGetMaxPcieLinkGeneration", h)
    def pcie_max_largura(self, h): return self._uint("nvmlDeviceGetMaxPcieLinkWidth", h)
    def pcie_ger(self, h): return self._uint("nvmlDeviceGetCurrPcieLinkGeneration", h)
    def pcie_largura(self, h): return self._uint("nvmlDeviceGetCurrPcieLinkWidth", h)
    def limite_padrao_mw(self, h): return self._uint("nvmlDeviceGetPowerManagementDefaultLimit", h)
    def limite_mw(self, h): return self._uint("nvmlDeviceGetEnforcedPowerLimit", h)
    def consumo_mw(self, h): return self._uint("nvmlDeviceGetPowerUsage", h)
    def clock_max(self, h, tipo): return self._uint("nvmlDeviceGetMaxClockInfo", h, ctypes.c_int(tipo))
    def clock(self, h, tipo): return self._uint("nvmlDeviceGetClockInfo", h, ctypes.c_int(tipo))
    def temperatura(self, h): return self._uint("nvmlDeviceGetTemperature", h, ctypes.c_int(0))
    def ventoinha(self, h): return self._uint("nvmlDeviceGetFanSpeed", h)

    def faixa_limite_mw(self, h):
        a, b = ctypes.c_uint(), ctypes.c_uint()
        return (a.value, b.value) if self._ok("nvmlDeviceGetPowerManagementLimitConstraints", ctypes.c_void_p(h),
                                              ctypes.byref(a), ctypes.byref(b)) else None

    def compute_capability(self, h):
        a, b = ctypes.c_int(), ctypes.c_int()
        return f"{a.value}.{b.value}" if self._ok("nvmlDeviceGetCudaComputeCapability", ctypes.c_void_p(h),
                                                  ctypes.byref(a), ctypes.byref(b)) else None

    def pstate(self, h):
        v = ctypes.c_int()
        return v.value if self._ok("nvmlDeviceGetPerformanceState", ctypes.c_void_p(h), ctypes.byref(v)) else None

    def limitadores(self, h):
        v = ctypes.c_ulonglong()
        return v.value if self._ok("nvmlDeviceGetCurrentClocksThrottleReasons", ctypes.c_void_p(h),
                                   ctypes.byref(v)) else None

    def uso(self, h):
        u = _Uso()
        return {"gpu": u.gpu, "memoria": u.memory} if self._ok("nvmlDeviceGetUtilizationRates", ctypes.c_void_p(h),
                                                               ctypes.byref(u)) else None

    def _uso_codec(self, funcao, h):
        v, p = ctypes.c_uint(), ctypes.c_uint()
        return v.value if self._ok(funcao, ctypes.c_void_p(h), ctypes.byref(v), ctypes.byref(p)) else None

    def encoder(self, h): return self._uso_codec("nvmlDeviceGetEncoderUtilization", h)
    def decoder(self, h): return self._uso_codec("nvmlDeviceGetDecoderUtilization", h)


_nvml: Nvml | None = None
_trava = threading.Lock()


def nvml() -> Nvml:
    global _nvml
    with _trava:
        if _nvml is None:
            _nvml = Nvml()
        return _nvml


def limitador(r: int) -> str:
    partes = []
    if r & 0x4:
        partes.append("Energia")
    if r & 0x60:
        partes.append("Térmico")
    if r & 0x8:
        partes.append("HW slowdown")
    if r & 0x80:
        partes.append("Power brake")
    if not partes:
        return "Ociosa" if r & 0x1 else "Nenhum"
    return " + ".join(partes)


def ao_vivo(handle) -> list[list[str]]:
    """Leituras dinâmicas da NVML que os sensores comuns não trazem (P-state, limitador...)."""
    n = nvml()
    if not handle or not n.disponivel:
        return []
    saida = []
    if (p := n.pstate(handle)) is not None:
        saida.append(["Estado de desempenho", f"P{p}"])
    if (r := n.limitadores(handle)) is not None:
        saida.append(["Limitador (PerfCap)", limitador(r)])
    pw, lim = n.consumo_mw(handle), n.limite_mw(handle)
    if pw is not None and lim:
        saida.append(["Consumo / limite", f"{pw / 1000:.1f} W · {100 * pw / lim:.0f}%".replace(".", ",")])
    if (u := n.uso(handle)) is not None:
        saida.append(["Controlador de memória", f"{u['memoria']}%"])
    if (e := n.encoder(handle)) is not None:
        saida.append(["Encoder (NVENC)", f"{e}%"])
    if (d := n.decoder(handle)) is not None:
        saida.append(["Decoder (NVDEC)", f"{d}%"])
    g, w = n.pcie_ger(handle), n.pcie_largura(handle)
    if g and w:
        saida.append(["PCIe em uso", f"{nome_pcie(g)} x{w}"])
    return saida


def leitura_nvml(handle) -> dict:
    """Temperatura, carga, consumo e clocks (usado quando a biblioteca de sensores não está disponível)."""
    n = nvml()
    if not handle or not n.disponivel:
        return {}
    u = n.uso(handle) or {}
    pw = n.consumo_mw(handle)
    mem = n.memoria(handle)
    return {"temp": n.temperatura(handle), "carga": u.get("gpu"), "watts": pw / 1000 if pw is not None else None,
            "clock": n.clock(handle, CLOCK_GRAFICO), "clock_mem": n.clock(handle, CLOCK_MEMORIA),
            "ventoinha": n.ventoinha(handle), "vram_usada": mem["usada"] if mem else None,
            "vram_total": mem["total"] if mem else None}


# ------------------------------------------------------------------ DXGI (DirectX)

class _GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def de(cls, texto: str) -> "_GUID":
        g = cls()
        g.bytes[:] = uuid.UUID(texto).bytes_le
        return g


class _DescAdaptador(ctypes.Structure):
    _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", ctypes.c_uint), ("DeviceId", ctypes.c_uint),
                ("SubSysId", ctypes.c_uint), ("Revision", ctypes.c_uint), ("DedicatedVideoMemory", ctypes.c_size_t),
                ("DedicatedSystemMemory", ctypes.c_size_t), ("SharedSystemMemory", ctypes.c_size_t),
                ("LuidLow", ctypes.c_uint32), ("LuidHigh", ctypes.c_int32), ("Flags", ctypes.c_uint)]


def _metodo(obj: ctypes.c_void_p, indice: int, *argtypes):
    """Função da vtable de um objeto COM (índice contado a partir de QueryInterface = 0)."""
    tabela = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(tabela[indice])


def adaptadores_dxgi(com_nivel: bool = True) -> list[dict]:
    """VRAM real (sem o limite de 4 GB do WMI), LUID e o maior nível do DirectX 12 aceito."""
    if not EH_WINDOWS:
        return []
    lista = []
    try:
        dxgi = ctypes.WinDLL("dxgi")
        dxgi.CreateDXGIFactory1.restype = ctypes.c_long
        fabrica = ctypes.c_void_p()
        if dxgi.CreateDXGIFactory1(ctypes.byref(_GUID.de("770aae78-f26f-4dba-a829-253c83d1b387")),
                                   ctypes.byref(fabrica)) < 0:
            return lista
        try:
            d3d12 = ctypes.WinDLL("d3d12")
            d3d12.D3D12CreateDevice.restype = ctypes.c_long
            d3d12.D3D12CreateDevice.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        except OSError:
            d3d12 = None
        enum1 = _metodo(fabrica, 12, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        i = 0
        while True:
            adaptador = ctypes.c_void_p()
            if enum1(fabrica, i, ctypes.byref(adaptador)) < 0:
                break
            i += 1
            desc = _DescAdaptador()
            if _metodo(adaptador, 10, ctypes.POINTER(_DescAdaptador))(adaptador, ctypes.byref(desc)) >= 0 \
                    and not desc.Flags & 2:  # ignora o adaptador de software (WARP)
                nivel = None
                if d3d12 and com_nivel:
                    iid = _GUID.de("189819f1-1db6-4b57-be54-1821339b85f7")  # ID3D12Device
                    for valor, nome in ((0xc200, "12_2"), (0xc100, "12_1"), (0xc000, "12_0"), (0xb100, "11_1"), (0xb000, "11_0")):
                        if d3d12.D3D12CreateDevice(adaptador, valor, ctypes.byref(iid), None) >= 0:
                            nivel = nome
                            break
                lista.append({"nome": desc.Description, "ven": f"{desc.VendorId:04X}", "dev": f"{desc.DeviceId:04X}",
                              "vram": desc.DedicatedVideoMemory, "compartilhada": desc.SharedSystemMemory,
                              "luid": f"0x{desc.LuidHigh & 0xFFFFFFFF:08X}_0x{desc.LuidLow:08X}", "nivel": nivel})
            _metodo(adaptador, 2)(adaptador)  # Release
        _metodo(fabrica, 2)(fabrica)
    except (OSError, ValueError):
        log.warning("DXGI indisponível", exc_info=True)
    return lista


# ------------------------------------------------------------------ Windows (WMI, PnP, registro)

_SCRIPT = r"""
$ks='DEVPKEY_PciDevice_CurrentLinkSpeed','DEVPKEY_PciDevice_CurrentLinkWidth','DEVPKEY_PciDevice_MaxLinkSpeed','DEVPKEY_PciDevice_MaxLinkWidth','DEVPKEY_Device_Driver','DEVPKEY_Device_LocationInfo'
@(foreach($g in Get-CimInstance Win32_VideoController){
  $p=@{}; $id=[string]$g.PNPDeviceID
  if($id){ try { Get-PnpDeviceProperty -InstanceId $id -KeyName $ks -ErrorAction Stop | ForEach-Object { if($_.Data -ne $null){ $p[$_.KeyName]=$_.Data } } } catch {} }
  $s=$null; if($id){ $s=Get-CimInstance Win32_PnPSignedDriver -Filter ("DeviceID='"+$id.Replace('\','\\')+"'") -ErrorAction SilentlyContinue | Select-Object -First 1 }
  [pscustomobject]@{ Nome=$g.Name; Pnp=$id; Driver=$g.DriverVersion; RamWmi=$g.AdapterRAM
    DataDriver=$(if($g.DriverDate){$g.DriverDate.ToString('dd/MM/yyyy')}); Larg=$g.CurrentHorizontalResolution
    Alt=$g.CurrentVerticalResolution; Hz=$g.CurrentRefreshRate; Props=$p; Assinado=$s.IsSigned; Assinante=$s.Signer }
}) | ConvertTo-Json -Depth 4 -Compress
"""


def _registro_driver(chave: str | None) -> dict:
    """Valores HardwareInformation.* da chave de classe do driver de vídeo."""
    if not chave or not EH_WINDOWS:
        return {}
    import winreg
    valores = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Control\Class\{chave}") as k:
            i = 0
            while True:
                try:
                    nome, valor, _ = winreg.EnumValue(k, i)
                except OSError:
                    break
                i += 1
                if nome.startswith("HardwareInformation."):
                    valores[nome] = valor
    except OSError:
        pass
    return valores


def _texto_registro(valor) -> str | None:
    """Alguns drivers gravam texto como binário UTF-16."""
    if valor is None:
        return None
    if isinstance(valor, bytes):
        valor = valor.decode("utf-16-le", errors="ignore")
    texto = str(valor).rstrip("\0").strip()
    return texto or None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _no(nome, valor=None, filhos=None) -> dict:
    return {"nome": nome, "valor": valor, "filhos": filhos}


def _montar(vc: dict, dxgi: list[dict], nvs: list[dict]) -> dict:
    nome = (vc.get("Nome") or "Placa de vídeo").strip()
    pnp = vc.get("Pnp") or ""
    m = re.search(r"VEN_([0-9A-F]{4})&DEV_([0-9A-F]{4})(?:&SUBSYS_([0-9A-F]{4})([0-9A-F]{4}))?(?:&REV_([0-9A-F]{2}))?",
                  pnp, re.I)
    ven, dev, sub_dev, sub_ven, rev = ((g or "").upper() for g in m.groups()) if m else ("", "", "", "", "")
    esp = especificacao(ven, dev, nome)
    dx = next((a for a in dxgi if a["ven"] == ven and a["dev"] == dev), None)
    n = nvml()
    h = None
    if ven == "10DE" and dev:
        h = next((d["handle"] for d in nvs if d["device"] == int(dev, 16)), None)
    props = vc.get("Props") or {}
    reg = _registro_driver(props.get("DEVPKEY_Device_Driver"))
    secoes = []

    def secao(titulo):
        itens = []
        secoes.append({"titulo": titulo, "itens": itens})
        return itens

    # identificação
    s = secao("Identificação")
    s += [["Nome", nome], ["Chip", esp["chip"] if esp else "—"], ["Arquitetura", esp["arquitetura"] if esp else "—"],
          ["Revisão", rev or "—"], ["Fabricante da placa", fabricante_placa(sub_ven) if sub_ven else "—"],
          ["Device ID", f"{ven} {dev}" + (f" · {sub_ven} {sub_dev}" if sub_ven else "")],
          ["Processo", esp["processo"] if esp else "—"], ["Área do die", f"{esp['die']} mm²" if esp else "—"],
          ["Transistores", f"{esp['transistores']:.1f} bilhões".replace(".", ",") if esp else "—"],
          ["Lançamento", esp["lancamento"] if esp else "—"]]

    # unidades e desempenho teórico
    s = secao("Unidades de processamento")
    shaders = (n.nucleos(h) if h else None) or (esp["shaders"] if esp else 0)
    nv = ven == "10DE"
    if shaders:
        por = 128 if nv else 64
        s.append(["CUDA cores" if nv else "Stream processors", f"{shaders:,}".replace(",", ".")])
        s.append(["SMs" if nv else "Compute units", str(shaders // por)])
        if nv and esp and esp["arquitetura"] in ("Blackwell", "Ada Lovelace", "Ampere"):
            s += [["Tensor cores", str(shaders // por * 4)], ["RT cores", str(shaders // por)]]
        elif not nv and esp and esp["arquitetura"].startswith("RDNA"):
            s.append(["Ray accelerators", str(shaders // por)])
    if esp:
        s += [["ROPs / TMUs", f"{esp['rops']} / {esp['tmus']}"],
              ["Pixel fillrate", f"{esp['rops'] * esp['boost'] / 1000:.1f} GPixel/s".replace(".", ",")],
              ["Texture fillrate", f"{esp['tmus'] * esp['boost'] / 1000:.1f} GTexel/s".replace(".", ",")],
              ["FP32 (teórico)", f"{(shaders or esp['shaders']) * 2 * esp['boost'] / 1e6:.2f} TFLOPS".replace(".", ",")]]
    if not s:
        s.append(["Sem dados", "Placa fora da tabela de especificações"])

    # memória
    s = secao("Memória")
    vram = 0
    if h and (mem := n.memoria(h)):
        vram = mem["total"]
    if not vram and dx:
        vram = dx["vram"]
    if not vram:
        vram = _int(reg.get("HardwareInformation.qwMemorySize")) or 0
    igpu = bool(esp and esp["memoria"].startswith("Memória do sistema"))
    if igpu or vram < 2**30:
        s.append(["Reservada (dedicada)" if igpu else "Tamanho", tamanho(vram)])
    else:
        s.append(["Tamanho", f"{gb_comercial(vram)} GB ({vram >> 20:,} MB utilizáveis)".replace(",", ".")])
    if dx and dx["compartilhada"]:
        s.append(["Compartilhada do sistema", tamanho(dx["compartilhada"])])
    s.append(["Tipo", esp["memoria"] if esp else "—"])
    bus = n.barramento(h) if h else None
    clk_mem = n.clock_max(h, CLOCK_MEMORIA) if h else None
    if bus:
        s.append(["Barramento", f"{bus} bits"])
    if clk_mem:
        gbps = clk_mem * 2 / 1000  # a NVML informa metade da taxa efetiva (GDDR6/6X/7)
        s.append(["Clock da memória", f"{clk_mem} MHz ({gbps:.1f} Gbps efetivos)".replace(".", ",")])
        if bus:
            s.append(["Largura de banda", f"{gbps * bus / 8:.1f} GB/s".replace(".", ",")])
    if h and (bar := n.bar1(h)):
        s.append(["Resizable BAR", f"Ativado (BAR1 {tamanho(bar)})" if bar > 256 * 2**20 else "Desativado"])

    # clocks
    s = secao("Clocks")
    if esp:
        s += [["Base (referência)", f"{esp['base']} MHz"], ["Boost (referência)", f"{esp['boost']} MHz"]]
    if h:
        if g := n.clock_max(h, CLOCK_GRAFICO):
            s.append(["Máximo (curva V/F)", f"{g} MHz"])
        if v := n.clock_max(h, CLOCK_VIDEO):
            s.append(["Video engine máx.", f"{v} MHz"])
    if not s:
        s.append(["Sem dados", "—"])

    # barramento
    s = secao("Barramento")
    max_g = (n.pcie_max_ger(h) if h else None) or _int(props.get("DEVPKEY_PciDevice_MaxLinkSpeed"))
    max_w = (n.pcie_max_largura(h) if h else None) or _int(props.get("DEVPKEY_PciDevice_MaxLinkWidth"))
    cur_g = (n.pcie_ger(h) if h else None) or _int(props.get("DEVPKEY_PciDevice_CurrentLinkSpeed"))
    cur_w = (n.pcie_largura(h) if h else None) or _int(props.get("DEVPKEY_PciDevice_CurrentLinkWidth"))
    if max_g and max_w:
        s.append(["Interface", f"{nome_pcie(max_g)} x{max_w}"])
    if cur_g and cur_w:
        extra = " (economia de energia em repouso)" if max_g and max_g > cur_g else ""
        if max_w and cur_w < max_w:
            extra = f" (a placa suporta x{max_w}: confira se está no slot principal)"
        s.append(["Em uso agora", f"{nome_pcie(cur_g)} x{cur_w}{extra}"])
    if props.get("DEVPKEY_Device_LocationInfo"):
        s.append(["Localização", str(props["DEVPKEY_Device_LocationInfo"])])
    if not s:
        s.append(["Sem dados", "—"])

    # energia
    if h or (esp and esp["tdp"]):
        s = secao("Energia")
        if esp and esp["tdp"]:
            s.append(["TDP (referência)", f"{esp['tdp']} W"])
        if h:
            if (lim := n.limite_mw(h)) is not None:
                s.append(["Limite atual", f"{lim / 1000:.0f} W"])
            if (pad := n.limite_padrao_mw(h)) is not None:
                s.append(["Limite padrão da placa", f"{pad / 1000:.0f} W"])
            if fx := n.faixa_limite_mw(h):
                s.append(["Faixa ajustável", f"{fx[0] / 1000:.0f} – {fx[1] / 1000:.0f} W"])

    # driver e BIOS
    s = secao("Driver e BIOS")
    vbios = (n.vbios(h) if h else None) or _texto_registro(reg.get("HardwareInformation.BiosString"))
    s.append(["BIOS (VBIOS)", vbios or "—"])
    ver_wmi = vc.get("Driver") or "—"
    ver_nv = n.versao_driver() if h else None
    s.append(["Versão do driver", f"{ver_nv} ({ver_wmi})" if ver_nv else ver_wmi])
    if vc.get("DataDriver"):
        s.append(["Data do driver", vc["DataDriver"]])
    if vc.get("Assinado") is False:
        assinatura = "Não assinado"
    elif vc.get("Assinado"):
        sig = vc.get("Assinante") or ""
        assinatura = "WHQL (Microsoft)" if "windows hardware compatibility" in sig.lower() else f"Assinado · {sig}"
    else:
        assinatura = "—"
    s.append(["Assinatura digital", assinatura])
    if h:
        if c := n.versao_cuda():
            s.append(["Versão CUDA", c])
        if c := n.compute_capability(h):
            s.append(["Compute capability", c])

    # tecnologias
    s = secao("Tecnologias")
    nivel = dx["nivel"] if dx else None
    s.append(["DirectX", "DirectX 12 Ultimate (12_2)" if nivel == "12_2" else
              f"DirectX 12 ({nivel.replace('_', '.')})" if nivel else "—"])
    sys32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    apis = []
    if nv and (sys32 / "nvcuda.dll").exists():
        apis.append("CUDA")
    if (sys32 / "OpenCL.dll").exists():
        apis.append("OpenCL")
    if (sys32 / "vulkan-1.dll").exists():
        apis.append("Vulkan")
    if nivel:
        apis += ["DirectCompute", "DirectML"]
    if nv:
        apis.append("PhysX")
    s.append(["APIs", " · ".join(apis) or "—"])
    if esp and esp["arquitetura"] in RECURSOS:
        s.append(["Recursos da arquitetura", RECURSOS[esp["arquitetura"]]])
    if vc.get("Larg"):
        s.append(["Tela principal", f"{vc['Larg']}×{vc.get('Alt')} a {vc.get('Hz')} Hz"])

    mem_rot = esp["memoria"] if esp and esp["memoria"].startswith("GDDR") else ""
    subtitulo = "  ·  ".join(x for x in (
        esp["chip"] if esp else None, esp["arquitetura"] if esp else None, esp["processo"] if esp else None,
        f"{gb_comercial(vram)} GB {mem_rot}".strip() if vram >= 2**30 and not igpu else None,
        fabricante_placa(sub_ven) if sub_ven else None) if x)
    return {"nome": nome, "subtitulo": subtitulo, "fabricante": VENDEDORES.get(ven, ven or "—"), "vram": vram,
            "handle": h, "luid": dx["luid"] if dx else None, "integrada": igpu or (vram and vram < 2**30),
            "secoes": secoes}


def coletar() -> list[dict]:
    """Todas as placas de vídeo, a dedicada (mais VRAM) primeiro."""
    if not EH_WINDOWS:
        return []
    dxgi = adaptadores_dxgi()
    nvs = nvml().dispositivos()
    placas = []
    for vc in windows.powershell_json(_SCRIPT, timeout=60):
        try:
            placas.append(_montar(vc, dxgi, nvs))
        except Exception as e:  # uma placa estranha não pode derrubar as outras
            log.exception("falha ao ler placa de vídeo")
            placas.append({"nome": vc.get("Nome") or "Placa de vídeo", "subtitulo": "", "fabricante": "—", "vram": 0,
                           "handle": None, "luid": None, "integrada": False,
                           "secoes": [{"titulo": "Erro", "itens": [["Falha ao ler", str(e)]]}]})
    placas.sort(key=lambda p: p["vram"], reverse=True)
    return placas


# ------------------------------------------------------------------ carga pelo Windows (qualquer marca)

class CargaGpu:
    """Carga 3D e VRAM em uso por adaptador, pelos mesmos contadores do Gerenciador de Tarefas."""

    def __init__(self):
        self._pdh = None
        if EH_WINDOWS:
            try:
                self._pdh = windows.ContadoresPDH({
                    "motor": r"\GPU Engine(*)\Utilization Percentage",
                    "vram": r"\GPU Adapter Memory(*)\Dedicated Usage",
                })
            except OSError:
                self._pdh = None

    def ler(self) -> dict[str, dict]:
        """{luid: {"carga": %, "vram_usada": bytes}}"""
        if not self._pdh:
            return {}
        dados = self._pdh.ler()
        por_luid: dict[str, dict] = {}
        motores: dict[tuple, float] = {}
        for inst, v in dados.get("motor", {}).items():
            m = re.search(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+).*engtype_(\w+)", inst, re.I)
            if m:
                chave = (m.group(1).upper().replace("0X", "0x"), m.group(2))
                motores[chave] = motores.get(chave, 0.0) + v
        for (luid, _tipo), v in motores.items():
            d = por_luid.setdefault(luid, {})
            d["carga"] = min(100.0, max(d.get("carga", 0.0), v))  # igual ao Gerenciador: o motor mais ocupado
        for inst, v in dados.get("vram", {}).items():
            m = re.search(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", inst, re.I)
            if m:
                d = por_luid.setdefault(m.group(1).upper().replace("0X", "0x"), {})
                d["vram_usada"] = d.get("vram_usada", 0) + v
        return por_luid
