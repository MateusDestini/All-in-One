"""Benchmark da placa de vídeo: cena de jogo (rasterização) e ray tracing, numa janela WebGL 2 (ui/bench_gpu.html).

- Cena de jogo: vale com rio, terreno por ruído fractal, dezenas de milhares de árvores por instancing, sombra em tempo
  real com filtro (PCF), água com ondas e reflexo do céu, nuvens, neblina e pós-processamento HDR com brilho (bloom).
- Ray tracing: esferas, colunas e piso com sombra suave do sol, oclusão de ambiente e reflexos em várias rebatidas.
  É calculado nos shaders (roda em qualquer placa com WebGL 2); não usa os núcleos de RT da placa (DXR).

Tudo é desenhado fora da tela na resolução escolhida e cada quadro é medido sem o limite do V-Sync. Para comparar com
confiança: aquecimento sem medir e 3 passadas iguais; a nota sai da mediana das passadas, e a diferença entre a melhor e
a pior diz se dá para confiar. Ideia da metodologia (aquecer, passadas, mediana, nota = FPS × 10) do ResetExplode
(GpuGameBench.cs e GpuRtBench.cs, licença MIT); a cena e o código são próprios.
"""

import json
import logging
import statistics
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config, sensores
from .estresse import _manter_acordado, _webview_disponivel, nome_renderizador

log = logging.getLogger(__name__)

PAGINA = Path(__file__).resolve().parent / "ui" / "bench_gpu.html"
RESOLUCOES = {"1080p": (1920, 1080), "1440p": (2560, 1440), "4k": (3840, 2160)}
QUALIDADES = ("normal", "alta", "extrema")
CENAS = {"jogo": "Cena de jogo", "rt": "Ray tracing"}
AQUECER_S, PASSADAS, DURACAO_S = 8, 3, 20
LIMITE_TEMP = 90.0
VARIACAO_CONFIAVEL = 3.0  # % entre a melhor e a pior passada
MAX_HISTORICO = 60


def _arquivo() -> Path:
    return config.pasta_dados() / "bench_gpu.json"


def historico() -> list[dict]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
        return dados if isinstance(dados, list) else []
    except (OSError, ValueError):
        return []


def _salvar(item: dict) -> None:
    h = historico()
    h.append(item)
    try:
        _arquivo().write_text(json.dumps(h[-MAX_HISTORICO:], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        log.warning("não deu para salvar o histórico do benchmark da placa", exc_info=True)


def apagar_historico() -> None:
    try:
        _arquivo().unlink()
    except OSError:
        pass


def resumir(cena: str, dados: dict) -> dict:
    """Junta as passadas: mediana do FPS médio e do 1% low, variação e nota (FPS × 10)."""
    passadas = [p for p in (dados.get("passadas") or []) if p.get("quadros", 0) > 0 and p.get("fps", 0) > 0]
    if not passadas:
        raise ValueError("nenhuma passada válida")
    fps = statistics.median(p["fps"] for p in passadas)
    low1 = statistics.median(p["low1"] for p in passadas)
    variacao = (max(p["fps"] for p in passadas) - min(p["fps"] for p in passadas)) / fps * 100 if len(passadas) > 1 else 0.0
    largura, altura = int(dados.get("largura") or 0), int(dados.get("altura") or 0)
    r = {"cena": cena, "nome": CENAS.get(cena, cena), "quando": datetime.now().strftime("%d/%m/%Y %H:%M"),
         "resolucao": f"{largura}x{altura}", "qualidade": dados.get("qualidade", ""), "fps": round(fps, 1), "low1": round(low1, 1),
         "variacao": round(variacao, 1), "confiavel": len(passadas) >= 3 and variacao < VARIACAO_CONFIAVEL,
         "pontos": round(fps * 10), "passadas": [round(p["fps"], 1) for p in passadas],
         "placa": nome_renderizador(str(dados.get("renderizador") or "")), "detalhe": str(dados.get("detalhe") or "")}
    if cena == "rt" and dados.get("rpp"):
        r["giga_raios"] = round(fps * largura * altura * float(dados["rpp"]) / 1e9, 2)
    return r


def comparar(r: dict, anteriores: list[dict]) -> dict | None:
    """Último resultado com a mesma cena, resolução e qualidade (só esses dá para comparar)."""
    iguais = [h for h in anteriores if h.get("cena") == r["cena"] and h.get("resolucao") == r["resolucao"]
              and h.get("qualidade") == r["qualidade"]]
    if not iguais:
        return None
    a = iguais[-1]
    dif = (r["fps"] - a["fps"]) / a["fps"] * 100 if a.get("fps") else 0.0
    return {"quando": a.get("quando"), "fps": a.get("fps"), "diferenca": round(dif, 1),
            "dentro_da_variacao": abs(dif) < max(VARIACAO_CONFIAVEL, r["variacao"], a.get("variacao", 0))}


class _Ponte:
    """Métodos chamados pela janela do benchmark."""

    def __init__(self, dono: "BenchGpu"):
        self._dono = dono

    def progresso(self, dados):
        self._dono._progresso(dict(dados or {}))

    def resultado(self, cena, dados):
        self._dono._resultado(str(cena), dict(dados or {}))

    def falha(self, mensagem):
        self._dono._terminar(f"A placa de vídeo falhou: {mensagem}", erro=True)

    def concluir(self):
        self._dono._terminar("Concluído")

    def fechar(self):
        self._dono._terminar("Cancelado", erro=True)


class BenchGpu:
    def __init__(self, notificar=None):
        self._notificar = notificar
        self._trava = threading.Lock()
        self.rodando = False
        self._janela = None
        self._fechando = False
        self._cfg: dict = {}
        self._vivo: dict = {}
        self._resultados: list[dict] = []
        self._mensagem = ""
        self._erro = False
        self._temp_max: float | None = None
        self._inicio = 0.0

    def iniciar(self, cenas, resolucao="1080p", qualidade="alta") -> dict:
        cenas = [c for c in (cenas or []) if c in CENAS]
        if not cenas:
            return {"ok": False, "mensagem": "Escolha pelo menos uma cena."}
        if resolucao not in RESOLUCOES or qualidade not in QUALIDADES:
            return {"ok": False, "mensagem": "Resolução ou qualidade inválida."}
        if not _webview_disponivel():
            return {"ok": False, "mensagem": "O benchmark da placa de vídeo precisa do painel moderno (WebView2)."}
        with self._trava:
            if self.rodando:
                return {"ok": False, "mensagem": "O benchmark já está rodando."}
            self.rodando = True
        largura, altura = RESOLUCOES[resolucao]
        self._cfg = {"largura": largura, "altura": altura, "qualidade": qualidade, "cenas": cenas,
                     "aquecer": AQUECER_S, "passadas": PASSADAS, "duracao": DURACAO_S}
        self._vivo, self._resultados, self._mensagem, self._erro = {"fase": "abrindo", "pct": 0.0}, [], "", False
        self._temp_max, self._inicio, self._fechando = None, time.time(), False
        erro = self._abrir_janela()
        if erro:
            self.rodando = False
            return {"ok": False, "mensagem": f"Não deu para abrir a janela do teste: {erro}"}
        _manter_acordado(True)
        threading.Thread(target=self._vigiar, name="bench-gpu", daemon=True).start()
        return {"ok": True, "mensagem": "Benchmark iniciado."}

    def _abrir_janela(self) -> str | None:
        try:
            import webview
            html = PAGINA.read_text(encoding="utf-8").replace("/*CONFIG*/null", json.dumps(self._cfg))
            self._janela = webview.create_window("Benchmark da placa de vídeo (Esc cancela)", html=html, js_api=_Ponte(self),
                                                 width=1280, height=760, background_color="#000000")
            self._janela.events.closed += self._janela_fechada
        except Exception as e:
            log.exception("falha ao abrir a janela do benchmark da placa")
            return str(e)
        return None

    def _janela_fechada(self) -> None:
        self._janela = None
        if not self._fechando and self.rodando:
            self._terminar("Cancelado (janela fechada)", erro=True)

    def cancelar(self) -> None:
        if self.rodando:
            self._terminar("Cancelado", erro=True)

    def duracao_estimada(self) -> int:
        return len(self._cfg.get("cenas") or []) * (AQUECER_S + PASSADAS * DURACAO_S)

    def _progresso(self, dados: dict) -> None:
        self._vivo = {"cena": dados.get("cena", ""), "fase": dados.get("fase", ""), "passada": dados.get("passada", 0),
                      "passadas": dados.get("passadas", PASSADAS), "fps": round(float(dados.get("fps") or 0), 1),
                      "pct": float(dados.get("pct") or 0)}

    def _resultado(self, cena: str, dados: dict) -> None:
        try:
            r = resumir(cena, dados)
        except (ValueError, TypeError, KeyError) as e:
            log.warning("resultado do benchmark da placa inválido: %s", e)
            return
        r["temp_max"] = self._temp_max
        r["comparacao"] = comparar(r, historico())
        self._resultados.append(r)
        _salvar({k: v for k, v in r.items() if k != "comparacao"})

    def _terminar(self, mensagem: str, erro: bool = False) -> None:
        with self._trava:
            if not self.rodando:
                return
            self.rodando = False
        self._mensagem, self._erro = mensagem, erro and not (mensagem == "Concluído")
        _manter_acordado(False)
        janela, self._janela = self._janela, None
        if janela:
            self._fechando = True
            threading.Thread(target=self._destruir, args=(janela,), daemon=True).start()
        if self._notificar and self._resultados and not erro:
            try:
                partes = [f"{r['nome']}: {r['fps']:.0f} FPS ({r['pontos']} pontos)".replace(".", ",") for r in self._resultados]
                self._notificar("Benchmark da placa de vídeo terminado", " · ".join(partes))
            except Exception:
                log.exception("falha ao notificar")

    @staticmethod
    def _destruir(janela) -> None:
        try:
            janela.destroy()
        except Exception:
            pass

    def _vigiar(self) -> None:
        s = sensores.instancia()
        limite_tempo = self.duracao_estimada() + 240  # compilação dos shaders e preparo da cena
        while self.rodando:
            try:
                t = s.leitura(False)["resumo"].get("gpu_temp")
            except Exception:
                t = None
            if t is not None:
                self._temp_max = max(self._temp_max or 0.0, float(t))
                if t >= LIMITE_TEMP:
                    self._terminar(f"Parado: placa de vídeo a {t:.0f} °C (limite de {LIMITE_TEMP:.0f} °C)", erro=True)
                    return
            if time.time() - self._inicio > limite_tempo:
                self._terminar("Parado: o teste passou muito do tempo previsto (a janela pode ter travado)", erro=True)
                return
            time.sleep(1)

    def estado(self) -> dict:
        h = historico()
        return {"rodando": self.rodando, "vivo": self._vivo, "resultados": self._resultados, "mensagem": self._mensagem,
                "erro": self._erro, "temp_max": self._temp_max, "duracao": self.duracao_estimada() if self.rodando else None,
                "historico": list(reversed(h[-20:])), "resolucoes": list(RESOLUCOES), "qualidades": list(QUALIDADES),
                "aquecer": AQUECER_S, "passadas": PASSADAS, "duracao_passada": DURACAO_S}
