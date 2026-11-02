"""FPS de qualquer jogo sem injetar nada no jogo, do jeito do PresentMon.

O Windows avisa por ETW (Event Tracing for Windows) cada quadro que um programa entrega pelo DXGI
(DirectX 10/11/12) ou pelo Direct3D 9. O monitor abre uma sessão de rastreamento em tempo real, recebe só
esses eventos e calcula FPS, tempo de quadro e 1% low de cada processo. Jogos Vulkan/OpenGL só aparecem
quando o driver apresenta pelo DXGI (comum nos drivers atuais, mas não garantido).

A sessão só existe enquanto alguém precisa dela (overlay, análise de gargalo ou a página do overlay aberta)
e o Windows só deixa criá-la com o monitor aberto como administrador.
Adaptado do ResetExplode (FrameMonitor.cs; licença MIT, © 2026 Gustavo).
"""

import bisect
import ctypes
import logging
import os
import struct
import sys
import threading
import time
import uuid
from collections import deque

log = logging.getLogger(__name__)

EH_WINDOWS = sys.platform == "win32"
NOME_SESSAO = "Destini-Quadros"
PROVEDOR_DXGI = uuid.UUID("CA11C036-0102-4A2D-A6AD-F03CFED5D3C9")
PROVEDOR_D3D9 = uuid.UUID("783ACA0A-790E-4D7F-8451-AA850511C6B9")
EVENTO_DXGI_PRESENT, EVENTO_D3D9_PRESENT = 42, 1
HISTORICO_MS = 10_000  # janela do 1% low
SEM_QUADROS_S = 2.0  # sem evento há mais que isso: o processo parou de desenhar

# Constantes do evntrace.h / evntprov.h
WNODE_FLAG_TRACED_GUID = 0x00020000
EVENT_TRACE_REAL_TIME_MODE = 0x00000100
EVENT_TRACE_CONTROL_STOP = 1
EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
TRACE_LEVEL_INFORMATION = 4
PROCESS_TRACE_MODE_REAL_TIME = 0x00000100
PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
EVENT_FILTER_TYPE_EVENT_ID = 0x80000200
ENABLE_TRACE_PARAMETERS_VERSION_2 = 2
SESSAO_INVALIDA = 0xFFFFFFFFFFFFFFFF
ERROR_ACCESS_DENIED, ERROR_ALREADY_EXISTS, ERROR_NO_SYSTEM_RESOURCES = 5, 183, 1450

u32, u64, i64 = ctypes.c_uint32, ctypes.c_uint64, ctypes.c_int64


class GUID(ctypes.Structure):
    _fields_ = [("Data1", u32), ("Data2", ctypes.c_uint16), ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def de(cls, u: uuid.UUID) -> "GUID":
        return cls.from_buffer_copy(u.bytes_le)


class WNODE_HEADER(ctypes.Structure):
    _fields_ = [("BufferSize", u32), ("ProviderId", u32), ("HistoricalContext", u64), ("TimeStamp", i64),
                ("Guid", GUID), ("ClientContext", u32), ("Flags", u32)]


class EVENT_TRACE_PROPERTIES(ctypes.Structure):
    _fields_ = [("Wnode", WNODE_HEADER), ("BufferSize", u32), ("MinimumBuffers", u32), ("MaximumBuffers", u32),
                ("MaximumFileSize", u32), ("LogFileMode", u32), ("FlushTimer", u32), ("EnableFlags", u32),
                ("AgeLimit", ctypes.c_int32), ("NumberOfBuffers", u32), ("FreeBuffers", u32), ("EventsLost", u32),
                ("BuffersWritten", u32), ("LogBuffersLost", u32), ("RealTimeBuffersLost", u32),
                ("LoggerThreadId", ctypes.c_void_p), ("LogFileNameOffset", u32), ("LoggerNameOffset", u32)]


class _PropriedadesComNome(ctypes.Structure):
    # O Windows copia o nome da sessão logo depois da estrutura (LoggerNameOffset).
    _fields_ = [("p", EVENT_TRACE_PROPERTIES), ("nome", ctypes.c_uint16 * 256)]


class EVENT_TRACE_LOGFILEW(ctypes.Structure):
    # Só os campos usados têm nome; CurrentEvent (EVENT_TRACE, 88 bytes) e LogfileHeader
    # (TRACE_LOGFILE_HEADER, 280 bytes) ficam como bytes. Tamanho total 448, igual ao do SDK em 64 bits.
    _fields_ = [("LogFileName", ctypes.c_void_p), ("LoggerName", ctypes.c_wchar_p), ("CurrentTime", i64),
                ("BuffersRead", u32), ("ProcessTraceMode", u32), ("CurrentEvent", ctypes.c_ubyte * 88),
                ("LogfileHeader", ctypes.c_ubyte * 280), ("BufferCallback", ctypes.c_void_p), ("BufferSize", u32),
                ("Filled", u32), ("EventsLost", u32), ("EventRecordCallback", ctypes.c_void_p),
                ("IsKernelTrace", u32), ("Context", ctypes.c_void_p)]


class EVENT_FILTER_DESCRIPTOR(ctypes.Structure):
    _fields_ = [("Ptr", u64), ("Size", u32), ("Type", u32)]


class ENABLE_TRACE_PARAMETERS(ctypes.Structure):
    _fields_ = [("Version", u32), ("EnableProperty", u32), ("ControlFlags", u32), ("SourceId", GUID),
                ("EnableFilterDesc", ctypes.POINTER(EVENT_FILTER_DESCRIPTOR)), ("FilterDescCount", u32)]


# EVENT_RECORD começa pelo EVENT_HEADER: ProcessId em 12, TimeStamp em 16, ProviderId em 24, EventDescriptor.Id em 40.
_CABECALHO = struct.Struct("<12xIq16sH")
_TAM_CABECALHO = _CABECALHO.size  # 42

if EH_WINDOWS:
    _CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)
else:  # só para os testes fora do Windows
    _CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_void_p)


def _advapi():
    a = ctypes.WinDLL("advapi32")
    a.StartTraceW.argtypes = [ctypes.POINTER(u64), ctypes.c_wchar_p, ctypes.c_void_p]
    a.StartTraceW.restype = u32
    a.ControlTraceW.argtypes = [u64, ctypes.c_wchar_p, ctypes.c_void_p, u32]
    a.ControlTraceW.restype = u32
    a.EnableTraceEx2.argtypes = [u64, ctypes.POINTER(GUID), u32, ctypes.c_ubyte, u64, u64, u32,
                                 ctypes.POINTER(ENABLE_TRACE_PARAMETERS)]
    a.EnableTraceEx2.restype = u32
    a.OpenTraceW.argtypes = [ctypes.POINTER(EVENT_TRACE_LOGFILEW)]
    a.OpenTraceW.restype = u64
    a.ProcessTrace.argtypes = [ctypes.POINTER(u64), u32, ctypes.c_void_p, ctypes.c_void_p]
    a.ProcessTrace.restype = u32
    a.CloseTrace.argtypes = [u64]
    a.CloseTrace.restype = u32
    return a


def _propriedades() -> _PropriedadesComNome:
    b = _PropriedadesComNome()
    b.p.Wnode.BufferSize = ctypes.sizeof(b)
    b.p.Wnode.Flags = WNODE_FLAG_TRACED_GUID
    b.p.Wnode.ClientContext = 1  # relógio QPC (precisão de microssegundos)
    b.p.Wnode.Guid = GUID.de(uuid.uuid4())
    b.p.LogFileMode = EVENT_TRACE_REAL_TIME_MODE
    b.p.BufferSize = 16  # KB: poucos eventos por segundo, buffers pequenos bastam
    b.p.MinimumBuffers = 4
    b.p.MaximumBuffers = 16
    b.p.FlushTimer = 1  # entrega o que tiver a cada 1 s, mesmo com buffer meio vazio
    b.p.LoggerNameOffset = ctypes.sizeof(EVENT_TRACE_PROPERTIES)
    return b


def _filtro_eventos(ids: tuple[int, ...]):
    """EVENT_FILTER_EVENT_ID {BOOLEAN FilterIn; UCHAR Reserved; USHORT Count; USHORT Events[]}: só esses eventos."""
    dados = struct.pack(f"<BBH{len(ids)}H", 1, 0, len(ids), *ids)
    buf = ctypes.create_string_buffer(dados, len(dados))
    desc = EVENT_FILTER_DESCRIPTOR(ctypes.addressof(buf), len(dados), EVENT_FILTER_TYPE_EVENT_ID)
    params = ENABLE_TRACE_PARAMETERS(Version=ENABLE_TRACE_PARAMETERS_VERSION_2,
                                     EnableFilterDesc=ctypes.pointer(desc), FilterDescCount=1)
    return params, (buf, desc)  # o segundo item mantém a memória viva durante a chamada


def calcular(ts: list[float]) -> dict | None:
    """FPS do último segundo, tempo de quadro e 1% low (percentil 99 do tempo de quadro em 10 s). ts em ms."""
    if len(ts) < 3:
        return None
    ultimo = ts[-1]
    ts = ts[bisect.bisect_left(ts, ultimo - HISTORICO_MS):]
    if len(ts) < 3:
        return None
    tempos = [b - a for a, b in zip(ts, ts[1:])]
    i = bisect.bisect_left(ts, ultimo - 1000)
    intervalo = ultimo - ts[i]
    quadros = len(ts) - 1 - i
    fps = quadros * 1000 / intervalo if intervalo > 0 else 0.0
    ordenados = sorted(tempos)
    p99 = ordenados[min(len(ordenados) - 1, int(len(ordenados) * 0.99))]
    return {"fps": fps, "frametime": 1000 / fps if fps > 0 else 0.0, "low1": 1000 / p99 if p99 > 0 else 0.0,
            "tempos": [round(t, 2) for t in tempos[-120:]]}


class Quadros:
    def __init__(self):
        self._presentes: dict[int, deque] = {}
        self._visto: dict[int, float] = {}
        self._nomes: dict[int, str] = {}
        self._retencoes: set[str] = set()
        self._trava = threading.Lock()
        self._sessao = 0
        self._rastro = SESSAO_INVALIDA
        self._props: _PropriedadesComNome | None = None
        self._fio: threading.Thread | None = None
        self._callback = _CALLBACK(self._evento)  # guardado: o coletor de lixo não pode levar o callback
        self._dxgi = PROVEDOR_DXGI.bytes_le
        self._d3d9 = PROVEDOR_D3D9.bytes_le
        self.erro: str | None = None
        self.eventos = 0
        self._tentativa = 0.0

    # ------------------------------------------------------------ quem precisa
    def reter(self, quem: str, sim: bool) -> None:
        with self._trava:
            if sim:
                self._retencoes.add(quem)
                # Depois de uma recusa (ex.: sem administrador) só tenta de novo a cada 20 s.
                if not self.rodando and (self.erro is None or time.monotonic() - self._tentativa > 20):
                    self._tentativa = time.monotonic()
                    self._iniciar()
            else:
                self._retencoes.discard(quem)
                if not self._retencoes and self.rodando:
                    self._parar()

    @property
    def rodando(self) -> bool:
        return self._fio is not None and self._fio.is_alive()

    # ------------------------------------------------------------ sessão ETW
    def _iniciar(self) -> None:
        if not EH_WINDOWS:
            self.erro = "só funciona no Windows"
            return
        try:
            a = _advapi()
            props = _propriedades()
            sessao = u64()
            r = a.StartTraceW(ctypes.byref(sessao), NOME_SESSAO, ctypes.byref(props))
            if r == ERROR_ALREADY_EXISTS:
                # Sobrou de uma execução fechada à força: derruba e cria de novo.
                velha = _propriedades()
                a.ControlTraceW(0, NOME_SESSAO, ctypes.byref(velha), EVENT_TRACE_CONTROL_STOP)
                props = _propriedades()
                r = a.StartTraceW(ctypes.byref(sessao), NOME_SESSAO, ctypes.byref(props))
            if r != 0:
                self.erro = self._mensagem(r)
                log.info("sessão de quadros não abriu: erro %s", r)
                return
            self._sessao, self._props = sessao.value, props
            for prov, evento in ((PROVEDOR_DXGI, EVENTO_DXGI_PRESENT), (PROVEDOR_D3D9, EVENTO_D3D9_PRESENT)):
                guid = GUID.de(prov)
                params, _vivo = _filtro_eventos((evento,))
                r = a.EnableTraceEx2(self._sessao, ctypes.byref(guid), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                     TRACE_LEVEL_INFORMATION, 0xFFFFFFFFFFFFFFFF, 0, 0, ctypes.byref(params))
                if r != 0:  # Windows sem filtro por evento: recebe tudo do provedor e filtra aqui
                    r = a.EnableTraceEx2(self._sessao, ctypes.byref(guid), EVENT_CONTROL_CODE_ENABLE_PROVIDER,
                                         TRACE_LEVEL_INFORMATION, 0xFFFFFFFFFFFFFFFF, 0, 0, None)
                if r != 0:
                    log.warning("provedor %s não ligou: erro %s", prov, r)
            arq = EVENT_TRACE_LOGFILEW()
            arq.LoggerName = NOME_SESSAO
            arq.ProcessTraceMode = PROCESS_TRACE_MODE_REAL_TIME | PROCESS_TRACE_MODE_EVENT_RECORD
            arq.EventRecordCallback = ctypes.cast(self._callback, ctypes.c_void_p)
            rastro = a.OpenTraceW(ctypes.byref(arq))
            if rastro == SESSAO_INVALIDA:
                erro = ctypes.get_last_error() or ctypes.GetLastError()
                self._fechar_sessao(a)
                self.erro = self._mensagem(erro)
                return
            self._rastro = rastro
            self._fio = threading.Thread(target=self._processar, args=(a, rastro), daemon=True, name="quadros-etw")
            self._fio.start()
            self.erro = None
            log.info("sessão de quadros aberta")
        except Exception as e:
            log.exception("falha ao abrir a sessão de quadros")
            self.erro = str(e)

    def _processar(self, a, rastro: int) -> None:
        h = u64(rastro)
        r = a.ProcessTrace(ctypes.byref(h), 1, None, None)
        if r not in (0, 1223):  # 1223 = ERROR_CANCELLED (sessão parada)
            self.erro = self._mensagem(r)
            log.info("leitura de quadros terminou: erro %s", r)

    def _fechar_sessao(self, a) -> None:
        if self._sessao and self._props is not None:
            a.ControlTraceW(self._sessao, None, ctypes.byref(self._props), EVENT_TRACE_CONTROL_STOP)
        self._sessao, self._props = 0, None

    def _parar(self) -> None:
        if not EH_WINDOWS:
            return
        try:
            a = _advapi()
            self._fechar_sessao(a)
            if self._rastro != SESSAO_INVALIDA:
                a.CloseTrace(self._rastro)
                self._rastro = SESSAO_INVALIDA
        except Exception:
            log.exception("falha ao fechar a sessão de quadros")
        fio, self._fio = self._fio, None
        if fio:
            fio.join(timeout=3)
        self._presentes.clear()
        self._visto.clear()
        log.info("sessão de quadros fechada")

    def encerrar(self) -> None:
        with self._trava:
            self._retencoes.clear()
            if self.rodando or self._sessao:
                self._parar()

    @staticmethod
    def _mensagem(codigo: int) -> str:
        if codigo == ERROR_ACCESS_DENIED:
            return "O Windows só libera a contagem de FPS para o monitor aberto como administrador."
        if codigo == ERROR_NO_SYSTEM_RESOURCES:
            return "O Windows atingiu o limite de sessões de rastreamento. Feche PresentMon, Afterburner ou CapFrameX e tente de novo."
        return f"O Windows recusou a contagem de FPS (erro {codigo})."

    # ------------------------------------------------------------ eventos (fio do ETW)
    def _evento(self, registro) -> None:
        try:
            pid, ts, provedor, ident = _CABECALHO.unpack(ctypes.string_at(registro, _TAM_CABECALHO))
            if not ((ident == EVENTO_DXGI_PRESENT and provedor == self._dxgi)
                    or (ident == EVENTO_D3D9_PRESENT and provedor == self._d3d9)):
                return
            fila = self._presentes.get(pid)
            if fila is None:
                fila = self._presentes[pid] = deque(maxlen=5000)
            fila.append(ts / 10_000)  # 100 ns -> ms
            self._visto[pid] = time.monotonic()
            self.eventos += 1
        except Exception:
            pass  # nunca deixar exceção escapar para dentro do Windows

    def registrar(self, pid: int, ts_ms: float) -> None:
        """Entrada manual de um quadro (usada nos testes)."""
        self._presentes.setdefault(pid, deque(maxlen=5000)).append(ts_ms)
        self._visto[pid] = time.monotonic()

    # ------------------------------------------------------------ leitura
    def _nome(self, pid: int) -> str:
        nome = self._nomes.get(pid)
        if nome is None:
            try:
                import psutil
                nome = psutil.Process(pid).name()
            except Exception:
                nome = f"PID {pid}"
            if len(self._nomes) > 200:
                self._nomes.clear()
            self._nomes[pid] = nome
        return nome

    def estatisticas(self, pid: int) -> dict | None:
        visto = self._visto.get(pid)
        fila = self._presentes.get(pid)
        if visto is None or fila is None or time.monotonic() - visto > SEM_QUADROS_S:
            return None
        r = calcular(list(fila))
        if r:
            r["pid"] = pid
            r["jogo"] = self._nome(pid)
        return r

    def todos(self) -> list[dict]:
        """Processos que entregaram quadros nos últimos 2 s, do maior FPS para o menor."""
        agora = time.monotonic()
        for pid in [p for p, v in list(self._visto.items()) if agora - v > HISTORICO_MS / 1000]:
            self._visto.pop(pid, None)
            self._presentes.pop(pid, None)
        lista = [s for s in (self.estatisticas(p) for p in list(self._visto)) if s]
        return sorted(lista, key=lambda s: -s["fps"])

    def primeiro_plano(self) -> dict | None:
        """Estatísticas do programa em primeiro plano (o jogo na tela)."""
        pid = pid_primeiro_plano()
        return self.estatisticas(pid) if pid and pid != os.getpid() else None


def pid_primeiro_plano() -> int | None:
    if not EH_WINDOWS:
        return None
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return None
    pid = ctypes.c_uint32()
    u.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
    return pid.value or None


_instancia: Quadros | None = None


def instancia() -> Quadros:
    global _instancia
    if _instancia is None:
        _instancia = Quadros()
    return _instancia
