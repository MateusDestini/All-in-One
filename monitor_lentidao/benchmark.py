"""Benchmarks do processador: render no estilo do Cinebench e simulação da CPU de um jogo.

O trabalho pesado fica em C (nativo/bench.c, na mesma DLL do teste de memória). As chamadas por ctypes soltam
o GIL, então o painel continua respondendo e lê o progresso enquanto o teste roda.
Adaptado do ResetExplode (BenchRenderer.cs e GameSimBench.cs; licença MIT, © 2026 Gustavo).
"""

import base64
import ctypes
import io
import json
import logging
import os
import threading
import time
from datetime import datetime

from . import config, gargalo, memoria, sensores

log = logging.getLogger(__name__)
MAX_FIOS = 64


class EstadoRender(ctypes.Structure):
    _fields_ = [("cancelar", ctypes.c_int32), ("blocos_feitos", ctypes.c_int32), ("blocos_total", ctypes.c_int32),
                ("passadas", ctypes.c_int32), ("amostras", ctypes.c_int64), ("ativos", ctypes.c_int32 * MAX_FIOS)]


class EstadoSim(ctypes.Structure):
    _fields_ = [("cancelar", ctypes.c_int32), ("aquecendo", ctypes.c_int32), ("quadros", ctypes.c_int32),
                ("progresso", ctypes.c_float), ("fps_atual", ctypes.c_float)]


class ResultadoSim(ctypes.Structure):
    _fields_ = [("fps_medio", ctypes.c_double), ("low1", ctypes.c_double), ("ms_principal", ctypes.c_double),
                ("ms_paralelo", ctypes.c_double), ("segundos", ctypes.c_double), ("quadros", ctypes.c_int32),
                ("threads", ctypes.c_int32)]


class EstadoEstresse(ctypes.Structure):
    _fields_ = [("cancelar", ctypes.c_int32), ("operacoes", ctypes.c_int64), ("passadas", ctypes.c_int64),
                ("erros", ctypes.c_int64), ("alocado", ctypes.c_int64)]


_preparada = False


def lib():
    global _preparada
    b = memoria._biblioteca()
    if not _preparada:
        b.bc_render.restype = ctypes.c_double
        b.bc_render.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_double, ctypes.POINTER(EstadoRender), ctypes.c_void_p]
        b.bc_dimensoes.argtypes = [ctypes.POINTER(ctypes.c_int32)]
        b.bc_bloco.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_int32)]
        b.gs_rodar.restype = ctypes.c_int
        b.gs_rodar.argtypes = [ctypes.c_int, ctypes.c_double, ctypes.POINTER(EstadoSim), ctypes.POINTER(ResultadoSim)]
        b.gs_desenhar.argtypes = [ctypes.c_void_p, ctypes.c_int]
        b.st_cpu.argtypes = [ctypes.c_int, ctypes.POINTER(EstadoEstresse)]
        b.st_ram.restype = ctypes.c_int64
        b.st_ram.argtypes = [ctypes.c_int64, ctypes.c_int, ctypes.POINTER(EstadoEstresse)]
        _preparada = True
    return b


# ------------------------------------------------------------------ um teste pesado por vez
_trava = threading.Lock()
_em_uso: str | None = None
NOMES = {"render": "o benchmark do processador", "sim": "a simulação de jogo", "estresse": "o teste de estresse"}


def reservar(nome: str) -> str | None:
    """Devolve None se conseguiu; senão, o que está rodando (para avisar o usuário)."""
    global _em_uso
    with _trava:
        if _em_uso and _em_uso != nome:
            return NOMES.get(_em_uso, _em_uso)
        _em_uso = nome
        return None


def em_uso() -> str | None:
    return _em_uso


def liberar(nome: str) -> None:
    global _em_uso
    with _trava:
        if _em_uso == nome:
            _em_uso = None


# ------------------------------------------------------------------ histórico
def _arquivo():
    return config.pasta_dados() / "benchmarks.json"


def historico() -> list[dict]:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _salvar(registro: dict) -> None:
    lista = historico() + [registro]
    por_tipo: dict[str, list] = {}
    for r in lista:
        por_tipo.setdefault(r.get("tipo", ""), []).append(r)
    lista = [r for v in por_tipo.values() for r in v[-10:]]  # últimos 10 de cada teste
    lista.sort(key=lambda r: r.get("ts", 0))
    try:
        _arquivo().write_text(json.dumps(lista, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.exception("não foi possível salvar o histórico de benchmarks")


def _jpeg(imagem, qualidade: int = 82) -> str:
    buf = io.BytesIO()
    imagem.save(buf, "JPEG", quality=qualidade)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _ao_vivo() -> dict:
    """Temperatura, clock, consumo e carga do processador agora."""
    try:
        r = sensores.instancia().leitura(False)["resumo"]
    except Exception:
        return {}
    return {k: r.get(k) for k in ("cpu_temp", "cpu_clock", "cpu_watts", "cpu_carga")}


# ------------------------------------------------------------------ render
class Render:
    MIN_MULTI_S = 15   # repete a imagem até completar o tempo (mede o desempenho sustentado, não só o pico)
    MIN_SINGLE_S = 10
    SUSTENTADO_S = 600
    PASSO_SINGLE = 16  # 1 núcleo desenha 1 a cada 16 blocos: mesma mistura da imagem, em menos tempo

    def __init__(self):
        self.rodando = False
        self.fase = ""
        self.erro = ""
        self.resultado: dict | None = None
        self._e = EstadoRender()
        self._pixels = None
        self._dims = None
        self._blocos: list[list[int]] = []
        self._inicio = self._inicio_fase = 0.0
        self._min_fase = 0.0
        self._threads = 1
        self._cache = (None, "")

    def _preparar(self):
        if self._dims is None:
            b = lib()
            d = (ctypes.c_int32 * 4)()
            b.bc_dimensoes(d)
            self._dims = list(d)
            out = (ctypes.c_int32 * 4)()
            for i in range(self._dims[2]):
                b.bc_bloco(i, out)
                self._blocos.append(list(out))
            self._pixels = (ctypes.c_uint8 * (self._dims[0] * self._dims[1]))()

    def iniciar(self, modo: str, sustentado: bool) -> dict:
        if self.rodando:
            return {"ok": False, "mensagem": "O benchmark já está rodando."}
        ocupado = reservar("render")
        if ocupado:
            return {"ok": False, "mensagem": f"Espere terminar {ocupado}."}
        try:
            self._preparar()
        except OSError:
            liberar("render")
            return {"ok": False, "mensagem": "O componente do benchmark não foi encontrado."}
        ctypes.memset(self._pixels, 0, len(self._pixels))
        self._e = EstadoRender()
        self.rodando, self.erro, self.resultado, self.fase = True, "", None, "Preparando..."
        self._inicio = time.time()
        self._threads = min(MAX_FIOS, os.cpu_count() or 1)
        threading.Thread(target=self._rodar, args=(modo, bool(sustentado)), daemon=True, name="render").start()
        return {"ok": True, "mensagem": ""}

    def cancelar(self) -> None:
        self._e.cancelar = 1

    def _rodar(self, modo: str, sustentado: bool) -> None:
        sensores.instancia().reter("render", True)
        b = lib()
        multi = single = None
        try:
            if modo in ("ambos", "multi"):
                self.fase = f"Todos os núcleos · {self._threads} threads"
                self._inicio_fase, self._min_fase = time.time(), self.SUSTENTADO_S if sustentado else self.MIN_MULTI_S
                nota = b.bc_render(self._threads, 1, self._min_fase, ctypes.byref(self._e), self._pixels)
                multi = nota if nota >= 0 else None
            if not self._e.cancelar and modo in ("ambos", "single"):
                self.fase = "1 núcleo · 1 thread"
                self._inicio_fase, self._min_fase = time.time(), self.SUSTENTADO_S if sustentado and modo == "single" else self.MIN_SINGLE_S
                nota = b.bc_render(1, self.PASSO_SINGLE, self._min_fase, ctypes.byref(self._e), self._pixels)
                single = nota if nota >= 0 else None
            if self._e.cancelar:
                self.fase = "Cancelado"
                return
            vivo = _ao_vivo()
            self.resultado = {
                "multi": round(multi) if multi else None, "single": round(single) if single else None,
                "razao": round(multi / single, 1) if multi and single else None, "threads": self._threads,
                "segundos": round(time.time() - self._inicio), "sustentado": sustentado,
                "cpu": sensores.nome_cpu(), "temp": vivo.get("cpu_temp")}
            self.fase = f"Concluído em {self.resultado['segundos'] // 60}:{self.resultado['segundos'] % 60:02d}"
            _salvar({"tipo": "render", "ts": time.time(), "quando": datetime.now().strftime("%d/%m %H:%M"),
                     "multi": self.resultado["multi"], "single": self.resultado["single"], "sustentado": sustentado,
                     "cpu": self.resultado["cpu"]})
        except Exception as e:
            log.exception("falha no benchmark de render")
            self.erro = f"Não foi possível concluir: {e}"
        finally:
            self.rodando = False
            sensores.instancia().reter("render", False)
            liberar("render")

    def _imagem(self) -> str:
        versao = (self._e.passadas, self._e.blocos_feitos, self.rodando)
        if self._cache[0] != versao:
            from PIL import Image
            img = Image.frombuffer("L", (self._dims[0], self._dims[1]), bytes(self._pixels), "raw", "L", 0, 1)
            self._cache = (versao, _jpeg(img.resize((self._dims[0] * 2 // 3, self._dims[1] * 2 // 3))))
        return self._cache[1]

    def estado(self, com_imagem: bool = True) -> dict:
        e = self._e
        total = max(1, e.blocos_total)
        saida = {"rodando": self.rodando, "fase": self.fase, "erro": self.erro, "resultado": self.resultado,
                 "decorrido": round(time.time() - self._inicio) if self._inicio else 0, "threads": self._threads,
                 "cpu": gargalo.nome_curto_cpu(sensores.nome_cpu()), "vivo": _ao_vivo(),
                 "historico": [h for h in historico() if h.get("tipo") == "render"]}
        if self.rodando:
            # A fase repete a imagem até passar do tempo mínimo: estima quantas passadas cabem e quanto falta.
            decorrido = time.time() - self._inicio_fase
            feitas = e.passadas + min(1.0, e.blocos_feitos / total)
            if feitas >= 0.03:
                por_passada = decorrido / feitas
                total_s = por_passada * max(1, -(-self._min_fase // por_passada))
                saida["progresso"] = round(min(0.99, decorrido / total_s), 3)
                saida["restante"] = max(0, round(total_s - decorrido))
            else:
                saida["progresso"] = 0.0
            saida["passada"] = e.passadas + 1
            saida["blocos"] = [self._blocos[i] for i in list(e.ativos)[:self._threads] if 0 <= i < len(self._blocos)]
        if com_imagem and self._dims:
            saida["imagem"] = self._imagem()
            saida["largura"], saida["altura"] = self._dims[0], self._dims[1]
        return saida


# ------------------------------------------------------------------ simulação de jogo
class SimJogo:
    LADO = 300
    UNIDADES = 30000

    def __init__(self):
        self.rodando = False
        self.erro = ""
        self.resultado: dict | None = None
        self._e = EstadoSim()
        self._segundos = 20
        self._buf = None

    def iniciar(self, segundos: int) -> dict:
        if self.rodando:
            return {"ok": False, "mensagem": "A simulação já está rodando."}
        ocupado = reservar("sim")
        if ocupado:
            return {"ok": False, "mensagem": f"Espere terminar {ocupado}."}
        try:
            lib()
        except OSError:
            liberar("sim")
            return {"ok": False, "mensagem": "O componente do benchmark não foi encontrado."}
        self._segundos = max(5, min(120, int(segundos or 20)))
        self._e = EstadoSim()
        self.rodando, self.erro, self.resultado = True, "", None
        threading.Thread(target=self._rodar, daemon=True, name="sim-jogo").start()
        return {"ok": True, "mensagem": ""}

    def cancelar(self) -> None:
        self._e.cancelar = 1

    def _rodar(self) -> None:
        r = ResultadoSim()
        threads = min(MAX_FIOS, os.cpu_count() or 1)
        try:
            rc = lib().gs_rodar(threads, float(self._segundos), ctypes.byref(self._e), ctypes.byref(r))
            if rc == 0:
                self.resultado = {"fps": round(r.fps_medio), "low1": round(r.low1), "ms_principal": round(r.ms_principal, 2),
                                  "ms_paralelo": round(r.ms_paralelo, 2), "quadros": r.quadros, "segundos": round(r.segundos),
                                  "threads": r.threads, "unidades": self.UNIDADES}
                _salvar({"tipo": "sim", "ts": time.time(), "quando": datetime.now().strftime("%d/%m %H:%M"),
                         "fps": self.resultado["fps"], "low1": self.resultado["low1"], "cpu": sensores.nome_cpu()})
        except Exception as e:
            log.exception("falha na simulação de jogo")
            self.erro = f"Não foi possível concluir: {e}"
        finally:
            self.rodando = False
            liberar("sim")

    def _imagem(self) -> str | None:
        try:
            from PIL import Image
            if self._buf is None:
                self._buf = (ctypes.c_uint8 * (self.LADO * self.LADO * 3))()
            lib().gs_desenhar(self._buf, self.LADO)
            return _jpeg(Image.frombuffer("RGB", (self.LADO, self.LADO), bytes(self._buf), "raw", "RGB", 0, 1), 85)
        except Exception:
            return None

    def estado(self) -> dict:
        e = self._e
        saida = {"rodando": self.rodando, "erro": self.erro, "resultado": self.resultado, "segundos": self._segundos,
                 "historico": [h for h in historico() if h.get("tipo") == "sim"]}
        if self.rodando:
            saida.update(aquecendo=bool(e.aquecendo), progresso=round(min(1.0, e.progresso), 3),
                         fps=round(e.fps_atual), quadros=e.quadros, imagem=self._imagem())
        return saida
