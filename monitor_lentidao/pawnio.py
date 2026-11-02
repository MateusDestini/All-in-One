"""Acesso ao driver PawnIO (https://pawnio.eu) para ler e ajustar o processador.

O PawnIO é um driver assinado pela Microsoft que só executa módulos assinados pelo autor dele. Cada módulo
limita o que pode ser feito (quais MSRs podem ser escritos, quais registradores do SMU podem ser tocados),
então o monitor nunca tem acesso livre ao hardware. Os módulos usados estão em `nativo/pawnio`.

Uso: `modulo("RyzenSMU").executar("ioctl_send_smu_command", [cmd, a0..a5], 6)`.
Operações no barramento PCI (SMU, SMN) devem segurar `trava_pci()`, o mutex compartilhado com os outros
programas de hardware (LibreHardwareMonitor, ZenTimings, HWiNFO...).
"""

import ctypes
import logging
import os
import sys
import threading
from pathlib import Path

from . import windows

log = logging.getLogger(__name__)

PASTA_MODULOS = Path(__file__).resolve().parent / "nativo" / "pawnio"

# HRESULT_FROM_NT(status) = status | 0x10000000
ERROS = {
    0xD0000022: "acesso negado (abra o monitor como administrador)",
    0xC0000022: "acesso negado (abra o monitor como administrador)",
    0xD00000BB: "não suportado neste processador",
    0xC00000BB: "não suportado neste processador",
    0xD0000010: "comando inválido",
    0xD000000D: "valor fora do permitido",
    0xD00000B5: "o processador não respondeu a tempo",
    0x90000011: "o processador está ocupado",
    0xD00000E5: "o processador recusou o comando",
    0xD0000003: "o processador não reconheceu o comando",
    0xD0000001: "o processador recusou o comando",
    0x80070002: "o driver PawnIO não está instalado",
    0x80070005: "acesso negado (abra o monitor como administrador)",
}


class ErroPawnIO(Exception):
    def __init__(self, onde: str, codigo: int):
        self.codigo = codigo & 0xFFFFFFFF
        self.onde = onde
        super().__init__(f"{onde}: {ERROS.get(self.codigo, f'erro 0x{self.codigo:08X}')}")

    @property
    def texto(self) -> str:
        return ERROS.get(self.codigo, f"erro 0x{self.codigo:08X}")


def _pasta_pawnio() -> Path | None:
    if not windows.EH_WINDOWS:
        return None
    import winreg
    for raiz in (winreg.HKEY_LOCAL_MACHINE,):
        for caminho in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"):
            try:
                with winreg.OpenKey(raiz, caminho) as k:
                    pasta = winreg.QueryValueEx(k, "InstallLocation")[0]
                    if pasta and (Path(pasta) / "PawnIOLib.dll").exists():
                        return Path(pasta)
            except OSError:
                pass
    padrao = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PawnIO"
    return padrao if (padrao / "PawnIOLib.dll").exists() else None


_lib = None
_lib_trava = threading.Lock()


def _biblioteca():
    global _lib
    with _lib_trava:
        if _lib is None:
            pasta = _pasta_pawnio()
            if not pasta:
                raise ErroPawnIO("PawnIO", 0x80070002)
            lib = ctypes.WinDLL(str(pasta / "PawnIOLib.dll"))
            H = ctypes.c_void_p
            lib.pawnio_open.argtypes = [ctypes.POINTER(H)]
            lib.pawnio_load.argtypes = [H, ctypes.c_char_p, ctypes.c_size_t]
            lib.pawnio_execute.argtypes = [H, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t,
                                           ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            lib.pawnio_close.argtypes = [H]
            for f in (lib.pawnio_open, lib.pawnio_load, lib.pawnio_execute, lib.pawnio_close):
                f.restype = ctypes.c_long
            _lib = lib
    return _lib


def disponivel() -> bool:
    return _pasta_pawnio() is not None


class Modulo:
    def __init__(self, nome: str):
        self.nome = nome
        self._trava = threading.Lock()
        lib = _biblioteca()
        h = ctypes.c_void_p()
        r = lib.pawnio_open(ctypes.byref(h))
        if r != 0:
            raise ErroPawnIO("abrir o PawnIO", r)
        dados = (PASTA_MODULOS / f"{nome}.bin").read_bytes()
        r = lib.pawnio_load(h, dados, len(dados))
        if r != 0:
            lib.pawnio_close(h)
            raise ErroPawnIO(f"carregar {nome}", r)
        self._h = h
        self._lib = lib

    def executar(self, funcao: str, entrada: list[int], n_saida: int) -> list[int]:
        ent = (ctypes.c_uint64 * max(1, len(entrada)))(*[int(v) & 0xFFFFFFFFFFFFFFFF for v in entrada])
        sai = (ctypes.c_uint64 * max(1, n_saida))()
        devolvido = ctypes.c_size_t()
        with self._trava:
            r = self._lib.pawnio_execute(self._h, funcao.encode(), ent, len(entrada), sai, n_saida,
                                         ctypes.byref(devolvido))
        if r != 0:
            raise ErroPawnIO(funcao, r)
        return [sai[i] for i in range(min(n_saida, devolvido.value or n_saida))]

    def fechar(self) -> None:
        if getattr(self, "_h", None):
            self._lib.pawnio_close(self._h)
            self._h = None


_modulos: dict = {}
_mod_trava = threading.Lock()


def modulo(nome: str) -> Modulo:
    """Módulo carregado (um por processo, reaproveitado)."""
    with _mod_trava:
        m = _modulos.get(nome)
        if m is None:
            m = Modulo(nome)
            _modulos[nome] = m
        return m


def fechar_todos() -> None:
    with _mod_trava:
        for m in _modulos.values():
            try:
                m.fechar()
            except Exception:
                pass
        _modulos.clear()


class trava_pci:
    """Mutex global "Access_PCI": evita que dois programas falem com o SMU ao mesmo tempo."""

    _handle = None

    def __init__(self, espera_ms: int = 5000):
        self.espera_ms = espera_ms

    def __enter__(self):
        if not windows.EH_WINDOWS:
            return self
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k.WaitForSingleObject.restype = ctypes.c_uint32
        if trava_pci._handle is None:
            trava_pci._handle = k.CreateMutexW(None, False, "Global\\Access_PCI")
        r = k.WaitForSingleObject(trava_pci._handle, self.espera_ms) if trava_pci._handle else 0
        if r not in (0, 0x80):  # WAIT_OBJECT_0 ou WAIT_ABANDONED
            raise ErroPawnIO("barramento PCI ocupado por outro programa", 0x90000011)
        return self

    def __exit__(self, *exc):
        if windows.EH_WINDOWS and trava_pci._handle:
            k = ctypes.WinDLL("kernel32")
            k.ReleaseMutex.argtypes = [ctypes.c_void_p]
            k.ReleaseMutex(trava_pci._handle)
        return False


def cpu_fms() -> tuple[str, int, int]:
    """(fabricante, família, modelo) do processador, pelo que o Windows informa."""
    import platform
    texto = platform.processor() or ""
    if not texto and windows.EH_WINDOWS:
        texto = os.environ.get("PROCESSOR_IDENTIFIER", "")
    fab = "amd" if "AuthenticAMD" in texto else "intel" if "GenuineIntel" in texto else ""
    import re
    f = re.search(r"Family (\d+)", texto)
    m = re.search(r"Model (\d+)", texto)
    if not fab and sys.platform != "win32":
        try:
            info = open("/proc/cpuinfo").read()
            fab = "amd" if "AuthenticAMD" in info else "intel" if "GenuineIntel" in info else ""
            f = re.search(r"cpu family\s*:\s*(\d+)", info)
            m = re.search(r"model\s*:\s*(\d+)", info)
        except OSError:
            pass
    return fab, int(f.group(1)) if f else 0, int(m.group(1)) if m else 0
