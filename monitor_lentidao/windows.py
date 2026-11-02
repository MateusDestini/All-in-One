"""Acesso a recursos específicos do Windows.

Tudo aqui falha de forma silenciosa (retornando vazio/None) fora do Windows ou
quando falta permissão, para que uma verificação indisponível não derrube as outras.
"""

import ctypes
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

EH_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000
NOME_APP = "MonitorLentidao"

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- comandos

def executar(cmd: list[str], timeout: int = 60) -> str:
    # Em segundo plano (modo leve), PowerShell e afins rodam com prioridade abaixo do normal.
    extra = {"creationflags": CREATE_NO_WINDOW | (0x4000 if _modo_atual else 0)} if EH_WINDOWS else {}
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **extra)
    except (OSError, subprocess.TimeoutExpired):
        log.warning("falha ao executar %s", cmd[0], exc_info=True)
        return ""
    return r.stdout.decode("utf-8", errors="replace")


def powershell_json(script: str, timeout: int = 90) -> list[dict]:
    """Roda um script PowerShell que termina em ConvertTo-Json e devolve sempre uma lista."""
    if not EH_WINDOWS:
        return []
    prefixo = "[Console]::OutputEncoding=[Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';"
    saida = executar(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-Command", prefixo + script],
        timeout,
    ).strip()
    if not saida:
        return []
    try:
        dados = json.loads(saida)
    except ValueError:
        log.warning("saída inesperada do PowerShell: %.200s", saida)
        return []
    return dados if isinstance(dados, list) else [dados]


# ---------------------------------------------------------------- contadores de desempenho (PDH)

if EH_WINDOWS:
    from ctypes import wintypes

    class _ValorPDH(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("longValue", wintypes.LONG), ("doubleValue", ctypes.c_double),
                        ("largeValue", ctypes.c_longlong), ("strValue", ctypes.c_wchar_p)]
        _fields_ = [("CStatus", wintypes.DWORD), ("u", _U)]

    class _ItemPDH(ctypes.Structure):
        _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _ValorPDH)]

_PDH_FMT_DOUBLE_NOCAP = 0x200 | 0x8000
_PDH_MORE_DATA = 0x800007D2


class ContadoresPDH:
    """Lê contadores de desempenho do Windows (os mesmos do Monitor de Desempenho).

    Os nomes em inglês funcionam em qualquer idioma do Windows.
    `ler()` devolve {nome: {instância: valor}}.
    """

    def __init__(self, caminhos: dict[str, str]):
        self.pdh = ctypes.WinDLL("pdh")
        self.query = ctypes.c_void_p()
        if self.pdh.PdhOpenQueryW(None, None, ctypes.byref(self.query)):
            raise OSError("PdhOpenQueryW falhou")
        self.contadores = {}
        for nome, caminho in caminhos.items():
            h = ctypes.c_void_p()
            if self.pdh.PdhAddEnglishCounterW(self.query, caminho, None, ctypes.byref(h)) == 0:
                self.contadores[nome] = h
            else:
                log.info("contador indisponível: %s", caminho)
        self.pdh.PdhCollectQueryData(self.query)

    def ler(self) -> dict[str, dict[str, float]]:
        self.pdh.PdhCollectQueryData(self.query)
        resultado = {}
        for nome, h in self.contadores.items():
            tamanho, qtd = wintypes.DWORD(0), wintypes.DWORD(0)
            st = self.pdh.PdhGetFormattedCounterArrayW(
                h, _PDH_FMT_DOUBLE_NOCAP, ctypes.byref(tamanho), ctypes.byref(qtd), None)
            if (st & 0xFFFFFFFF) != _PDH_MORE_DATA:
                continue
            buffer = (ctypes.c_byte * tamanho.value)()
            st = self.pdh.PdhGetFormattedCounterArrayW(
                h, _PDH_FMT_DOUBLE_NOCAP, ctypes.byref(tamanho), ctypes.byref(qtd), buffer)
            if st:
                continue
            itens = ctypes.cast(buffer, ctypes.POINTER(_ItemPDH))
            resultado[nome] = {
                itens[i].szName: itens[i].FmtValue.u.doubleValue
                for i in range(qtd.value)
                if itens[i].FmtValue.CStatus in (0, 1)  # dado válido / novo
            }
        return resultado


def criar_contadores() -> ContadoresPDH | None:
    if not EH_WINDOWS:
        return None
    try:
        return ContadoresPDH({
            "disco_ocioso": r"\PhysicalDisk(*)\% Idle Time",
            "disco_latencia": r"\PhysicalDisk(*)\Avg. Disk sec/Transfer",
            "cpu_desempenho": r"\Processor Information(_Total)\% Processor Performance",
        })
    except OSError:
        log.warning("contadores de desempenho indisponíveis", exc_info=True)
        return None


# ---------------------------------------------------------------- logs de eventos

_SCRIPT_EVENTOS = r"""
$ini=(Get-Date).AddHours(-%(horas)d)
$ev=@()
$ev+=Get-WinEvent -FilterHashtable @{LogName='System';Level=1,2,3;StartTime=$ini} -MaxEvents 1500 -ErrorAction SilentlyContinue
$ev+=Get-WinEvent -FilterHashtable @{LogName='Application';ProviderName='Application Hang','Application Error';StartTime=$ini} -MaxEvents 300 -ErrorAction SilentlyContinue
$ev | Where-Object {$_} | Select-Object Id,ProviderName,Level,
  @{n='Hora';e={$_.TimeCreated.ToString('s')}},
  @{n='Msg';e={(($_.Message -split "`n")[0]).Trim()}} | ConvertTo-Json -Compress
"""


def eventos_recentes(horas: int = 24) -> list[dict]:
    return powershell_json(_SCRIPT_EVENTOS % {"horas": horas})


_SCRIPT_BOOT = r"""
$r=@()
$e=Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational';Id=100} -MaxEvents 1 -ErrorAction SilentlyContinue
foreach($x in $e){ $d=([xml]$x.ToXml()).Event.EventData.Data
  $r+=[pscustomobject]@{Tipo='boot';Hora=$x.TimeCreated.ToString('s');Ms=[int](($d|Where-Object{$_.Name -eq 'BootTime'}).'#text');Nome=''} }
$c=Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational';Id=101,102,103;StartTime=(Get-Date).AddDays(-7)} -MaxEvents 50 -ErrorAction SilentlyContinue
foreach($x in $c){ $d=([xml]$x.ToXml()).Event.EventData.Data
  $n=($d|Where-Object{$_.Name -eq 'FriendlyName'}).'#text'; if(-not $n){$n=($d|Where-Object{$_.Name -eq 'Name'}).'#text'}
  $r+=[pscustomobject]@{Tipo='culpado';Hora=$x.TimeCreated.ToString('s');Ms=[int](($d|Where-Object{$_.Name -eq 'TotalTime'}).'#text');Nome=$n} }
$r | ConvertTo-Json -Compress
"""


def diagnostico_inicializacao() -> list[dict]:
    """Tempo do último boot e programas/drivers que atrasaram (pode exigir administrador)."""
    return powershell_json(_SCRIPT_BOOT)


# ---------------------------------------------------------------- inicialização e energia

def programas_inicializacao() -> list[dict]:
    """Programas que abrem junto com o Windows (registro Run + pasta Inicializar)."""
    from . import inicializacao
    return inicializacao.listar()


_descricoes: dict[str, str] = {}


def descricao_arquivo(caminho: str | None) -> str:
    """'Descrição · Fabricante' das propriedades do .exe (aba Detalhes)."""
    if not EH_WINDOWS or not caminho:
        return ""
    if caminho in _descricoes:
        return _descricoes[caminho]
    texto = ""
    try:
        ver = ctypes.WinDLL("version")
        tam = ver.GetFileVersionInfoSizeW(caminho, None)
        if tam:
            buf = ctypes.create_string_buffer(tam)
            if ver.GetFileVersionInfoW(caminho, 0, tam, buf):
                ptr, n = ctypes.c_void_p(), ctypes.c_uint()
                idioma = "040904b0"
                if ver.VerQueryValueW(buf, "\\VarFileInfo\\Translation", ctypes.byref(ptr), ctypes.byref(n)) and n.value >= 4:
                    lang, cp = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16))[0:2]
                    idioma = f"{lang:04x}{cp:04x}"
                partes = []
                for campo in ("FileDescription", "CompanyName"):
                    if ver.VerQueryValueW(buf, f"\\StringFileInfo\\{idioma}\\{campo}", ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                        v = ctypes.wstring_at(ptr.value, n.value).strip("\x00 ").strip()
                        if v and v not in partes:
                            partes.append(v)
                texto = " · ".join(partes)
    except OSError:
        pass
    _descricoes[caminho] = texto
    return texto


ECONOMIA_ENERGIA = "a1841308-3541-4fab-bc81-f71556f20b4a"


def plano_energia() -> tuple[str, str] | None:
    """(guid, nome) do plano de energia ativo."""
    if not EH_WINDOWS:
        return None
    saida = executar(["powercfg", "/getactivescheme"], timeout=15)
    for parte in saida.split():
        if parte.count("-") == 4 and len(parte) == 36:
            nome = saida[saida.find("(") + 1:saida.rfind(")")] if "(" in saida else ""
            return parte.lower(), nome
    return None


# ---------------------------------------------------------------- integração com o sistema

def _comando_autostart() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --bandeja'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    lancador = Path(__file__).resolve().parent.parent / "MonitorLentidao.pyw"
    return f'"{pythonw if pythonw.exists() else sys.executable}" "{lancador}" --bandeja'


_CHAVE_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_ativo() -> bool:
    if not EH_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CHAVE_RUN) as k:
            winreg.QueryValueEx(k, NOME_APP)
            return True
    except OSError:
        return False


def definir_autostart(ativo: bool) -> None:
    if not EH_WINDOWS:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _CHAVE_RUN, 0, winreg.KEY_SET_VALUE) as k:
        if ativo:
            winreg.SetValueEx(k, NOME_APP, 0, winreg.REG_SZ, _comando_autostart())
        else:
            try:
                winreg.DeleteValue(k, NOME_APP)
            except FileNotFoundError:
                pass


_mutex = None


def instancia_unica() -> bool:
    """False se já existe outro Monitor de Lentidão rodando nesta sessão."""
    global _mutex
    if not EH_WINDOWS:
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = kernel32.CreateMutexW(None, False, f"Local\\{NOME_APP}")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def eh_admin() -> bool:
    if not EH_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def reabrir_como_admin() -> bool:
    """Abre outra cópia do monitor como administrador (o Windows pede confirmação). True se foi aceito."""
    if not EH_WINDOWS:
        return False
    if getattr(sys, "frozen", False):
        exe, args = sys.executable, "--aguardar"
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pythonw if pythonw.exists() else sys.executable)
        args = f'"{Path(__file__).resolve().parent.parent / "MonitorLentidao.pyw"}" --aguardar'
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
    return r > 32


def aguardar_instancia(segundos: float = 15) -> bool:
    """Espera a cópia anterior fechar (usado ao reabrir como administrador)."""
    import time
    fim = time.time() + segundos
    while time.time() < fim:
        if instancia_unica():
            return True
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.CloseHandle(ctypes.c_void_p(_mutex))
        time.sleep(0.3)
    return False


def ativar_dpi() -> None:
    """Deixa o texto nítido em telas com zoom (125%, 150%...)."""
    if EH_WINDOWS:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def mensagem(titulo: str, texto: str) -> None:
    if EH_WINDOWS:
        ctypes.windll.user32.MessageBoxW(None, texto, titulo, 0x40)
    else:
        print(f"{titulo}: {texto}")


def abrir(caminho: Path) -> None:
    if EH_WINDOWS:
        os.startfile(caminho)
    else:
        print(f"(abrir) {caminho}")


# ---------------------------------------------------------------- processos numa chamada só (como o Gerenciador de Tarefas)
#
# NtQuerySystemInformation(SystemProcessInformation) devolve, de uma vez, tempo de CPU, memória, leituras/escritas
# e handles de todos os processos, sem abrir nenhum deles. O psutil abre cada processo (e, quando não tem permissão,
# refaz essa mesma consulta inteira por processo), o que custa muito mais num PC lento.

_OFS = {"proximo": 0, "cpu_user": 40, "cpu_kernel": 48, "nome_len": 56, "nome_ptr": 64, "pid": 80, "handles": 96,
        "ws": 144, "leitura": 232, "escrita": 240}
_buffer_proc = None


def ler_processos_bruto(buf, base: int) -> list[tuple]:
    """Interpreta o buffer de SYSTEM_PROCESS_INFORMATION (x64): (pid, nome, cpu_100ns, ws_bytes, io_bytes, handles)."""
    import struct
    mv = memoryview(buf)
    saida, pos, tam = [], 0, len(buf)
    while pos + 256 <= tam:
        proximo = struct.unpack_from("<I", mv, pos)[0]
        user, kernel = struct.unpack_from("<qq", mv, pos + _OFS["cpu_user"])
        nome_len = struct.unpack_from("<H", mv, pos + _OFS["nome_len"])[0]
        nome_ptr = struct.unpack_from("<Q", mv, pos + _OFS["nome_ptr"])[0]
        pid = struct.unpack_from("<Q", mv, pos + _OFS["pid"])[0]
        handles = struct.unpack_from("<I", mv, pos + _OFS["handles"])[0]
        ws = struct.unpack_from("<Q", mv, pos + _OFS["ws"])[0]
        leitura, escrita = struct.unpack_from("<qq", mv, pos + _OFS["leitura"])
        nome = ""
        if nome_ptr and nome_len:
            ini = nome_ptr - base
            if 0 <= ini and ini + nome_len <= tam:
                nome = bytes(mv[ini:ini + nome_len]).decode("utf-16-le", errors="replace")
        saida.append((int(pid), nome, user + kernel, ws, leitura + escrita, handles))
        if not proximo:
            break
        pos += proximo
    return saida


def processos_rapido() -> list[tuple] | None:
    """Lista de (pid, nome, cpu_100ns, ws_bytes, io_bytes, handles), ou None se não der (aí o psutil é usado)."""
    global _buffer_proc
    if not EH_WINDOWS or ctypes.sizeof(ctypes.c_void_p) != 8:
        return None
    ntdll = ctypes.WinDLL("ntdll")
    tamanho = ctypes.c_ulong(0)
    for _ in range(6):
        if _buffer_proc is None:
            _buffer_proc = ctypes.create_string_buffer(512 * 1024)
        status = ntdll.NtQuerySystemInformation(5, _buffer_proc, ctypes.sizeof(_buffer_proc), ctypes.byref(tamanho))
        status &= 0xFFFFFFFF
        if status == 0:
            return ler_processos_bruto(_buffer_proc.raw[:tamanho.value or ctypes.sizeof(_buffer_proc)],
                                       ctypes.addressof(_buffer_proc))
        if status != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH
            log.warning("NtQuerySystemInformation falhou: 0x%08X", status)
            return None
        _buffer_proc = ctypes.create_string_buffer(max(tamanho.value, ctypes.sizeof(_buffer_proc)) + 64 * 1024)
    return None


# ---------------------------------------------------------------- modo leve do próprio monitor

_modo_atual = None


def modo_economico(ligar: bool) -> None:
    """Em segundo plano o monitor roda com prioridade baixa, EcoQoS (núcleos e clocks econômicos no Windows 11)
    e memória de baixa prioridade. Com o painel aberto ou um teste rodando, volta ao normal."""
    global _modo_atual
    if not EH_WINDOWS or _modo_atual == ligar:
        return
    _modo_atual = ligar
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentProcess.restype = ctypes.c_void_p
    h = ctypes.c_void_p(k.GetCurrentProcess())
    try:
        k.SetPriorityClass(h, 0x4000 if ligar else 0x20)  # BELOW_NORMAL / NORMAL

        class Throttle(ctypes.Structure):
            _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]
        t = Throttle(1, 0x1, 0x1 if ligar else 0x0)  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        k.SetProcessInformation(h, 4, ctypes.byref(t), ctypes.sizeof(t))  # ProcessPowerThrottling
        prioridade = ctypes.c_ulong(2 if ligar else 5)  # MEMORY_PRIORITY_LOW / NORMAL
        k.SetProcessInformation(h, 0, ctypes.byref(prioridade), ctypes.sizeof(prioridade))  # ProcessMemoryPriority
    except (AttributeError, OSError):
        log.debug("modo econômico indisponível", exc_info=True)


def aparar_memoria() -> None:
    """Devolve ao Windows a memória que o monitor não está usando agora (vai para a lista de espera)."""
    import gc
    gc.collect()
    if not EH_WINDOWS:
        return
    try:
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetProcessWorkingSetSizeEx(ctypes.c_void_p(k.GetCurrentProcess()), ctypes.c_size_t(-1), ctypes.c_size_t(-1), 0)
    except (AttributeError, OSError):
        pass
