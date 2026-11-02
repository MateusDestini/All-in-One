"""Teste de estresse: processador (todos os threads), memória RAM (grava e confere padrões) e placa de vídeo.

Para sozinho quando a temperatura passa do limite escolhido, quando a RAM devolve um valor errado ou quando o
driver de vídeo reinicia. A carga do processador e da RAM é feita em C (nativo/bench.c); a da placa de vídeo é
um shader pesado em WebGL numa janela própria (o WebView2 desenha com Direct3D 11 na placa principal).
Adaptado do ResetExplode (StressTester.cs, GpuBurn.cs e MainWindow; licença MIT, © 2026 Gustavo).
"""

import ctypes
import json
import logging
import os
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import psutil

from . import benchmark, gpu, sensores, windows

log = logging.getLogger(__name__)
PAGINA_GPU = Path(__file__).resolve().parent / "ui" / "estresse_gpu.html"
RESOLUCOES_GPU = {"720p": (1280, 720), "1080p": (1920, 1080), "tela": (0, 0)}
INTENSIDADES_GPU = {"media": 48, "alta": 96, "extrema": 160}

ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _manter_acordado(sim: bool) -> None:
    """Sem isso o Windows pode entrar em suspensão no meio de um teste longo."""
    if windows.EH_WINDOWS:
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if sim else 0))
        except OSError:
            pass


class _PonteGpu:
    """Métodos chamados pela janela do estresse da placa de vídeo."""

    def __init__(self, dono: "Estresse"):
        self._dono = dono

    def relatar(self, fps, quadros, renderizador=""):
        self._dono._relato_gpu(float(fps or 0), int(quadros or 0), str(renderizador or ""))

    def falha(self, mensagem):
        self._dono._falha_gpu(str(mensagem or "falha desconhecida"))

    def fechar(self):
        threading.Thread(target=self._dono.parar, args=("Janela da placa de vídeo fechada",), daemon=True).start()


class Estresse:
    def __init__(self, notificar=None):
        self._notificar = notificar
        self.rodando = False
        self._e = benchmark.EstadoEstresse()
        self._fios: list[threading.Thread] = []
        self._log: deque = deque(maxlen=200)
        self._hist: deque = deque(maxlen=900)
        self._cfg: dict = {}
        self._inicio = 0.0
        self._max_cpu = self._max_gpu = 0.0
        self._status, self._alerta = "Parado", False
        self._janela = None
        self._fechando_janela = False
        self._gpu = {"ligada": False, "fps": 0.0, "quadros": 0, "renderizador": "", "erro": "", "inicio": 0.0}
        self._ram_threads = 0
        self._trava = threading.Lock()

    # ------------------------------------------------------------ preparo
    @staticmethod
    def preparar() -> dict:
        """O que o painel precisa saber antes de começar (avisar se não há sensor de temperatura)."""
        s = sensores.instancia()
        s.reter("estresse-preparo", True)
        try:
            for _ in range(10):  # a biblioteca leva alguns segundos para achar o hardware
                r = s.leitura(False)
                if (r["resumo"]["cpu_temp"] is not None or not windows.EH_WINDOWS
                        or (r["diagnostico"] or {}).get("codigo") != "carregando"):
                    break
                time.sleep(0.5)
        finally:
            s.reter("estresse-preparo", False)
        vm = psutil.virtual_memory()
        return {"cpu_temp": r["resumo"]["cpu_temp"], "gpu_temp": r["resumo"]["gpu_temp"],
                "gpu_nome": r["resumo"]["gpu_nome"], "diagnostico": r["diagnostico"],
                "ram_livre_gb": round(vm.available / 2**30, 1), "threads": os.cpu_count() or 1,
                "gpu_disponivel": _webview_disponivel()}

    # ------------------------------------------------------------ início e fim
    def iniciar(self, cfg: dict) -> dict:
        cpu, ram, placa = bool(cfg.get("cpu")), bool(cfg.get("ram")), bool(cfg.get("gpu"))
        if not (cpu or ram or placa):
            return {"ok": False, "mensagem": "Escolha processador, memória ou placa de vídeo."}
        try:
            limite_cpu = float(cfg.get("limite_cpu", 90))
            limite_gpu = float(cfg.get("limite_gpu", 85))
            minutos = float(cfg.get("minutos", 10))
        except (TypeError, ValueError):
            return {"ok": False, "mensagem": "Valores inválidos."}
        if not 40 <= limite_cpu <= 110:
            return {"ok": False, "mensagem": "O limite do processador deve ficar entre 40 e 110 °C."}
        if placa and not 40 <= limite_gpu <= 100:
            return {"ok": False, "mensagem": "O limite da placa de vídeo deve ficar entre 40 e 100 °C."}
        if placa and not _webview_disponivel():
            return {"ok": False, "mensagem": "O estresse da placa de vídeo precisa do painel moderno (WebView2)."}
        if (cpu or ram):
            try:
                benchmark.lib()
            except OSError:
                return {"ok": False, "mensagem": "O componente do teste não foi encontrado."}
        with self._trava:
            if self.rodando:
                return {"ok": False, "mensagem": "O teste já está rodando."}
            ocupado = benchmark.reservar("estresse")
            if ocupado:
                return {"ok": False, "mensagem": f"Espere terminar {ocupado}."}
            self.rodando = True
        fracao = min(0.9, max(0.1, float(cfg.get("fracao_ram", 0.6))))
        self._cfg = {"cpu": cpu, "ram": ram, "gpu": placa, "limite_cpu": limite_cpu, "limite_gpu": limite_gpu,
                     "minutos": max(0.0, minutos), "fracao_ram": fracao,
                     "resolucao": cfg.get("resolucao", "1080p"), "intensidade": cfg.get("intensidade", "alta")}
        self._e = benchmark.EstadoEstresse()
        self._fios = []
        self._hist.clear()
        self._max_cpu = self._max_gpu = 0.0
        self._inicio = time.time()
        self._status, self._alerta = "Rodando", False
        self._gpu = {"ligada": False, "fps": 0.0, "quadros": 0, "renderizador": "", "erro": "", "inicio": time.time()}
        sensores.instancia().reter("estresse", True)
        _manter_acordado(True)

        threads = os.cpu_count() or 1
        self._ram_threads = 0
        if ram:
            self._ram_threads = 1 if cpu else max(1, min(4, threads // 2))
            alvo = int(psutil.virtual_memory().available * fracao)
            self._iniciar_fio(lambda: benchmark.lib().st_ram(alvo, self._ram_threads, ctypes.byref(self._e)), "estresse-ram")
        if cpu:
            n = max(1, threads - (1 if ram and threads > 1 else 0))  # com RAM, sobra um thread para ela
            self._iniciar_fio(lambda: benchmark.lib().st_cpu(n, ctypes.byref(self._e)), "estresse-cpu")
        descricao_gpu = "não"
        if placa:
            erro = self._abrir_janela_gpu()
            if erro:
                self._registrar(f"Placa de vídeo não iniciou: {erro}")
            else:
                descricao_gpu = f"sim ({self._cfg['resolucao']}, carga {self._cfg['intensidade']}, limite {limite_gpu:.0f} °C)"
        self._registrar(
            f"Iniciado · processador {'sim (limite %.0f °C)' % limite_cpu if cpu else 'não'} · "
            f"RAM {'sim (%.0f%% da livre)' % (fracao * 100) if ram else 'não'} · placa de vídeo {descricao_gpu} · "
            f"{_duracao(minutos)}")
        threading.Thread(target=self._vigiar, daemon=True, name="estresse").start()
        return {"ok": True, "mensagem": ""}

    def _iniciar_fio(self, alvo, nome):
        t = threading.Thread(target=alvo, daemon=True, name=nome)
        t.start()
        self._fios.append(t)

    def parar(self, motivo: str = "Parado por você") -> None:
        with self._trava:
            if not self.rodando:
                return
            self.rodando = False
        self._e.cancelar = 1
        for t in self._fios:
            t.join(5)
        self._fechar_janela_gpu()
        sensores.instancia().reter("estresse", False)
        _manter_acordado(False)
        benchmark.liberar("estresse")
        abortado = motivo.startswith("ABORTADO")
        self._status, self._alerta = ("Abortado" if abortado else "Parado"), abortado
        partes = [motivo, f"duração {_hms(time.time() - self._inicio)}"]
        if self._cfg.get("cpu") and self._max_cpu:
            partes.append(f"processador máx. {_num(self._max_cpu)} °C")
        if self._cfg.get("ram"):
            partes.append(f"RAM {self._e.passadas // max(1, self._ram_threads)} passadas, {self._e.erros} erros")
        if self._cfg.get("gpu") and self._gpu["quadros"]:
            dur = max(1.0, time.time() - self._gpu["inicio"])
            partes.append(f"placa: {self._gpu['quadros']:,} quadros, média {self._gpu['quadros'] / dur:.0f} por segundo".replace(",", ".")
                          + (f", máx. {_num(self._max_gpu)} °C" if self._max_gpu else ""))
        self._registrar(" · ".join(partes))
        if self._notificar and (abortado or time.time() - self._inicio > 60):
            try:
                self._notificar("Teste de estresse " + ("interrompido" if abortado else "terminado"), motivo)
            except Exception:
                log.exception("falha ao notificar")

    # ------------------------------------------------------------ placa de vídeo (janela WebGL)
    def _abrir_janela_gpu(self) -> str | None:
        try:
            import webview
        except ImportError:
            return "pywebview indisponível"
        largura, altura = RESOLUCOES_GPU.get(self._cfg["resolucao"], (1920, 1080))
        cheia = largura == 0
        config_js = {"largura": largura, "altura": altura, "passos": INTENSIDADES_GPU.get(self._cfg["intensidade"], 96)}
        html = PAGINA_GPU.read_text(encoding="utf-8").replace("/*CONFIG*/null", json.dumps(config_js))
        self._fechando_janela = False
        try:
            # A janela abre um pouco menor que a resolução para caber na tela; o desenho continua na resolução escolhida.
            self._janela = webview.create_window(
                "Estresse da placa de vídeo (Esc ou fechar para parar)", html=html, js_api=_PonteGpu(self),
                width=min(largura or 1280, 1280), height=min(altura or 720, 760), fullscreen=cheia,
                background_color="#000000")
            self._janela.events.closed += self._janela_fechada
        except Exception as e:
            log.exception("falha ao abrir a janela do estresse da placa de vídeo")
            return str(e)
        self._gpu["ligada"] = True
        self._gpu["inicio"] = time.time()
        return None

    def _fechar_janela_gpu(self) -> None:
        janela, self._janela = self._janela, None
        self._gpu["ligada"] = False
        if janela:
            self._fechando_janela = True
            try:
                janela.destroy()
            except Exception:
                pass

    def _janela_fechada(self) -> None:
        self._gpu["ligada"] = False
        self._janela = None
        if not self._fechando_janela and self.rodando:
            threading.Thread(target=self.parar, args=("Janela da placa de vídeo fechada",), daemon=True).start()

    def _relato_gpu(self, fps: float, quadros: int, renderizador: str) -> None:
        self._gpu.update(fps=fps, quadros=quadros)
        if renderizador and not self._gpu["renderizador"]:
            renderizador = nome_renderizador(renderizador)
            self._gpu["renderizador"] = renderizador
            self._registrar(f"Placa de vídeo em uso: {renderizador}")

    def _falha_gpu(self, mensagem: str) -> None:
        self._gpu["erro"] = mensagem

    # ------------------------------------------------------------ acompanhamento
    def _vigiar(self) -> None:
        s = sensores.instancia()
        cfg = self._cfg
        while self.rodando:
            try:
                r = s.leitura(False)["resumo"]
            except Exception:
                r = {}
            t_cpu, t_gpu = r.get("cpu_temp"), r.get("gpu_temp")
            carga_cpu = cfg["cpu"] or cfg["ram"]
            if t_cpu is not None and carga_cpu:
                self._max_cpu = max(self._max_cpu, t_cpu)
            if t_gpu is not None and cfg["gpu"]:
                self._max_gpu = max(self._max_gpu, t_gpu)
            self._hist.append([round(time.time() - self._inicio), t_cpu, t_gpu])
            decorrido = time.time() - self._inicio
            motivo = None
            if carga_cpu and t_cpu is not None and t_cpu >= cfg["limite_cpu"]:
                motivo = f"ABORTADO: processador a {_num(t_cpu)} °C atingiu o limite de {cfg['limite_cpu']:.0f} °C"
            elif cfg["gpu"] and self._gpu["ligada"] and t_gpu is not None and t_gpu >= cfg["limite_gpu"]:
                motivo = f"ABORTADO: placa de vídeo a {_num(t_gpu)} °C atingiu o limite de {cfg['limite_gpu']:.0f} °C"
            elif self._e.erros > 0:
                motivo = f"ABORTADO: {self._e.erros} erros de memória (a RAM devolveu valores diferentes do gravado)"
            elif self._gpu["erro"]:
                motivo = f"ABORTADO: a placa de vídeo falhou ({self._gpu['erro']})"
            elif cfg["ram"] and not cfg["cpu"] and not cfg["gpu"] and decorrido > 20 and self._e.alocado == 0:
                motivo = "ABORTADO: não foi possível reservar memória para o teste"
            elif cfg["minutos"] > 0 and decorrido >= cfg["minutos"] * 60:
                motivo = "Concluído (tempo esgotado)"
            if motivo:
                threading.Thread(target=self.parar, args=(motivo,), daemon=True).start()
                return
            time.sleep(1)

    def _registrar(self, texto: str) -> None:
        self._log.append(f"{datetime.now():%H:%M:%S}  {texto}")

    def estado_curto(self) -> bool:
        """True quando o último teste foi abortado (temperatura, erro de memória ou falha da placa)."""
        return self._alerta

    def estado(self) -> dict:
        s = sensores.instancia()
        try:
            leitura = s.leitura(False)
            r = leitura["resumo"]
        except Exception:
            leitura, r = {}, {}
        limitador = ""
        h = s.placa_nvidia()
        if h:
            motivos = gpu.nvml().limitadores(h)
            limitador = gpu.limitador(motivos) if motivos is not None else ""
        dur_gpu = max(1.0, time.time() - self._gpu["inicio"]) if self._gpu["quadros"] else 0
        return {
            "rodando": self.rodando, "status": self._status, "alerta": self._alerta,
            "decorrido": _hms(time.time() - self._inicio) if self._inicio else "00:00:00",
            "cfg": self._cfg, "log": list(self._log), "historico": list(self._hist),
            "diagnostico": leitura.get("diagnostico"),
            "cpu": {"temp": r.get("cpu_temp"), "max": self._max_cpu or None, "carga": r.get("cpu_carga"),
                    "watts": r.get("cpu_watts"), "clock": r.get("cpu_clock")},
            "gpu": {"nome": r.get("gpu_nome"), "temp": r.get("gpu_temp"), "max": self._max_gpu or None,
                    "watts": r.get("gpu_watts"), "carga": r.get("gpu_carga"), "limitador": limitador,
                    "fps": round(self._gpu["fps"]) if self._gpu["ligada"] else None,
                    "fps_medio": round(self._gpu["quadros"] / dur_gpu) if dur_gpu else None,
                    "renderizador": self._gpu["renderizador"]},
            "ram_livre_gb": round(psutil.virtual_memory().available / 2**30, 1),
            "ram": {"alocado_gb": round(self._e.alocado / 2**30, 1), "passadas": self._e.passadas // max(1, self._ram_threads)
                    if self._ram_threads else 0, "erros": self._e.erros},
        }


def _duracao(minutos: float) -> str:
    if minutos <= 0:
        return "sem limite de tempo"
    return f"{minutos * 60:.0f} s" if minutos < 1 else f"{minutos:g} min".replace(".", ",")


def nome_renderizador(texto: str) -> str:
    """'ANGLE (NVIDIA, NVIDIA GeForce RTX 4070 (0x00002786) Direct3D11 vs_5_0 ps_5_0, D3D11)' -> 'NVIDIA GeForce RTX 4070 (Direct3D11)'."""
    m = re.match(r"ANGLE \([^,]*, (.+?)(?: \(0x[0-9A-Fa-f]+\))? (Direct3D\d+|OpenGL[^,]*|Vulkan[^,]*?)(?: vs_\S+ ps_\S+)?,", texto)
    return f"{m.group(1)} ({m.group(2).strip()})" if m else texto


def _num(v: float) -> str:
    return f"{v:.1f}".replace(".", ",")


def _hms(segundos: float) -> str:
    s = int(max(0, segundos))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _webview_disponivel() -> bool:
    import importlib.util
    return importlib.util.find_spec("webview") is not None
