"""Overlay sobre o jogo (como o do Afterburner/RTSS): FPS, tempo de quadro, 1% low e sensores num canto da tela.

É uma janela Win32 desenhada com GDI, sempre no topo, que ignora cliques e nunca rouba o foco. Aparece sobre
jogos em tela cheia "sem bordas" e na maioria dos DX11/12 em tela cheia (que o Windows apresenta via flip); não
aparece em tela cheia exclusiva antiga. Tudo roda num único fio que fica parado em GetMessage quando não há nada
a fazer: o atalho Ctrl+Shift+O liga e desliga o overlay de qualquer lugar.
Adaptado do ResetExplode (OverlayWindow, OverlayMetrics e MainWindow.Overlay; licença MIT, © 2026 Gustavo).
"""

import ctypes
import logging
import sys
import threading
import time

import psutil

from . import config, gpu, quadros, sensores

log = logging.getLogger(__name__)
EH_WINDOWS = sys.platform == "win32"

# (chave, grupo, rótulo, ligado por padrão)
ITENS = [
    ("fps", "FPS", "FPS", True),
    ("frametime", "FPS", "Tempo de quadro (ms)", True),
    ("low1", "FPS", "1% low", True),
    ("grafico", "FPS", "Gráfico do tempo de quadro", True),
    ("gpu_carga", "GPU", "Uso", True),
    ("gpu_temp", "GPU", "Temperatura", True),
    ("gpu_hotspot", "GPU", "Hotspot", False),
    ("gpu_clock", "GPU", "Clock do núcleo", True),
    ("gpu_clockmem", "GPU", "Clock da memória", False),
    ("gpu_watts", "GPU", "Consumo", True),
    ("gpu_volt", "GPU", "Tensão do núcleo", False),
    ("gpu_fan", "GPU", "Ventoinha", False),
    ("gpu_limite", "GPU", "Limitador (NVIDIA)", False),
    ("vram_usada", "VRAM", "Usada (GB)", True),
    ("vram_pct", "VRAM", "Uso (%)", False),
    ("vram_temp", "VRAM", "Temperatura", False),
    ("cpu_carga", "CPU", "Uso total", True),
    ("cpu_nucleo", "CPU", "Núcleo mais ocupado", False),
    ("cpu_temp", "CPU", "Temperatura", True),
    ("cpu_clock", "CPU", "Clock médio", True),
    ("cpu_clockmax", "CPU", "Clock máximo (núcleo)", False),
    ("cpu_watts", "CPU", "Consumo", True),
    ("ram_usada", "RAM", "Usada (GB)", True),
    ("ram_pct", "RAM", "Uso (%)", False),
    ("jogo", "Outros", "Nome do jogo", True),
    ("hora", "Outros", "Hora", False),
]
CHAVES = [i[0] for i in ITENS]
PADRAO = [i[0] for i in ITENS if i[3]]
PRESETS = {"padrao": PADRAO, "tudo": CHAVES, "minimo": ["fps", "frametime", "low1"]}
CANTOS = ("TL", "TR", "BL", "BR")
ESCALAS = (0.8, 1.0, 1.25, 1.5)


# ------------------------------------------------------------------ montagem do texto (testável fora do Windows)
def _achar(linhas, hw, tipo, filtro):
    for l in linhas:
        if (hw is None or l[1] == hw) and l[3] == tipo and filtro(l[4]) and l[5] is not None:
            return l[5]
    return None


def montar(mostrar: set, f: dict | None, linhas: list, resumo: dict, nv: dict | None = None) -> dict:
    """O que desenhar: FPS (ou "—"), tempos do gráfico e as linhas GPU/VRAM/CPU/RAM só com o que foi escolhido."""
    nv = nv or {}
    g = resumo.get("gpu_nome")

    def t(v):
        return f"{v:.0f} °C" if v is not None else "—"

    def p(v):
        return f"{v:.0f}%" if v is not None else "—"

    saida = {"mostrar_fps": "fps" in mostrar, "mostrar_ft": "frametime" in mostrar, "mostrar_low": "low1" in mostrar,
             "grafico": None, "linhas": [], "rodape": ""}
    if f:
        saida.update(fps=f"{f['fps']:.0f}", ft=f"{f['frametime']:.1f} ms", low=f"1% low {f['low1']:.0f}")
    else:
        saida.update(fps="—", ft="— ms", low="aguardando jogo")
    if "grafico" in mostrar:
        saida["grafico"] = list(f["tempos"]) if f else []

    gpu_l = []
    if "gpu_carga" in mostrar:
        gpu_l.append(p(resumo.get("gpu_carga")))
    if "gpu_temp" in mostrar:
        gpu_l.append(t(resumo.get("gpu_temp")))
    if "gpu_hotspot" in mostrar:
        gpu_l.append("HS " + t(_achar(linhas, g, "Temperature", lambda n: n == "GPU Hot Spot")))
    if "gpu_clock" in mostrar:
        c = nv.get("clock") or resumo.get("gpu_clock")
        gpu_l.append(f"{c:.0f} MHz" if c else "— MHz")
    if "gpu_clockmem" in mostrar:
        c = nv.get("clock_mem") or _achar(linhas, g, "Clock", lambda n: n == "GPU Memory")
        gpu_l.append(f"mem {c:.0f} MHz" if c else "mem —")
    if "gpu_watts" in mostrar:
        w = resumo.get("gpu_watts")
        gpu_l.append(f"{w:.0f} W" if w else "— W")
    if "gpu_volt" in mostrar:
        v = _achar(linhas, g, "Voltage", lambda n: n == "GPU Core")
        gpu_l.append(f"{v:.3f} V" if v else "— V")
    if "gpu_fan" in mostrar:
        rpm = _achar(linhas, g, "Fan", lambda n: n.startswith("GPU Fan"))
        pct = _achar(linhas, g, "Control", lambda n: n.startswith("GPU Fan"))
        gpu_l.append(f"{rpm:.0f} RPM" if rpm else f"fan {pct:.0f}%" if pct is not None else "fan —")
    if "gpu_limite" in mostrar and nv.get("limitador"):
        gpu_l.append(nv["limitador"])
    if gpu_l:
        saida["linhas"].append(("GPU", "  ".join(gpu_l)))

    vram_l = []
    usada, total = resumo.get("vram_usada"), resumo.get("vram_total")  # MB
    if "vram_usada" in mostrar:
        vram_l.append(f"{usada / 1024:.1f} GB" if usada else "—")
    if "vram_pct" in mostrar:
        vram_l.append(p(100 * usada / total if usada and total else None))
    if "vram_temp" in mostrar:
        vram_l.append(t(_achar(linhas, g, "Temperature", lambda n: n.startswith("GPU Memory"))))
    if vram_l:
        saida["linhas"].append(("VRAM", "  ".join(vram_l)))

    cpu_l = []
    nucleos = [l[5] for l in linhas if l[2] == "Cpu" and l[3] == "Clock" and l[4].startswith("Core #")
               and "Effective" not in l[4] and (l[5] or 0) > 0]
    if "cpu_carga" in mostrar:
        cpu_l.append(p(resumo.get("cpu_carga")))
    if "cpu_nucleo" in mostrar:
        cargas = [l[5] for l in linhas if l[2] == "Cpu" and l[3] == "Load" and l[4].startswith("CPU Core #")
                  and l[5] is not None]
        if not cargas:
            try:
                cargas = psutil.cpu_percent(percpu=True)
            except Exception:
                cargas = []
        cpu_l.append(f"núcleo {max(cargas):.0f}%" if cargas else "núcleo —")
    if "cpu_temp" in mostrar:
        cpu_l.append(t(resumo.get("cpu_temp")))
    if "cpu_clock" in mostrar:
        cpu_l.append(f"{sum(nucleos) / len(nucleos) / 1000:.2f} GHz" if nucleos else "— GHz")
    if "cpu_clockmax" in mostrar:
        cpu_l.append(f"pico {max(nucleos) / 1000:.2f} GHz" if nucleos else "pico —")
    if "cpu_watts" in mostrar:
        w = resumo.get("cpu_watts")
        cpu_l.append(f"{w:.0f} W" if w else "— W")  # sem leitura sem administrador
    if cpu_l:
        saida["linhas"].append(("CPU", "  ".join(cpu_l)))

    ram_l = []
    vm = psutil.virtual_memory()
    if "ram_usada" in mostrar:
        ram_l.append(f"{(vm.total - vm.available) / 2**30:.1f} GB")
    if "ram_pct" in mostrar:
        ram_l.append(p(vm.percent))
    if ram_l:
        saida["linhas"].append(("RAM", "  ".join(ram_l)))

    rodape = []
    if "jogo" in mostrar and f:
        rodape.append(f["jogo"].removesuffix(".exe"))
    if "hora" in mostrar:
        rodape.append(time.strftime("%H:%M"))
    saida["rodape"] = "  ·  ".join(rodape)
    return saida


# ------------------------------------------------------------------ janela Win32
WM_DESTROY, WM_PAINT, WM_ERASEBKGND, WM_TIMER, WM_HOTKEY, WM_QUIT = 0x0002, 0x000F, 0x0014, 0x0113, 0x0312, 0x0012
WM_NCHITTEST, HTTRANSPARENT = 0x0084, -1
WM_APP_LIGAR, WM_APP_DESLIGAR, WM_APP_OPCOES = 0x8001, 0x8002, 0x8003
WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x8, 0x20, 0x80, 0x80000, 0x8000000
SWP_NOACTIVATE, SWP_NOOWNERZORDER, SWP_SHOWWINDOW = 0x10, 0x200, 0x40
HWND_TOPMOST = -1
LWA_ALPHA = 0x2
MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x2, 0x4, 0x4000
EVENT_SYSTEM_FOREGROUND, WINEVENT_OUTOFCONTEXT = 0x0003, 0
MONITOR_DEFAULTTOPRIMARY, MONITOR_DEFAULTTONEAREST = 1, 2
ID_ATALHO, ID_TIMER = 0x4453, 1
INTERVALO_MS = 500
OPACIDADE = 222  # de 255


def _rgb(r, g, b):
    return r | (g << 8) | (b << 16)


BRANCO, CINZA, LINHA, FUNDO = _rgb(245, 245, 245), _rgb(150, 150, 150), _rgb(70, 70, 70), _rgb(0, 0, 0)


class _Win32:
    """Protótipos das funções usadas (cópias próprias das DLLs para não mexer nas usadas por outros módulos)."""

    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        L = ctypes.c_ssize_t
        self.WNDPROC = ctypes.WINFUNCTYPE(L, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.WINEVENTPROC = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                        ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                        ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]

        class PAINTSTRUCT(ctypes.Structure):
            _fields_ = [("hdc", w.HDC), ("fErase", w.BOOL), ("rcPaint", w.RECT), ("fRestore", w.BOOL),
                        ("fIncUpdate", w.BOOL), ("rgbReserved", ctypes.c_byte * 32)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", w.DWORD), ("rcMonitor", w.RECT), ("rcWork", w.RECT), ("dwFlags", w.DWORD)]

        self.WNDCLASSEXW, self.PAINTSTRUCT, self.MONITORINFO = WNDCLASSEXW, PAINTSTRUCT, MONITORINFO
        u = self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        g = self.gdi32 = ctypes.WinDLL("gdi32")
        k = self.kernel32 = ctypes.WinDLL("kernel32")
        P = ctypes.POINTER

        def proto(f, res, *args):
            f.restype, f.argtypes = res, list(args)

        proto(k.GetCurrentThreadId, w.DWORD)
        proto(k.GetModuleHandleW, w.HMODULE, w.LPCWSTR)
        proto(u.RegisterClassExW, w.ATOM, P(WNDCLASSEXW))
        proto(u.CreateWindowExW, w.HWND, w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
              ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID)
        proto(u.DefWindowProcW, L, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        proto(u.DestroyWindow, w.BOOL, w.HWND)
        proto(u.ShowWindow, w.BOOL, w.HWND, ctypes.c_int)
        proto(u.SetWindowPos, w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)
        proto(u.SetLayeredWindowAttributes, w.BOOL, w.HWND, w.COLORREF, w.BYTE, w.DWORD)
        proto(u.SetTimer, ctypes.c_size_t, w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID)
        proto(u.KillTimer, w.BOOL, w.HWND, ctypes.c_size_t)
        proto(u.InvalidateRect, w.BOOL, w.HWND, w.LPVOID, w.BOOL)
        proto(u.BeginPaint, w.HDC, w.HWND, P(PAINTSTRUCT))
        proto(u.EndPaint, w.BOOL, w.HWND, P(PAINTSTRUCT))
        proto(u.GetMessageW, w.BOOL, P(w.MSG), w.HWND, w.UINT, w.UINT)
        proto(u.PeekMessageW, w.BOOL, P(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT)
        proto(u.TranslateMessage, w.BOOL, P(w.MSG))
        proto(u.DispatchMessageW, L, P(w.MSG))
        proto(u.PostThreadMessageW, w.BOOL, w.DWORD, w.UINT, w.WPARAM, w.LPARAM)
        proto(u.RegisterHotKey, w.BOOL, w.HWND, ctypes.c_int, w.UINT, w.UINT)
        proto(u.UnregisterHotKey, w.BOOL, w.HWND, ctypes.c_int)
        proto(u.GetForegroundWindow, w.HWND)
        proto(u.GetWindowThreadProcessId, w.DWORD, w.HWND, P(w.DWORD))
        proto(u.MonitorFromWindow, w.HANDLE, w.HWND, w.DWORD)
        proto(u.MonitorFromPoint, w.HANDLE, w.POINT, w.DWORD)
        proto(u.GetMonitorInfoW, w.BOOL, w.HANDLE, P(MONITORINFO))
        proto(u.SetWinEventHook, w.HANDLE, w.DWORD, w.DWORD, w.HMODULE, self.WINEVENTPROC, w.DWORD, w.DWORD, w.DWORD)
        proto(u.UnhookWinEvent, w.BOOL, w.HANDLE)
        proto(u.FillRect, ctypes.c_int, w.HDC, P(w.RECT), w.HBRUSH)
        try:
            proto(u.GetDpiForSystem, w.UINT)
        except AttributeError:
            pass
        proto(g.CreateFontW, w.HFONT, *([ctypes.c_int] * 5), *([w.DWORD] * 8), w.LPCWSTR)
        proto(g.GetTextFaceW, ctypes.c_int, w.HDC, ctypes.c_int, w.LPWSTR)
        proto(g.SelectObject, w.HGDIOBJ, w.HDC, w.HGDIOBJ)
        proto(g.DeleteObject, w.BOOL, w.HGDIOBJ)
        proto(g.SetTextColor, w.COLORREF, w.HDC, w.COLORREF)
        proto(g.SetBkMode, ctypes.c_int, w.HDC, ctypes.c_int)
        proto(g.TextOutW, w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPCWSTR, ctypes.c_int)
        proto(g.GetTextExtentPoint32W, w.BOOL, w.HDC, w.LPCWSTR, ctypes.c_int, P(w.SIZE))
        proto(g.CreateSolidBrush, w.HBRUSH, w.COLORREF)
        proto(g.CreatePen, w.HPEN, ctypes.c_int, ctypes.c_int, w.COLORREF)
        proto(g.Polyline, w.BOOL, w.HDC, P(w.POINT), ctypes.c_int)
        proto(g.MoveToEx, w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int, w.LPVOID)
        proto(g.LineTo, w.BOOL, w.HDC, ctypes.c_int, ctypes.c_int)
        proto(g.CreateCompatibleDC, w.HDC, w.HDC)
        proto(g.CreateCompatibleBitmap, w.HBITMAP, w.HDC, ctypes.c_int, ctypes.c_int)
        proto(g.BitBlt, w.BOOL, w.HDC, *([ctypes.c_int] * 4), w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD)
        proto(g.DeleteDC, w.BOOL, w.HDC)


class JanelaOverlay:
    """Um fio com fila de mensagens: atalho global e, quando ligado, a janela do overlay."""

    def __init__(self, fonte, ao_atalho):
        self._fonte = fonte  # () -> dict de montar()
        self._ao_atalho = ao_atalho  # chamado (fora deste fio) quando Ctrl+Shift+O é apertado
        self._tid = 0
        self._pronto = threading.Event()
        self._fio: threading.Thread | None = None
        self._hwnd = None
        self._gancho = None
        self._monitor = None
        self._fontes: dict[str, int] = {}
        self._dados: dict | None = None
        self._tam = (0, 0)
        self.canto, self.escala = "TL", 1.0
        self.erro_atalho: str | None = None
        self.erro: str | None = None

    @property
    def visivel(self) -> bool:
        return self._hwnd is not None

    def iniciar(self) -> None:
        if not EH_WINDOWS or (self._fio and self._fio.is_alive()):
            return
        self._fio = threading.Thread(target=self._rodar, daemon=True, name="overlay")
        self._fio.start()
        self._pronto.wait(3)

    def _postar(self, msg: int) -> None:
        if self._tid:
            self._w.user32.PostThreadMessageW(self._tid, msg, 0, 0)

    def mostrar(self, sim: bool) -> None:
        self.iniciar()
        self._postar(WM_APP_LIGAR if sim else WM_APP_DESLIGAR)

    def opcoes(self, canto: str, escala: float) -> None:
        self.canto = canto if canto in CANTOS else "TL"
        self.escala = escala if escala in ESCALAS else 1.0
        self._postar(WM_APP_OPCOES)

    def encerrar(self) -> None:
        self._postar(WM_QUIT)

    # ------------------------------------------------------------ fio
    def _rodar(self) -> None:
        try:
            self._w = w32 = _Win32()
        except Exception as e:
            log.exception("overlay indisponível")
            self.erro = str(e)
            self._pronto.set()
            return
        u, wt = w32.user32, w32.w
        msg = wt.MSG()
        u.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # cria a fila deste fio
        self._tid = w32.kernel32.GetCurrentThreadId()
        if not u.RegisterHotKey(None, ID_ATALHO, MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT, ord("O")):
            self.erro_atalho = "Outro programa já usa Ctrl+Shift+O; ligue o overlay pelo painel ou pelo menu da bandeja."
        self._proc = w32.WNDPROC(self._wndproc)
        self._proc_evento = w32.WINEVENTPROC(self._evento_primeiro_plano)
        classe = w32.WNDCLASSEXW(cbSize=ctypes.sizeof(w32.WNDCLASSEXW), lpfnWndProc=self._proc,
                                 hInstance=w32.kernel32.GetModuleHandleW(None), lpszClassName="DestiniOverlay")
        u.RegisterClassExW(ctypes.byref(classe))
        self._pronto.set()
        try:
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if not msg.hWnd:
                    if msg.message == WM_HOTKEY and msg.wParam == ID_ATALHO:
                        threading.Thread(target=self._ao_atalho, daemon=True).start()
                        continue
                    if msg.message == WM_APP_LIGAR:
                        self._criar()
                        continue
                    if msg.message == WM_APP_DESLIGAR:
                        if self._hwnd:
                            u.DestroyWindow(self._hwnd)
                        continue
                    if msg.message == WM_APP_OPCOES:
                        self._fontes_limpar()
                        self._atualizar()
                        continue
                u.TranslateMessage(ctypes.byref(msg))
                u.DispatchMessageW(ctypes.byref(msg))
        finally:
            if self._hwnd:
                u.DestroyWindow(self._hwnd)
            u.UnregisterHotKey(None, ID_ATALHO)

    def _criar(self) -> None:
        if self._hwnd:
            return
        w32 = self._w
        u = w32.user32
        estilo = WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        hwnd = u.CreateWindowExW(estilo, "DestiniOverlay", "Destini overlay", WS_POPUP, 0, 0, 10, 10,
                                 None, None, w32.kernel32.GetModuleHandleW(None), None)
        if not hwnd:
            self.erro = f"o Windows não criou a janela do overlay (erro {ctypes.get_last_error()})"
            return
        self._hwnd = hwnd
        self.erro = None
        u.SetLayeredWindowAttributes(hwnd, 0, OPACIDADE, LWA_ALPHA)
        self._gancho = u.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None, self._proc_evento,
                                         0, 0, WINEVENT_OUTOFCONTEXT)
        self._seguir_jogo(u.GetForegroundWindow())
        u.SetTimer(hwnd, ID_TIMER, INTERVALO_MS, None)
        self._atualizar()
        u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE

    def _wndproc(self, hwnd, msg, wparam, lparam):
        u = self._w.user32
        try:
            if msg == WM_TIMER:
                self._atualizar()
                return 0
            if msg == WM_PAINT:
                self._pintar(hwnd)
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_DESTROY:
                u.KillTimer(hwnd, ID_TIMER)
                if self._gancho:
                    u.UnhookWinEvent(self._gancho)
                    self._gancho = None
                self._fontes_limpar()
                self._hwnd = None
                return 0
        except Exception:
            log.exception("falha no overlay")
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _evento_primeiro_plano(self, _gancho, _evento, hwnd, *_):
        try:
            self._seguir_jogo(hwnd)
            self._posicionar()
        except Exception:
            pass

    def _seguir_jogo(self, hwnd) -> None:
        """Acompanha o monitor da janela ativa, a não ser que seja do próprio Destini."""
        if not hwnd:
            return
        import os
        pid = self._w.w.DWORD()
        self._w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != os.getpid():
            self._monitor = self._w.user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

    # ------------------------------------------------------------ desenho
    def _fator(self) -> float:
        dpi = 96
        try:
            dpi = self._w.user32.GetDpiForSystem() or 96
        except AttributeError:
            pass
        return dpi / 96 * self.escala

    def _fonte_gdi(self, nome: str, altura: float, negrito: bool = False) -> int:
        chave = f"{nome}{altura:.0f}{negrito}"
        f = self._fontes.get(chave)
        if not f:
            g = self._w.gdi32
            f = None
            for face in ("Cascadia Mono", "Consolas"):
                f = g.CreateFontW(-int(altura), 0, 0, 0, 700 if negrito else 400, 0, 0, 0, 1, 0, 0, 4, 0, face)
                if self._face_ok(f, face):
                    break
                g.DeleteObject(f)
                f = None
            if not f:
                f = g.CreateFontW(-int(altura), 0, 0, 0, 700 if negrito else 400, 0, 0, 0, 1, 0, 0, 4, 0, "Consolas")
            self._fontes[chave] = f
        return f

    def _face_ok(self, fonte, face: str) -> bool:
        g = self._w.gdi32
        dc = g.CreateCompatibleDC(None)
        antiga = g.SelectObject(dc, fonte)
        buf = ctypes.create_unicode_buffer(64)
        g.GetTextFaceW(dc, 64, buf)
        g.SelectObject(dc, antiga)
        g.DeleteDC(dc)
        return buf.value.lower() == face.lower()

    def _fontes_limpar(self) -> None:
        for f in self._fontes.values():
            self._w.gdi32.DeleteObject(f)
        self._fontes.clear()

    def _atualizar(self) -> None:
        if not self._hwnd:
            return
        try:
            self._dados = self._fonte()
        except Exception:
            log.exception("falha ao montar o overlay")
            return
        self._tam = self._medir(self._dados)
        self._posicionar()
        self._w.user32.InvalidateRect(self._hwnd, None, False)

    def _posicionar(self) -> None:
        """Cola no canto escolhido do monitor do jogo e volta para o topo (jogos sem borda também se põem no topo)."""
        if not self._hwnd:
            return
        u, w32 = self._w.user32, self._w
        if not self._monitor:
            self._monitor = u.MonitorFromPoint(w32.w.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
        mi = w32.MONITORINFO(cbSize=ctypes.sizeof(w32.MONITORINFO))
        if not u.GetMonitorInfoW(self._monitor, ctypes.byref(mi)):
            return
        r = mi.rcMonitor
        larg, alt = self._tam
        m = int(12 * self._fator())
        x = r.right - larg - m if self.canto.endswith("R") else r.left + m
        y = r.bottom - alt - m if self.canto.startswith("B") else r.top + m
        u.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, larg, alt, SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_SHOWWINDOW)

    def _layout(self, d: dict, medir):
        """Lista de (x, y, texto, fonte, cor) e o tamanho total. medir(texto, fonte) -> (larg, alt)."""
        s = self._fator()
        pad = int(10 * s)
        grande, pequena, normal = ("Cascadia Mono", 30 * s, True), ("Cascadia Mono", 12 * s, False), ("Cascadia Mono", 13 * s, False)
        itens, y, largura = [], pad, int(200 * s)
        if d["mostrar_fps"] or d["mostrar_ft"]:
            x = pad
            alt_linha = 0
            if d["mostrar_fps"]:
                lw, lh = medir(d["fps"], grande)
                itens.append((x, y, d["fps"], grande, BRANCO))
                x += lw + int(4 * s)
                uw, uh = medir("FPS", pequena)
                itens.append((x, y + lh - uh - int(5 * s), "FPS", pequena, CINZA))
                x += uw + int(12 * s)
                alt_linha = lh
            if d["mostrar_ft"]:
                fw, fh = medir(d["ft"], pequena)
                yy = y + (alt_linha - fh - int(5 * s) if alt_linha else 0)
                itens.append((x, yy, d["ft"], pequena, CINZA))
                x += fw
                alt_linha = alt_linha or fh
            largura = max(largura, x + pad)
            y += alt_linha
        if d["mostrar_low"]:
            lw, lh = medir(d["low"], pequena)
            itens.append((pad, y, d["low"], pequena, CINZA))
            largura = max(largura, lw + 2 * pad)
            y += lh + int(4 * s)
        grafico = None
        if d["grafico"] is not None:
            gh = int(36 * s)
            grafico = (pad, y + int(2 * s), int(200 * s), gh)
            largura = max(largura, int(200 * s) + 2 * pad)
            y += gh + int(8 * s)
        rot_w = max([medir(r, pequena)[0] for r, _ in d["linhas"]] + [0]) + int(10 * s)
        for rotulo, valor in d["linhas"]:
            vw, vh = medir(valor, normal)
            itens.append((pad, y + int(1 * s), rotulo, pequena, CINZA))
            itens.append((pad + rot_w, y, valor, normal, BRANCO))
            largura = max(largura, pad + rot_w + vw + pad)
            y += vh + int(3 * s)
        if d["rodape"]:
            rw, rh = medir(d["rodape"], pequena)
            itens.append((pad, y + int(2 * s), d["rodape"], pequena, CINZA))
            largura = max(largura, rw + 2 * pad)
            y += rh + int(2 * s)
        if grafico:
            grafico = (grafico[0], grafico[1], largura - 2 * pad, grafico[3])
        return itens, grafico, (largura, y + pad)

    def _medidor(self, dc):
        g, w = self._w.gdi32, self._w.w

        def medir(texto, fonte):
            f = self._fonte_gdi(*fonte)
            antiga = g.SelectObject(dc, f)
            tam = w.SIZE()
            g.GetTextExtentPoint32W(dc, texto, len(texto), ctypes.byref(tam))
            g.SelectObject(dc, antiga)
            return tam.cx, tam.cy
        return medir

    def _medir(self, d: dict) -> tuple[int, int]:
        g = self._w.gdi32
        dc = g.CreateCompatibleDC(None)
        try:
            _, _, tam = self._layout(d, self._medidor(dc))
        finally:
            g.DeleteDC(dc)
        return tam

    def _pintar(self, hwnd) -> None:
        w32 = self._w
        u, g, w = w32.user32, w32.gdi32, w32.w
        ps = w32.PAINTSTRUCT()
        hdc = u.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            d = self._dados
            if not d:
                return
            larg, alt = self._tam
            mem = g.CreateCompatibleDC(hdc)
            bmp = g.CreateCompatibleBitmap(hdc, larg, alt)
            antigo_bmp = g.SelectObject(mem, bmp)
            fundo = g.CreateSolidBrush(FUNDO)
            u.FillRect(mem, ctypes.byref(w.RECT(0, 0, larg, alt)), fundo)
            g.DeleteObject(fundo)
            g.SetBkMode(mem, 1)  # TRANSPARENT
            itens, grafico, _ = self._layout(d, self._medidor(mem))
            for x, y, texto, fonte, cor in itens:
                antiga = g.SelectObject(mem, self._fonte_gdi(*fonte))
                g.SetTextColor(mem, cor)
                g.TextOutW(mem, x, y, texto, len(texto))
                g.SelectObject(mem, antiga)
            if grafico:
                self._grafico(mem, grafico, d["grafico"])
            g.BitBlt(hdc, 0, 0, larg, alt, mem, 0, 0, 0x00CC0020)  # SRCCOPY
            g.SelectObject(mem, antigo_bmp)
            g.DeleteObject(bmp)
            g.DeleteDC(mem)
        finally:
            u.EndPaint(hwnd, ctypes.byref(ps))

    def _grafico(self, dc, area, tempos) -> None:
        """Tempos de quadro recentes: picos para cima são engasgos."""
        g, w = self._w.gdi32, self._w.w
        x0, y0, larg, alt = area
        caneta = g.CreatePen(0, 1, LINHA)
        antiga = g.SelectObject(dc, caneta)
        g.MoveToEx(dc, x0, y0 + alt - 1, None)
        g.LineTo(dc, x0 + larg, y0 + alt - 1)
        g.SelectObject(dc, antiga)
        g.DeleteObject(caneta)
        if len(tempos) < 2:
            return
        topo = max(max(tempos), sum(tempos) / len(tempos) * 2)  # mostra os picos sem achatar o normal
        passo = larg / (len(tempos) - 1)
        pontos = (w.POINT * len(tempos))(*(w.POINT(int(x0 + i * passo), int(y0 + alt - 2 - (alt - 3) * min(1.0, t / topo)))
                                            for i, t in enumerate(tempos)))
        caneta = g.CreatePen(0, max(1, int(self._fator())), BRANCO)
        antiga = g.SelectObject(dc, caneta)
        g.Polyline(dc, pontos, len(tempos))
        g.SelectObject(dc, antiga)
        g.DeleteObject(caneta)


# ------------------------------------------------------------------ controle (painel, bandeja e atalho)
class Controle:
    def __init__(self):
        self._janela = JanelaOverlay(self._dados, self._atalho)
        self.ligado = False
        self._trava = threading.Lock()
        self._ouvintes: list = []

    def ao_mudar(self, funcao) -> None:
        """funcao(ligado) é chamada quando o overlay liga/desliga (ex.: menu da bandeja)."""
        self._ouvintes.append(funcao)

    def iniciar(self) -> None:
        """Na abertura do monitor: registra o atalho Ctrl+Shift+O (o fio fica parado até ser usado)."""
        if config.preferencias().get("overlay_atalho", True):
            self._janela.iniciar()
        self._aplicar_opcoes()

    def _aplicar_opcoes(self) -> None:
        pref = config.preferencias()
        self._janela.canto = pref.get("overlay_canto", "TL")
        self._janela.escala = float(pref.get("overlay_escala", 1.0))

    def itens(self) -> list[str]:
        salvos = config.preferencias().get("overlay_itens")
        return [c for c in CHAVES if c in salvos] if isinstance(salvos, list) else list(PADRAO)

    def _atalho(self) -> None:
        self.ligar(not self.ligado)

    def ligar(self, sim: bool) -> None:
        with self._trava:
            if sim == self.ligado:
                return
            self.ligado = sim
            sensores.instancia().reter("overlay", sim)
            quadros.instancia().reter("overlay", sim)
            self._aplicar_opcoes()
            self._janela.mostrar(sim)
        for f in self._ouvintes:
            try:
                f(sim)
            except Exception:
                log.exception("falha ao avisar mudança do overlay")

    def definir(self, chave: str, valor) -> None:
        if chave == "canto" and valor in CANTOS:
            config.definir_preferencia("overlay_canto", valor)
        elif chave == "escala" and float(valor) in ESCALAS:
            config.definir_preferencia("overlay_escala", float(valor))
        elif chave == "itens" and isinstance(valor, list):
            config.definir_preferencia("overlay_itens", [c for c in CHAVES if c in valor])
        elif chave == "preset" and valor in PRESETS:
            config.definir_preferencia("overlay_itens", list(PRESETS[valor]))
        self._aplicar_opcoes()
        self._janela.opcoes(self._janela.canto, self._janela.escala)

    def _dados(self) -> dict:
        s = sensores.instancia()
        linhas, _ = s.linhas()
        resumo = sensores.Sensores.resumo(linhas)
        mostrar = set(self.itens())
        f = quadros.instancia().primeiro_plano()
        nv = {}
        h = s.placa_nvidia()
        if h and mostrar & {"gpu_clock", "gpu_clockmem", "gpu_limite"}:
            n = gpu.nvml()
            nv = {"clock": n.clock(h, gpu.CLOCK_GRAFICO), "clock_mem": n.clock(h, gpu.CLOCK_MEMORIA)}
            r = n.limitadores(h)
            if r is not None:
                nv["limitador"] = gpu.limitador(r)
        return montar(mostrar, f, linhas, resumo, nv)

    def estado(self) -> dict:
        q = quadros.instancia()
        pref = config.preferencias()
        f = q.primeiro_plano() if q.rodando else None
        desenhando = [{"jogo": x["jogo"], "fps": round(x["fps"]), "low1": round(x["low1"]), "pid": x["pid"]}
                      for x in q.todos()[:8]] if q.rodando else []
        return {"ligado": self.ligado, "erro_fps": q.erro, "contando": q.rodando, "erro": self._janela.erro,
                "erro_atalho": self._janela.erro_atalho,
                "atual": {"jogo": f["jogo"], "fps": round(f["fps"]), "frametime": round(f["frametime"], 1),
                          "low1": round(f["low1"])} if f else None,
                "desenhando": desenhando, "canto": pref.get("overlay_canto", "TL"),
                "escala": float(pref.get("overlay_escala", 1.0)), "itens": self.itens(),
                "catalogo": [{"chave": c, "grupo": g, "rotulo": r} for c, g, r, _ in ITENS]}

    def acompanhar(self, sim: bool) -> None:
        """Página do overlay aberta: conta FPS para mostrar o status ao vivo, mesmo com o overlay desligado."""
        quadros.instancia().reter("pagina", sim)

    def encerrar(self) -> None:
        self._janela.encerrar()
        quadros.instancia().encerrar()


_controle: Controle | None = None


def controle() -> Controle:
    global _controle
    if _controle is None:
        _controle = Controle()
    return _controle
