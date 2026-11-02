"""Ícone na bandeja do sistema, notificações e laço principal."""

import logging
import threading
import time
from datetime import datetime

from pathlib import Path

import pystray

from PIL import Image, ImageDraw

from . import atualizador, config, dispositivos, oc, oc_cpu, overlay, relatorio, windows
from .janelas import JanelaTk, criar_janela
from .verificacoes import CRITICO, Monitor, Problema

log = logging.getLogger(__name__)

CORES = {"ok": (46, 160, 67), "aviso": (230, 170, 0), "critico": (210, 40, 40)}


ICONE_MARCA = Path(__file__).resolve().parent / "ui" / "marca" / "icone-64.png"


def desenhar_icone(estado: str) -> Image.Image:
    """Logo Destini; com aviso ou problema grave ganha um ponto amarelo ou vermelho no canto."""
    try:
        img = Image.open(ICONE_MARCA).convert("RGBA").resize((64, 64), Image.LANCZOS)
    except OSError:
        return _icone_simples(estado)
    if estado != "ok":
        d = ImageDraw.Draw(img)
        d.ellipse((36, 36, 63, 63), fill=CORES[estado], outline=(0, 0, 0), width=4)
    return img


def _icone_simples(estado: str) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=CORES[estado], outline=(255, 255, 255), width=3)
    if estado == "ok":
        d.line((19, 33, 28, 42, 45, 22), fill="white", width=6)
    else:
        d.rectangle((29, 14, 35, 38), fill="white")
        d.rectangle((29, 44, 35, 50), fill="white")
    return img


class App:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.dados = config.pasta_dados()
        self.historico = self.dados / "alertas.log"
        self.monitor = Monitor(cfg)
        self.parar = threading.Event()
        self.pausado_ate = 0.0
        self.notificados: dict[str, tuple[float, str]] = {}  # chave -> (quando, assinatura)
        self.estado = "ok"
        self._avisou_bandeja = False
        self.janela = criar_janela(self)
        self.vigia = dispositivos.Vigia(self._aviso_dispositivo)

        self.icone = pystray.Icon(
            windows.NOME_APP, desenhar_icone("ok"), "Destini · Monitor de Lentidão: iniciando...",
            menu=pystray.Menu(
                pystray.MenuItem("Abrir painel", lambda: self.janela.mostrar(), default=True),
                pystray.MenuItem("Verificar tudo agora", self.verificar_agora),
                pystray.MenuItem("Exportar relatório em texto",
                                 lambda: self.janela.chamar(self.exportar_relatorio)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Notificações", pystray.Menu(
                    *(pystray.MenuItem(rotulo, self._definir_notificacoes(modo), radio=True,
                                       checked=lambda _, m=modo: config.preferencias()["notificacoes"] == m)
                      for modo, rotulo in (("todas", "Todas"), ("graves", "Só problemas graves"),
                                           ("nenhuma", "Desligadas"))))),
                pystray.MenuItem("Pausar alertas por 1 hora", self.alternar_pausa,
                                 checked=lambda _: time.time() < self.pausado_ate),
                pystray.MenuItem("Overlay de FPS (Ctrl+Shift+O)", self.alternar_overlay,
                                 checked=lambda _: overlay.controle().ligado),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Sair", self.sair),
            ),
        )

    # ------------------------------------------------------------ ciclo

    def executar(self, mostrar_janela: bool = True) -> None:
        try:
            self.icone.run_detached()
            self.icone.visible = True
        except NotImplementedError:  # backend sem bandeja (testes)
            pass
        threading.Thread(target=self._laco, name="monitor", daemon=True).start()
        self.vigia.iniciar()
        try:
            atualizador.ao_abrir(self.notificar, self.sair, bandeja=not mostrar_janela)
        except Exception:
            log.exception("falha ao conferir atualização")
        try:
            overlay.controle().ao_mudar(lambda _: self._atualizar_menu())
            overlay.controle().iniciar()  # registra Ctrl+Shift+O; o overlay só aparece quando pedido
        except Exception:
            log.exception("falha ao preparar o overlay")
        try:
            self.janela.executar(mostrar_janela)
        except Exception:
            if isinstance(self.janela, JanelaTk):
                raise
            log.exception("painel moderno falhou; usando o painel simples")
            self.janela = JanelaTk(self)
            self.janela.executar(mostrar_janela)

    def _modo_leve(self) -> None:
        """Em segundo plano: prioridade baixa, EcoQoS e memória devolvida ao Windows a cada 10 minutos."""
        api = getattr(self.janela, "_api", None)
        leve = not (api and api._precisa_desempenho())
        if leve != self._leve:
            self._leve = leve
            windows.modo_economico(leve)
            if leve:
                self._ultimo_aparo = 0.0
        if leve and time.time() - self._ultimo_aparo >= 600:
            self._ultimo_aparo = time.time()
            windows.aparar_memoria()

    def _perfil_gpu(self) -> None:
        """Nos primeiros 15 minutos: avisa o que aconteceu com os perfis aplicados no logon e com o ajuste automático
        do processador, se o PC reiniciou no meio dele."""
        if time.time() - self._inicio > 900:
            return
        if not getattr(self, "_queda_cpu_vista", False):
            self._queda_cpu_vista = True
            texto = oc_cpu.verificar_queda()
            if texto:
                self.notificar("Ajuste automático do processador", texto + " Ele está na aba Processador do painel.")
        r = oc.resultado_inicio()
        if r:
            self.notificar("Overclock aplicado ao iniciar", r["texto"])

    def _laco(self) -> None:
        intervalo = self.cfg["intervalo_amostra_s"]
        self._leve, self._ultimo_aparo = None, 0.0
        self._inicio = time.time()
        while not self.parar.is_set():
            try:
                self._perfil_gpu()
            except Exception:
                log.exception("falha ao ler o resultado do perfil da placa")
            try:
                self._modo_leve()
            except Exception:
                log.exception("falha ao ajustar o modo leve")
            try:
                self.monitor.amostrar()
                if self.monitor.verificacao_pesada_pendente():
                    self.monitor.pesada_rodando = True
                    threading.Thread(target=self.monitor.verificacao_pesada, daemon=True).start()
                self._atualizar()
            except Exception:
                log.exception("erro no ciclo de monitoramento")
            self.parar.wait(intervalo)

    def _atualizar(self) -> None:
        problemas = self.monitor.problemas()
        estado = ("critico" if any(p.severidade == CRITICO for p in problemas)
                  else "aviso" if problemas else "ok")
        if estado != self.estado:
            self.estado = estado
            self.icone.icon = desenhar_icone(estado)
        if problemas:
            titulo = f"Monitor de Lentidão: {len(problemas)} problema(s) - {problemas[0].titulo}"
        else:
            titulo = "Monitor de Lentidão: tudo certo"
        self.icone.title = titulo[:127]
        self._notificar(problemas)

    def _deve_notificar(self, p: Problema, agora: float) -> bool:
        anterior = self.notificados.get(p.chave)
        if anterior is None:
            return True
        quando, assinatura = anterior
        if p.assinatura != assinatura:
            return True
        intervalo = self.cfg["silenciar_repeticao_min"] if p.renotificar_min is None else p.renotificar_min
        return intervalo > 0 and agora - quando >= intervalo * 60

    def _notificar(self, problemas: list[Problema]) -> None:
        agora = time.time()
        novos = [p for p in problemas if self._deve_notificar(p, agora)]
        if not novos:
            return
        for p in novos:
            self.notificados[p.chave] = (agora, p.assinatura)
        self._registrar(novos)  # o histórico é sempre gravado, mesmo sem notificação
        modo = config.preferencias()["notificacoes"]
        if agora < self.pausado_ate or modo == "nenhuma":
            return
        if modo == "graves":
            novos = [p for p in novos if p.severidade == CRITICO]
            if not novos:
                return
        if len(novos) == 1:
            titulo, texto = novos[0].titulo, novos[0].detalhe
        else:
            titulo = f"{len(novos)} possíveis causas de lentidão"
            texto = "\n".join(f"• {p.titulo}" for p in novos[:4])
            if len(novos) > 4:
                texto += f"\n• e mais {len(novos) - 4}"
        try:
            self.icone.notify(texto[:250] + "\nClique no ícone para ver o diagnóstico.", titulo[:63])
        except Exception:
            log.exception("falha ao mostrar notificação")

    def _aviso_dispositivo(self, titulo: str, texto: str) -> None:
        """Pendrive/USB conectado: aviso informativo, só com as notificações em "Todas"."""
        p = config.preferencias()
        if not p.get("avisos_usb", True) or p["notificacoes"] != "todas" or time.time() < self.pausado_ate:
            return
        try:
            self.icone.notify(texto[:250], titulo[:63])
        except Exception:
            log.exception("falha ao mostrar aviso de dispositivo")

    def notificar(self, titulo: str, texto: str) -> None:
        """Aviso de um teste (estresse, gargalo): só não aparece com as notificações desligadas."""
        if config.preferencias()["notificacoes"] == "nenhuma":
            return
        try:
            self.icone.notify(texto[:250], titulo[:63])
        except Exception:
            log.exception("falha ao mostrar notificação")

    def _registrar(self, problemas: list[Problema]) -> None:
        carimbo = f"{datetime.now():%d/%m %H:%M}"
        with self.historico.open("a", encoding="utf-8") as f:
            for p in problemas:
                f.write(f"{carimbo} [{p.severidade.upper()}] {p.titulo} - {p.detalhe}\n")

    # ------------------------------------------------------------ janela e menu

    def avisar_bandeja(self) -> None:
        if self._avisou_bandeja or config.preferencias()["notificacoes"] == "nenhuma":
            return
        self._avisou_bandeja = True
        try:
            self.icone.notify("O monitor continua funcionando aqui. Clique duas vezes no ícone para abrir o "
                              "painel; para encerrar, use Sair no menu.", "Monitor de Lentidão")
        except Exception:
            pass

    def exportar_relatorio(self) -> None:
        arquivo = self.dados / "diagnostico.txt"
        arquivo.write_text(relatorio.gerar(self.monitor, self.historico), encoding="utf-8")
        windows.abrir(arquivo)

    def verificar_agora(self, *_):
        if not self.monitor.pesada_rodando:
            self.monitor.pesada_rodando = True
            threading.Thread(target=self.monitor.verificacao_pesada, kwargs={"tudo": True}, daemon=True).start()

    def _definir_notificacoes(self, modo: str):
        return lambda: config.definir_preferencia("notificacoes", modo)

    def alternar_pausa(self, *_):
        self.pausado_ate = 0.0 if time.time() < self.pausado_ate else time.time() + 3600

    def alternar_overlay(self, *_):
        c = overlay.controle()
        threading.Thread(target=c.ligar, args=(not c.ligado,), daemon=True).start()

    def _atualizar_menu(self) -> None:
        try:
            self.icone.update_menu()
        except Exception:
            pass

    def sair(self, *_) -> None:
        self.parar.set()
        try:
            overlay.controle().encerrar()
        except Exception:
            log.exception("falha ao fechar o overlay")
        try:
            self.icone.stop()
        except Exception:
            pass
        self.janela.sair()
