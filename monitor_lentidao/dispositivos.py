"""Aviso quando um pendrive, HD externo ou dispositivo USB é conectado ou removido.

Uma janela invisível recebe o WM_DEVICECHANGE do Windows. Unidades (letras) chegam direto na mensagem;
para os outros dispositivos USB, a lista atual é comparada com a anterior depois que as mensagens param.
O nome mostrado é o que o próprio aparelho informa (ex.: "G502 HERO Gaming Mouse"), não o genérico do Windows.

Ideia e regras adaptadas do ResetExplode (MainWindow.xaml.cs e UsbCleaner.cs; licença MIT, © 2026 Gustavo).
"""

import ctypes
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime

from . import windows

log = logging.getLogger(__name__)

FABRICANTES_USB = {
    "2341": "Arduino", "2A03": "Arduino", "1A86": "CH340/CH341", "0403": "FTDI", "10C4": "CP210x",
    "303A": "Espressif (ESP32)", "239A": "Adafruit", "16C0": "Teensy", "1B4F": "SparkFun", "2E8A": "Raspberry Pi Pico",
    "067B": "Prolific PL2303", "0483": "STMicro", "1366": "SEGGER", "046D": "Logitech", "1532": "Razer",
    "1B1C": "Corsair", "1038": "SteelSeries", "0951": "Kingston", "0781": "SanDisk", "045E": "Microsoft",
    "054C": "Sony", "057E": "Nintendo", "28DE": "Valve", "0BDA": "Realtek", "8087": "Intel",
}
GENERICOS = ("composite", "composto", "input device", "entrada usb", "hub", "mass storage", "armazenamento em massa",
             "usb device", "dispositivo usb", "dispositivo de entrada", "unknown", "desconhecido", "root")
ARMAZENAMENTO = ("mass storage", "armazenamento em massa", "usbstor", "uas")

eventos: deque = deque(maxlen=30)  # avisos recentes, para a Visão geral


def _registrar(titulo: str, texto: str) -> None:
    eventos.appendleft({"quando": datetime.now().strftime("%d/%m %H:%M"), "titulo": titulo, "texto": texto})


def _generico(nome: str) -> bool:
    n = nome.lower()
    return any(g in n for g in GENERICOS)


# ------------------------------------------------------------------ lista de dispositivos USB (SetupAPI)

if windows.EH_WINDOWS:
    from ctypes import wintypes

    class _DevInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("ClassGuid", ctypes.c_ubyte * 16), ("DevInst", wintypes.DWORD),
                    ("Reserved", ctypes.c_void_p)]

    class _PropKey(ctypes.Structure):
        _fields_ = [("fmtid", ctypes.c_ubyte * 16), ("pid", wintypes.ULONG)]

    def _chave(guid: str, pid: int) -> _PropKey:
        import uuid
        k = _PropKey()
        k.fmtid[:] = uuid.UUID(guid).bytes_le
        k.pid = pid
        return k

    _BUS_DESC = _chave("540b947e-8b40-45bc-a8a2-6a0b894cbda2", 4)  # DEVPKEY_Device_BusReportedDeviceDesc
    _SPDRP_DEVICEDESC, _SPDRP_CLASS, _SPDRP_FRIENDLYNAME, _SPDRP_MFG = 0x0, 0x7, 0xC, 0xB


def usb_presentes() -> dict[str, dict]:
    """{id do aparelho: {nome, fabricante, armazenamento}}; interfaces de um mesmo aparelho viram um item só."""
    if not windows.EH_WINDOWS:
        return {}
    setup = ctypes.WinDLL("setupapi", use_last_error=True)
    setup.SetupDiGetClassDevsW.restype = ctypes.c_void_p
    setup.SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, wintypes.DWORD]
    setup.SetupDiEnumDeviceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    setup.SetupDiGetDeviceInstanceIdW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, wintypes.DWORD,
                                                  ctypes.c_void_p]
    setup.SetupDiGetDeviceRegistryPropertyW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                                        ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
    setup.SetupDiGetDevicePropertyW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
    h = setup.SetupDiGetClassDevsW(None, "USB", None, 0x2 | 0x4)  # DIGCF_PRESENT | DIGCF_ALLCLASSES
    if not h or h == ctypes.c_void_p(-1).value:
        return {}
    aparelhos: dict[str, dict] = {}
    try:
        dado = _DevInfo(cbSize=ctypes.sizeof(_DevInfo))
        buf = ctypes.create_unicode_buffer(1024)
        i = 0
        while setup.SetupDiEnumDeviceInfo(h, i, ctypes.byref(dado)):
            i += 1

            def prop_reg(p):
                if setup.SetupDiGetDeviceRegistryPropertyW(h, ctypes.byref(dado), p, None, buf, ctypes.sizeof(buf), None):
                    return buf.value.strip() or None
                return None

            if not setup.SetupDiGetDeviceInstanceIdW(h, ctypes.byref(dado), buf, len(buf), None):
                continue
            ident = buf.value.upper()
            if ident.startswith("USB\\ROOT_HUB"):
                continue
            tipo = wintypes.ULONG()
            barramento = None
            if setup.SetupDiGetDevicePropertyW(h, ctypes.byref(dado), ctypes.byref(_BUS_DESC), ctypes.byref(tipo),
                                               buf, ctypes.sizeof(buf), None, 0):
                barramento = buf.value.strip() or None
            nome = prop_reg(_SPDRP_FRIENDLYNAME) or prop_reg(_SPDRP_DEVICEDESC) or ident
            classe = (prop_reg(_SPDRP_CLASS) or "").lower()
            m = re.match(r"USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", ident)
            base = m.group(0) if m else ident  # interfaces de um aparelho composto viram um item só
            vid = m.group(1) if m else None
            a = aparelhos.setdefault(base, {"nomes": [], "fabricante": FABRICANTES_USB.get(vid or ""),
                                           "armazenamento": False, "hub": False})
            a["nomes"] += [n for n in (barramento, nome) if n]
            a["armazenamento"] |= any(k in (nome + " " + classe).lower() for k in ARMAZENAMENTO)
            a["hub"] |= "hub" in nome.lower() and not barramento
    finally:
        setup.SetupDiDestroyDeviceInfoList(h)
    saida = {}
    for chave, a in aparelhos.items():
        if a["hub"]:
            continue
        nome = next((n for n in a["nomes"] if not _generico(n)), a["nomes"][0] if a["nomes"] else "Dispositivo USB")
        saida[chave] = {"nome": nome, "fabricante": a["fabricante"], "armazenamento": a["armazenamento"]}
    return saida


# ------------------------------------------------------------------ unidades (letras)

def _info_unidade(letra: str) -> tuple[str, bool, int, int]:
    """(rótulo, removível, total, livre)"""
    import shutil
    k32 = ctypes.windll.kernel32
    raiz = letra + "\\"
    rotulo = ctypes.create_unicode_buffer(261)
    k32.GetVolumeInformationW(raiz, rotulo, 261, None, None, None, None, 0)
    removivel = k32.GetDriveTypeW(raiz) == 2
    try:
        uso = shutil.disk_usage(raiz)
        return rotulo.value, removivel, uso.total, uso.free
    except OSError:
        return rotulo.value, removivel, 0, 0


def _gb(b: int) -> str:
    return f"{b / 2**30:.1f} GB".replace(".", ",") if b >= 2**30 else f"{b / 2**20:.0f} MB"


# ------------------------------------------------------------------ janela invisível

class Vigia:
    """Recebe WM_DEVICECHANGE numa janela invisível e chama `avisar(titulo, texto)`."""

    def __init__(self, avisar):
        self._avisar = avisar
        self._presentes: dict[str, dict] = {}
        self._ultimo_volume = 0.0
        self._timer: threading.Timer | None = None
        self._letras: dict[str, str] = {}

    def iniciar(self) -> None:
        if not windows.EH_WINDOWS:
            return
        threading.Thread(target=self._executar, name="dispositivos", daemon=True).start()

    def _executar(self) -> None:
        try:
            self._presentes = usb_presentes()
        except Exception:
            log.exception("falha ao listar USB")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        LRESULT = ctypes.c_ssize_t
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t)
        user32.DefWindowProcW.restype = LRESULT
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]

        def procedimento(hwnd, msg, wparam, lparam):
            if msg == 0x0219:  # WM_DEVICECHANGE
                try:
                    self._mudou(wparam, lparam)
                except Exception:
                    log.exception("falha ao tratar WM_DEVICECHANGE")
                return 1
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(procedimento)  # precisa continuar referenciado

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p)]
        classe = WNDCLASSW(lpfnWndProc=self._proc, lpszClassName="MonitorLentidaoDispositivos",
                           hInstance=ctypes.windll.kernel32.GetModuleHandleW(None))
        user32.RegisterClassW(ctypes.byref(classe))
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        # Janela de nível superior (nunca mostrada): janelas só de mensagem não recebem os avisos de unidade.
        hwnd = user32.CreateWindowExW(0x80, classe.lpszClassName, "Monitor de Lentidão - dispositivos", 0x80000000,
                                      0, 0, 0, 0, None, None, classe.hInstance, None)
        if not hwnd:
            log.warning("não foi possível criar a janela de dispositivos")
            return
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _mudou(self, evento: int, lparam: int) -> None:
        CHEGOU, SAIU, NOS_MUDARAM, VOLUME = 0x8000, 0x8004, 0x0007, 2
        if evento in (CHEGOU, SAIU) and lparam:
            campos = ctypes.cast(lparam, ctypes.POINTER(ctypes.c_uint32))
            if campos[1] == VOLUME:
                mascara = campos[3]
                letras = [f"{chr(65 + i)}:" for i in range(26) if mascara & (1 << i)]
                self._ultimo_volume = time.time()
                threading.Thread(target=self._volumes, args=(letras, evento == CHEGOU), daemon=True).start()
        elif evento == NOS_MUDARAM:
            # chega várias vezes seguidas por aparelho: espera assentar e compara com a lista anterior
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(1.5, self._comparar_usb)
            self._timer.daemon = True
            self._timer.start()

    def _volumes(self, letras: list[str], chegou: bool) -> None:
        if chegou:
            time.sleep(0.8)  # o sistema de arquivos leva um instante para montar
        for l in letras:
            if chegou:
                rotulo, removivel, total, livre = _info_unidade(l)
                titulo = "Pendrive conectado" if removivel else "Unidade conectada"
                texto = f"{l} {rotulo}".strip() + (f" · {_gb(livre)} livres de {_gb(total)}" if total else "")
                self._letras[l] = f"{l} {rotulo}".strip()
            else:
                titulo = "Unidade removida"
                texto = f"{self._letras.pop(l, l)} pode ser desconectada."
            _registrar(titulo, texto)
            self._avisar(titulo, texto)

    def _comparar_usb(self) -> None:
        try:
            agora = usb_presentes()
        except Exception:
            log.exception("falha ao listar USB")
            return
        novos = [d for k, d in agora.items() if k not in self._presentes]
        saiu = [d for k, d in self._presentes.items() if k not in agora]
        self._presentes = agora
        if time.time() - self._ultimo_volume < 4:
            return  # pendrive já gerou o aviso de unidade: não repete com o nome genérico
        novos = [d for d in novos if not d["armazenamento"]]
        saiu = [d for d in saiu if not d["armazenamento"]]

        def melhor(lista):
            return next((d for d in lista if d["fabricante"]), None) or (lista[0] if lista else None)
        if d := melhor(novos):
            titulo = "Dispositivo USB conectado"
            texto = d["nome"] + (f" · {d['fabricante']}" if d["fabricante"] and d["fabricante"].lower() not in d["nome"].lower() else "")
        elif d := melhor(saiu):
            titulo, texto = "Dispositivo USB desconectado", d["nome"]
        else:
            return
        _registrar(titulo, texto)
        self._avisar(titulo, texto)
