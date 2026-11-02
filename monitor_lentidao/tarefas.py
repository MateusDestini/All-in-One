"""Tarefas demoradas (testes de rede, disco...) rodando numa thread, com progresso para o painel."""

import logging
import threading

log = logging.getLogger(__name__)


class Tarefa:
    def __init__(self, nome: str):
        self.nome = nome
        self.rodando = False
        self.progresso = 0.0
        self.etapa = ""
        self.resultado = None
        self.erro = ""

    def iniciar(self, funcao, *args) -> bool:
        """`funcao(progresso, *args)`, onde progresso(fracao, etapa)."""
        if self.rodando:
            return False
        self.rodando, self.progresso, self.etapa, self.erro = True, 0.0, "Preparando...", ""
        threading.Thread(target=self._rodar, args=(funcao, args), name=f"tarefa-{self.nome}", daemon=True).start()
        return True

    def _rodar(self, funcao, args):
        try:
            self.resultado = funcao(self._avancar, *args)
        except Exception as e:
            log.exception("falha na tarefa %s", self.nome)
            self.erro = f"Não foi possível concluir: {e}"
        finally:
            self.rodando = False

    def _avancar(self, fracao: float, etapa: str = ""):
        self.progresso = max(0.0, min(1.0, fracao))
        if etapa:
            self.etapa = etapa

    def estado(self) -> dict:
        return {"rodando": self.rodando, "progresso": round(self.progresso, 3), "etapa": self.etapa,
                "erro": self.erro, "resultado": self.resultado}
