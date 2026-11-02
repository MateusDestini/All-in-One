"""Sensores ao vivo: temperaturas, cargas, clocks, ventoinhas, consumo e tensões.

A leitura usa a LibreHardwareMonitorLib (MPL-2.0, a mesma biblioteca do LibreHardwareMonitor e do
ResetExplode) num processo separado (`--sensores`): assim a detecção do hardware, que leva alguns segundos,
nunca trava o painel, e uma falha no driver não derruba o monitor. Sem a biblioteca, ainda dá para ler a
placa NVIDIA (NVML), a carga de qualquer placa (contadores do Windows) e a carga do processador.

Temperatura e consumo do processador exigem o programa aberto como administrador e o driver PawnIO.
Formatos, categorias e diagnóstico adaptados do ResetExplode (SensorMonitor.cs e SensorRow.cs; licença MIT,
© 2026 Gustavo).
"""

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import psutil

from . import gpu, windows

log = logging.getLogger(__name__)
PASTA_LHM = Path(__file__).resolve().parent / "nativo" / "lhm"
HISTORICO_MAX = 900  # 15 min a 1 amostra/s
DESLIGAR_APOS_S = 90  # sem ninguém pedindo leituras, o processo de sensores é encerrado

CATEGORIAS = {
    "Temperature": "Temperatura", "Load": "Carga", "Clock": "Clock", "Power": "Consumo", "Voltage": "Tensão",
    "Current": "Corrente", "Fan": "Ventoinhas", "Control": "Controle", "Level": "Nível", "Data": "Dados",
    "SmallData": "Dados", "Throughput": "Taxa", "Frequency": "Frequência", "Energy": "Energia", "Flow": "Fluxo",
    "Humidity": "Umidade", "Factor": "Fator", "Timing": "Tempo", "TimeSpan": "Tempo", "Noise": "Ruído",
}
ORDEM = {"Clock": 0, "Temperature": 1, "Load": 2, "Fan": 3, "Control": 4, "Power": 5, "Voltage": 6, "Current": 7,
         "SmallData": 8, "Data": 9, "Throughput": 10}
TIPOS_HW = {"Cpu": "Processador", "GpuNvidia": "Placa de vídeo", "GpuAmd": "Placa de vídeo",
            "GpuIntel": "Placa de vídeo", "Memory": "Memória", "Motherboard": "Placa-mãe", "SuperIO": "Placa-mãe",
            "Storage": "Disco", "Network": "Rede", "Cooler": "Refrigeração", "EmbeddedController": "Placa-mãe",
            "Psu": "Fonte", "Battery": "Bateria"}


def formatar(tipo: str, v) -> str:
    if v is None:
        return "—"

    def n(x, casas):
        return f"{x:.{casas}f}".replace(".", ",")
    if tipo == "Temperature":
        return n(v, 1) + " °C"
    if tipo in ("Load", "Control", "Level", "Humidity"):
        return n(v, 1) + " %"
    if tipo == "Clock":
        return f"{v:.0f} MHz"
    if tipo == "Power":
        return n(v, 1) + " W"
    if tipo == "Voltage":
        return n(v, 3) + " V"
    if tipo == "Current":
        return n(v, 3) + " A"
    if tipo == "Fan":
        return f"{v:.0f} RPM"
    if tipo == "Flow":
        return n(v, 1) + " L/h"
    if tipo == "Frequency":
        return n(v, 1) + " Hz"
    if tipo == "Data":
        return n(v, 2) + " GB"
    if tipo == "SmallData":
        return n(v, 0) + " MB"
    if tipo == "Throughput":
        return n(v / 1048576, 2) + " MB/s"
    if tipo == "Energy":
        return n(v, 0) + " mWh"
    if tipo == "Timing":
        return n(v, 3 if v < 2 else 2) + " ns"
    return n(v, 2)


# ================================================================== processo de sensores (filho)

def _nome(enum) -> str:
    try:
        return str(enum.ToString())
    except AttributeError:
        return str(enum).rsplit(".", 1)[-1]


def _numero(v):
    """Valor do sensor (float? do .NET) como float do Python; NaN e infinito viram None."""
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        try:
            x = float(v.Value) if v.HasValue else None
        except Exception:
            return None
    return x if x is not None and math.isfinite(x) else None


def servidor() -> None:
    """Roda no processo `--sensores`: lê tudo a cada segundo e escreve uma linha JSON por leitura."""
    saida = sys.stdout or open(1, "w", closefd=False)
    entrada = sys.stdin or open(0, "r", closefd=False)

    def enviar(obj):
        saida.write(json.dumps(obj) + "\n")
        saida.flush()

    # Quando o monitor fecha (ou morre), a entrada padrão termina e este processo sai junto.
    comandos: deque = deque()

    def ouvir():
        for linha in entrada:
            comandos.append(linha.strip())
        os._exit(0)
    threading.Thread(target=ouvir, daemon=True).start()

    try:
        computador = _abrir_lhm()
    except Exception as e:  # sem .NET, sem a DLL, antivírus bloqueou...
        log.exception("biblioteca de sensores indisponível")
        enviar({"erro": f"{type(e).__name__}: {e}"})
        return

    def visitar(hw, linhas, zerar):
        hw.Update()
        tipo_hw = _nome(hw.HardwareType)
        for s in hw.Sensors:
            if zerar:
                s.ResetMin()
                s.ResetMax()
            linhas.append([str(s.Identifier), str(hw.Name), tipo_hw, _nome(s.SensorType), str(s.Name),
                           _numero(s.Value), _numero(s.Min), _numero(s.Max)])
        for sub in hw.SubHardware:
            visitar(sub, linhas, zerar)

    aberto_em, spd_pedido = time.time(), False
    while True:
        zerar = False
        while comandos:
            cmd = comandos.popleft()
            zerar |= cmd == "zerar"
            spd_pedido |= cmd == "spd"
        linhas = []
        inicio = time.time()
        for hw in computador.Hardware:
            try:
                visitar(hw, linhas, zerar)
            except Exception:
                log.exception("falha ao ler %s", hw.Name)
        enviar({"linhas": linhas})
        # O SPD é lido aqui, entre duas leituras, para nunca disputar o barramento com a própria LHM. Só depois
        # que a LHM achou os pentes (ela tenta por uns 12 s); sem pentes achados, não mexe no barramento.
        tem_pentes = any(l[2] == "Memory" and "/dimm/" in l[0] for l in linhas)
        if spd_pedido and (tem_pentes or time.time() - aberto_em > 15):
            spd_pedido = False
            try:
                enviar({"spd": _ler_spd() if tem_pentes else []})
            except Exception as e:
                log.exception("falha ao ler o SPD")
                enviar({"spd": [], "spd_erro": f"{type(e).__name__}: {e}"})
        time.sleep(max(0.2, 1.0 - (time.time() - inicio)))


def _ns(v, ciclo: bool = False):
    """Tempo do SPD em ns (a biblioteca pode entregar em ps)."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    if v > (5 if ciclo else 5000):
        v /= 1000
    return round(v, 3)


def _ler_spd() -> list[dict]:
    """Lê o SPD dos pentes pela RAMSPDToolkit, a mesma biblioteca e o mesmo driver que a LHM usa para a
    temperatura dos pentes (vem junto com ela, MPL-2.0). Só leitura: código do pente, fabricante do chip e os
    tempos mínimos JEDEC gravados nele."""
    try:
        import clr
        clr.AddReference("RAMSPDToolkit-NDD")  # a LHM já carregou; garante que a pythonnet enxergue o namespace
    except Exception:
        pass
    from RAMSPDToolkit.I2CSMBus import SMBusManager
    from RAMSPDToolkit.SPD import SPDDetector
    from RAMSPDToolkit.SPD.Enums import PageData
    from RAMSPDToolkit.SPD.Interop.Shared import SPDMemoryType
    from System import Byte

    saida = []
    for barramento in list(SMBusManager.RegisteredSMBuses):
        for endereco in range(0x50, 0x58):
            try:
                # 3º parâmetro opcional no C#: SPD_RESERVED = "descubra o tipo sozinho" (o mesmo que a LHM usa)
                ac = SPDDetector(barramento, Byte(endereco), SPDMemoryType.SPD_RESERVED).Accessor
            except Exception:
                continue
            if ac is None:
                continue
            item = {"endereco": endereco}

            def guardar(chave, ler):
                try:
                    v = ler()
                    if v not in (None, ""):
                        item[chave] = v
                except Exception:
                    pass

            def pagina(p) -> bool:
                try:
                    return bool(ac.ChangePage(p))
                except Exception:
                    return False

            guardar("indice", lambda: int(ac.Index))
            guardar("tipo", lambda: _nome(ac.MemoryType()))
            if pagina(PageData.ModulePartNumber):
                guardar("modelo", lambda: str(ac.ModulePartNumber()).strip().strip("\x00").strip())
            if pagina(PageData.ModuleManufacturerID):
                guardar("fabricante_pente", lambda: str(ac.GetModuleManufacturerString()).strip())
            if pagina(PageData.DRAMManufacturerCode):
                guardar("fabricante_chip", lambda: str(ac.GetDRAMManufacturerString()).strip())
            t = getattr(ac, "SDRAMTimings", None)
            if t is not None:
                tempos = {}
                for chave, nomes, ciclo in (
                        ("tck", ("MinimumCycleTime",), True), ("taa", ("MinimumCASLatencyTime",), False),
                        ("trcd", ("MinimumRASToCASDelayTime",), False), ("trp", ("MinimumRowPrechargeDelayTime",), False),
                        ("tras", ("MinimumActiveToPrechargeDelayTime",), False),
                        ("trc", ("MinimumActiveToActiveRefreshDelayTime",), False),
                        ("trfc1", ("NormalRefreshRecoveryTime", "MinimumRefreshRecoveryDelayTime1"), False),
                        ("trfc2", ("FineGranularityRefreshRecoveryTime", "MinimumRefreshRecoveryDelayTime2"), False),
                        ("trfcsb", ("SameBankRefreshRecoveryTime",), False)):
                    for nome in nomes:
                        try:
                            v = _ns(getattr(t, nome), ciclo)
                        except Exception:
                            v = None
                        if v:
                            tempos[chave] = v
                            break
                item["tempos"] = tempos
            saida.append(item)
    return saida


def _abrir_lhm():
    """Carrega a LibreHardwareMonitorLib (.NET Framework 4.7.2) pela pythonnet e detecta o hardware."""
    # Arquivos extraídos de um zip baixado da internet ficam marcados pelo Windows; o .NET recusa carregá-los.
    for dll in PASTA_LHM.glob("*.dll"):
        try:
            os.remove(str(dll) + ":Zone.Identifier")
        except OSError:
            pass
    import clr  # pythonnet (já vem com a pywebview)
    from System import AppDomain, ResolveEventHandler
    from System.Reflection import Assembly, AssemblyName

    def resolver(_origem, args):
        caminho = PASTA_LHM / (AssemblyName(args.Name).Name + ".dll")
        return Assembly.UnsafeLoadFrom(str(caminho)) if caminho.exists() else None
    AppDomain.CurrentDomain.AssemblyResolve += ResolveEventHandler(resolver)
    Assembly.UnsafeLoadFrom(str(PASTA_LHM / "LibreHardwareMonitorLib.dll"))
    try:
        clr.AddReference("LibreHardwareMonitorLib")  # garante que a pythonnet enxergue o namespace
    except Exception:
        pass
    from LibreHardwareMonitor.Hardware import Computer

    # Abre vazio e liga um grupo de cada vez: se um deles quebrar (ex.: discos), os outros continuam.
    c = Computer()
    c.Open()
    for prop in GRUPOS_LHM:
        try:
            setattr(c, prop, True)
        except AttributeError:
            pass  # versão da biblioteca sem esse grupo
        except Exception:
            log.exception("grupo de sensores %s indisponível", prop)
            try:
                setattr(c, prop, False)
            except Exception:
                pass
    return c


GRUPOS_LHM = ("IsCpuEnabled", "IsGpuEnabled", "IsMemoryEnabled", "IsMotherboardEnabled", "IsControllerEnabled",
              "IsPsuEnabled", "IsBatteryEnabled", "IsPowerMonitorEnabled", "IsNetworkEnabled", "IsStorageEnabled")


# ================================================================== lado do monitor (pai)

def pawnio_instalado() -> bool:
    """O driver PawnIO lê temperatura e consumo do processador; a biblioteca procura esta chave."""
    if not windows.EH_WINDOWS:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO",
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
            return True
    except OSError:
        return False


EXPLICACOES = {
    "sem_biblioteca": "A biblioteca de sensores não carregou ({erro}). Por enquanto aparecem só a carga do "
                      "processador e os dados da placa de vídeo que o driver informa.",
    "nao_admin": "O Windows só libera a temperatura e o consumo do processador para programas abertos como "
                 "administrador. Use o botão ao lado e aceite o pedido do Windows.",
    "sem_pawnio": "Falta o driver PawnIO, que lê a temperatura e o consumo do processador. Ele é gratuito, "
                  "assinado e usado também pelo LibreHardwareMonitor e pelo FanControl. Depois de instalar, "
                  "os sensores reiniciam sozinhos.",
    "falha": "O PawnIO está instalado, mas a leitura falhou. Se acabou de instalar, reinicie o PC. Se continuar, "
             "confira se o antivírus não bloqueou o driver.",
}


class Sensores:
    def __init__(self):
        self._trava = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._linhas: list = []
        self._erro: str | None = None
        self._ultimo_pedido = 0.0
        self._ultima_leitura = 0.0
        self._historico: deque = deque(maxlen=HISTORICO_MAX)
        self._placas: list[dict] | None = None
        self._carga_gpu = None
        self._permitido = True
        self._retencoes: set[str] = set()  # testes que precisam dos sensores mesmo com a janela escondida
        self._spd: list | None = None  # SPD dos pentes (não muda com o PC ligado: lido uma vez)
        self._spd_chegou = threading.Event()
        self._spd_pedido_a = None  # processo a quem o SPD já foi pedido
        threading.Thread(target=self._vigiar, daemon=True, name="sensores").start()

    # ------------------------------------------------------------ processo
    def _comando(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, "--sensores"]
        return [sys.executable, "-m", "monitor_lentidao", "--sensores"]

    def _iniciar_processo(self) -> None:
        if not windows.EH_WINDOWS or not (PASTA_LHM / "LibreHardwareMonitorLib.dll").exists():
            self._erro = self._erro or "biblioteca não encontrada"
            return
        try:
            self._proc = subprocess.Popen(
                self._comando(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=str(Path(__file__).resolve().parent.parent), creationflags=windows.CREATE_NO_WINDOW)
        except OSError as e:
            self._erro = str(e)
            return
        threading.Thread(target=self._ler_processo, args=(self._proc,), daemon=True).start()

    def _ler_processo(self, proc: subprocess.Popen) -> None:
        for linha in proc.stdout:
            try:
                dados = json.loads(linha)
            except ValueError:
                continue
            if "erro" in dados:
                self._erro = dados["erro"]
                break
            if "spd" in dados:
                if dados.get("spd_erro"):
                    log.warning("SPD dos pentes: %s", dados["spd_erro"])
                self._spd = dados["spd"] or []
                self._spd_chegou.set()
                continue
            primeira = not self._linhas
            with self._trava:
                self._linhas = dados.get("linhas") or []
                self._erro = None
                self._ultima_leitura = time.time()
            if primeira and self._spd is None and windows.eh_admin():
                self._pedir_spd()  # já deixa pronto para a página Timings da RAM
        if self._proc is proc:
            self._proc = None

    def parar(self) -> None:
        proc, self._proc = self._proc, None
        if proc:
            # Fechar a entrada faz o filho sair sozinho (e liberar o driver). No .exe de arquivo único o
            # processo real é neto do Popen, então matar o Popen não bastaria.
            try:
                proc.stdin.close()
                proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
        with self._trava:
            self._linhas = []

    def reiniciar(self) -> None:
        self.parar()
        self._erro = None
        self._iniciar_processo()

    def _pedir_spd(self) -> None:
        proc = self._proc
        if proc is None or proc is self._spd_pedido_a or not proc.stdin:
            return
        try:
            proc.stdin.write(b"spd\n")
            proc.stdin.flush()
            self._spd_pedido_a = proc
        except OSError:
            pass

    def spd(self, espera: float = 0.0) -> list | None:
        """SPD dos pentes [{modelo, fabricante_chip, tempos...}]; [] quando não dá para ler (sem administrador, sem
        PawnIO, placa sem acesso ao barramento) e None enquanto a resposta não chegou."""
        if self._spd is not None or not windows.EH_WINDOWS:
            return self._spd
        self._garantir_processo()
        self._pedir_spd()
        if espera > 0:
            self._spd_chegou.wait(espera)
        return self._spd

    def zerar_min_max(self) -> None:
        if self._proc and self._proc.stdin:
            try:
                self._proc.stdin.write(b"zerar\n")
                self._proc.stdin.flush()
            except OSError:
                pass

    def permitir(self, sim: bool) -> None:
        """A janela escondida não pede sensores; ao esconder, encerra o processo para não gastar nada."""
        self._permitido = sim
        if not sim and not self._retencoes:
            self.parar()

    def reter(self, quem: str, sim: bool) -> None:
        """Estresse, benchmark e análise de gargalo mantêm os sensores ligados mesmo com o painel fechado."""
        if sim:
            self._retencoes.add(quem)
            self._ultimo_pedido = time.time()
            if self._proc is None and self._erro is None and windows.EH_WINDOWS:
                self._iniciar_processo()
        else:
            self._retencoes.discard(quem)

    def _ativo(self) -> bool:
        if self._retencoes:
            self._ultimo_pedido = time.time()
            return True
        return self._permitido and time.time() - self._ultimo_pedido <= DESLIGAR_APOS_S

    def _vigiar(self) -> None:
        """Coleta uma amostra por segundo enquanto alguém está olhando e desliga depois."""
        while True:
            time.sleep(1)
            if not self._ativo():
                if self._proc:
                    self.parar()
                continue
            if self._proc is None and self._erro is None:
                self._iniciar_processo()
            try:
                self._amostrar()
            except Exception:
                log.exception("falha ao amostrar sensores")

    # ------------------------------------------------------------ leitura
    def _placas_video(self) -> list[dict]:
        if self._placas is None:
            try:
                self._placas = [{"nome": p["nome"], "handle": p["handle"], "luid": p["luid"], "integrada": p["integrada"]}
                                for p in gpu.coletar()]
            except Exception:
                log.exception("falha ao listar placas de vídeo")
                self._placas = []
        return self._placas

    def _linhas_reserva(self) -> list:
        """Sem a biblioteca: processador (psutil), placa NVIDIA (NVML) e carga de qualquer placa (Windows)."""
        linhas = [["/cpu/carga", "Processador", "Cpu", "Load", "CPU Total", psutil.cpu_percent(), None, None]]
        vm = psutil.virtual_memory()
        linhas += [["/ram/uso", "Memória", "Memory", "Load", "Memory", vm.percent, None, None],
                   ["/ram/usada", "Memória", "Memory", "Data", "Memory Used", vm.used / 2**30, None, None],
                   ["/ram/livre", "Memória", "Memory", "Data", "Memory Available", vm.available / 2**30, None, None]]
        if windows.EH_WINDOWS and self._carga_gpu is None:
            self._carga_gpu = gpu.CargaGpu()
        cargas = self._carga_gpu.ler() if self._carga_gpu else {}
        for i, p in enumerate(self._placas_video()):
            base = f"/gpu/{i}"
            nv = gpu.leitura_nvml(p["handle"]) if p["handle"] else {}
            carga = nv.get("carga")
            if carga is None:
                carga = cargas.get(p["luid"] or "", {}).get("carga")
            tipo = "GpuNvidia" if p["handle"] else "GpuAmd"
            for sid, t, nome, v in (("temp", "Temperature", "GPU Core", nv.get("temp")),
                                    ("carga", "Load", "GPU Core", carga),
                                    ("clock", "Clock", "GPU Core", nv.get("clock")),
                                    ("clockmem", "Clock", "GPU Memory", nv.get("clock_mem")),
                                    ("watts", "Power", "GPU Package", nv.get("watts")),
                                    ("fan", "Control", "GPU Fan", nv.get("ventoinha")),
                                    ("vram", "SmallData", "GPU Memory Used",
                                     (nv.get("vram_usada") or cargas.get(p["luid"] or "", {}).get("vram_usada") or 0) / 2**20 or None),
                                    ("vramtot", "SmallData", "GPU Memory Total",
                                     (nv.get("vram_total") or 0) / 2**20 or None)):
                if v is not None:
                    linhas.append([f"{base}/{sid}", p["nome"], tipo, t, nome, float(v), None, None])
        return linhas

    def _linhas_atuais(self) -> tuple[list, bool]:
        with self._trava:
            linhas = list(self._linhas)
            fresco = time.time() - self._ultima_leitura < 5
        if linhas and fresco:
            # A biblioteca às vezes não acha a placa de vídeo (driver antigo): completa com a NVML.
            if not any(l[2].startswith("Gpu") for l in linhas):
                linhas += [l for l in self._linhas_reserva() if l[2].startswith("Gpu")]
            return linhas, True
        return self._linhas_reserva(), False

    @staticmethod
    def resumo(linhas: list) -> dict:
        def achar(tipo_hw, tipo, filtro=lambda n: True, hw=None):
            for l in linhas:
                if (l[2] == tipo_hw or (tipo_hw == "Gpu" and l[2].startswith("Gpu"))) and l[3] == tipo \
                        and filtro(l[4]) and l[5] is not None and (hw is None or l[1] == hw):
                    return l[5]
            return None
        temps = [l for l in linhas if l[2] == "Cpu" and l[3] == "Temperature" and (l[5] or 0) > 0]
        principal = next((l for l in temps if any(k in l[4] for k in ("Package", "Tctl", "Tdie"))), None)
        cpu_temp = principal[5] if principal else max((l[5] for l in temps), default=None)
        clocks = [l[5] for l in linhas if l[2] == "Cpu" and l[3] == "Clock" and l[4].startswith("Core #") and (l[5] or 0) > 0]
        # placa de vídeo principal: a primeira dedicada (NVIDIA/AMD antes da Intel integrada)
        gpus = list(dict.fromkeys(l[1] for l in sorted(
            (l for l in linhas if l[2].startswith("Gpu")), key=lambda l: l[2] == "GpuIntel")))
        g = gpus[0] if gpus else None
        def primeiro(*valores):
            return next((v for v in valores if v is not None), None)
        return {
            "cpu_temp": cpu_temp,
            "cpu_carga": achar("Cpu", "Load", lambda n: n == "CPU Total"),
            "cpu_watts": achar("Cpu", "Power", lambda n: "Package" in n),
            "cpu_clock": sum(clocks) / len(clocks) if clocks else None,
            "gpu_nome": g,
            "gpu_temp": primeiro(achar("Gpu", "Temperature", lambda n: n == "GPU Core", g),
                                 achar("Gpu", "Temperature", hw=g)) if g else None,
            "gpu_carga": primeiro(achar("Gpu", "Load", lambda n: n == "GPU Core", g),
                                  achar("Gpu", "Load", lambda n: n == "D3D 3D", g)) if g else None,
            "gpu_watts": primeiro(achar("Gpu", "Power", lambda n: "Package" in n or "Board" in n, g),
                                  achar("Gpu", "Power", hw=g)) if g else None,
            "gpu_clock": achar("Gpu", "Clock", lambda n: n == "GPU Core", g) if g else None,
            "vram_usada": primeiro(achar("Gpu", "SmallData", lambda n: n == "GPU Memory Used", g),
                                   achar("Gpu", "SmallData", lambda n: n == "D3D Dedicated Memory Used", g)) if g else None,
            "vram_total": achar("Gpu", "SmallData", lambda n: n == "GPU Memory Total", g) if g else None,
        }

    def _amostrar(self) -> None:
        linhas, _ = self._linhas_atuais()
        r = self.resumo(linhas)
        self._historico.append([round(time.time()), r["cpu_temp"], r["gpu_temp"], r["cpu_carga"], r["gpu_carga"],
                                r["cpu_watts"], r["gpu_watts"]])

    def diagnostico(self, resumo: dict, da_biblioteca: bool) -> dict | None:
        if resumo["cpu_temp"] is not None:
            return None
        if not windows.EH_WINDOWS:
            return None
        if self._erro and not da_biblioteca:
            codigo = "sem_biblioteca"
        elif not windows.eh_admin():
            codigo = "nao_admin"
        elif not pawnio_instalado():
            codigo = "sem_pawnio"
        elif not da_biblioteca:
            return {"codigo": "carregando", "texto": "Detectando o hardware... leva alguns segundos."}
        else:
            codigo = "falha"
        return {"codigo": codigo, "texto": EXPLICACOES[codigo].format(erro=self._erro or "")}

    def leitura(self, com_linhas: bool = True, desde: int = 0) -> dict:
        """Chamado pelo painel. Também mantém o processo de sensores ligado enquanto houver pedidos."""
        self._garantir_processo()
        linhas, da_biblioteca = self._linhas_atuais()
        r = self.resumo(linhas)
        r["cpu_nome"] = next((l[1] for l in linhas if l[2] == "Cpu" and l[1] != "Processador"), None) or nome_cpu()
        saida = {"resumo": r, "biblioteca": da_biblioteca, "diagnostico": self.diagnostico(r, da_biblioteca),
                 "historico": [h for h in self._historico if h[0] > desde]}
        if com_linhas:
            grupos: dict[str, dict] = {}
            for sid, hw, tipo_hw, tipo, nome, v, mn, mx in linhas:
                g = grupos.setdefault(hw, {"nome": hw, "tipo": TIPOS_HW.get(tipo_hw, tipo_hw), "linhas": []})
                g["linhas"].append({"id": sid, "categoria": CATEGORIAS.get(tipo, tipo), "nome": nome,
                                    "valor": formatar(tipo, v), "min": formatar(tipo, mn), "max": formatar(tipo, mx),
                                    "ordem": ORDEM.get(tipo, 11)})
            for g in grupos.values():
                g["linhas"].sort(key=lambda x: (x["ordem"], x["nome"]))
            ordem_hw = {"Processador": 0, "Placa de vídeo": 1, "Memória": 2, "Placa-mãe": 3, "Disco": 4}
            saida["grupos"] = sorted(grupos.values(), key=lambda g: (ordem_hw.get(g["tipo"], 9), g["nome"]))
        return saida

    def linhas(self) -> tuple[list, bool]:
        """Leitura bruta [id, hardware, tipo_hw, tipo, nome, valor, mín, máx] e se veio da biblioteca."""
        self._garantir_processo()
        return self._linhas_atuais()

    def _garantir_processo(self) -> None:
        self._ultimo_pedido = time.time()
        if (self._permitido or self._retencoes) and self._proc is None and self._erro is None and windows.EH_WINDOWS:
            self._iniciar_processo()

    def placas(self) -> list[dict]:
        """Placas de vídeo {nome, handle, luid, integrada}, a dedicada de mais VRAM primeiro."""
        return list(self._placas_video())

    def placa_nvidia(self):
        """Handle NVML da primeira placa NVIDIA, ou None."""
        return next((p["handle"] for p in self._placas_video() if p.get("handle")), None)

    def snapshot(self) -> list[dict]:
        """Leitura do momento para o relatório (pode vir só da reserva, se a biblioteca estiver desligada)."""
        linhas, _ = self._linhas_atuais()
        return [{"hardware": hw, "categoria": CATEGORIAS.get(t, t), "sensor": n, "valor": formatar(t, v),
                 "min": formatar(t, mn), "max": formatar(t, mx)} for _, hw, _, t, n, v, mn, mx in linhas]


_nome_cpu: str | None = None


def nome_cpu() -> str:
    """Nome comercial do processador (ex.: "AMD Ryzen 7 7700 8-Core Processor")."""
    global _nome_cpu
    if _nome_cpu is None:
        _nome_cpu = ""
        if windows.EH_WINDOWS:
            import winreg
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                    _nome_cpu = str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
            except OSError:
                pass
        if not _nome_cpu:
            import platform
            _nome_cpu = platform.processor() or ""
            try:
                with open("/proc/cpuinfo", encoding="utf-8") as f:
                    _nome_cpu = next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), _nome_cpu)
            except OSError:
                pass
    return _nome_cpu


_instancia: Sensores | None = None


def instancia() -> Sensores:
    global _instancia
    if _instancia is None:
        _instancia = Sensores()
    return _instancia


def instalar_pawnio(progresso=lambda f, e: None) -> dict:
    """Instala o PawnIO pelo winget (o Windows pede confirmação de administrador)."""
    from . import apps
    progresso(0.1, "Baixando e instalando o PawnIO pelo winget...")
    ok, msg = apps.instalar_winget("namazso.PawnIO")
    if ok:
        instancia().reiniciar()
    return {"ok": ok, "mensagem": "PawnIO instalado. Os sensores do processador vão aparecer em alguns segundos."
            if ok else f"Não deu para instalar o PawnIO: {msg}"}
