"""Janelas do painel.

`JanelaWeb` é a interface moderna (página HTML numa janela WebView2, que já vem no
Windows 11). Se a pywebview não estiver disponível, `JanelaTk` é usada como reserva.
Os métodos `mostrar`, `chamar` e `sair` podem ser chamados de qualquer thread.
"""

import logging
import queue
import threading

from .painel_web import Api, html_pagina

log = logging.getLogger(__name__)


class JanelaWeb:
    def __init__(self, app):
        import webview
        self._webview = webview
        self._app = app
        self._saindo = False
        self._janela = None
        self._pedido = threading.Event()
        self._descarregar: threading.Timer | None = None
        self._descarregada = False
        self._api = Api(app.monitor, app.cfg, app.historico, app.verificar_agora, app.sair, app.notificar)

    def executar(self, mostrar: bool) -> None:
        if not mostrar:
            # Aberto escondido (junto com o Windows): o WebView2 só é criado quando alguém abre o painel.
            self._api._janela_visivel(False)
            self._pedido.wait()
            if self._saindo:
                return
        self._api._janela_visivel(True)
        self._janela = self._webview.create_window(
            "Destini · Monitor de Lentidão", html=html_pagina(), js_api=self._api,
            width=1200, height=820, min_size=(960, 640), background_color="#000000")
        self._janela.events.closing += self._fechando
        self._webview.start()

    def _fechando(self):
        if self._saindo:
            return True
        # Fechar só esconde: o monitor continua na bandeja.
        self._api._janela_visivel(False)
        threading.Thread(target=self._janela.hide, daemon=True).start()
        self._agendar_descarga()
        self._app.avisar_bandeja()
        return False

    SEGUNDOS_ATE_DESCARREGAR = 60
    PAGINA_VAZIA = "<html><body style='background:#000'></body></html>"

    def _agendar_descarga(self) -> None:
        """Escondido por 1 minuto: troca o painel por uma página vazia. O WebView2 libera a memória da página
        e os timers do painel param de chamar o monitor. Ao abrir de novo, o painel é recarregado."""
        if self._descarregar:
            self._descarregar.cancel()
        self._descarregar = threading.Timer(self.SEGUNDOS_ATE_DESCARREGAR, self._descarregar_pagina)
        self._descarregar.daemon = True
        self._descarregar.start()

    def _descarregar_pagina(self) -> None:
        if self._saindo or self._descarregada or getattr(self._api, "_visivel", False) or self._api._precisa_desempenho():
            if not self._saindo and not self._descarregada and not getattr(self._api, "_visivel", False):
                self._agendar_descarga()  # teste rodando: tenta de novo depois
            return
        try:
            self._janela.load_html(self.PAGINA_VAZIA)
            self._descarregada = True
            log.info("painel descarregado da memória (escondido)")
        except Exception:
            log.exception("falha ao descarregar o painel")

    def mostrar(self) -> None:
        if self._janela is None:
            self._pedido.set()  # primeira abertura: o executar() cria a janela
            return
        if self._descarregar:
            self._descarregar.cancel()
        self._api._janela_visivel(True)
        if self._descarregada:
            self._descarregada = False
            try:
                self._janela.load_html(html_pagina())
            except Exception:
                log.exception("falha ao recarregar o painel")
        self._janela.show()
        self._janela.restore()

    def chamar(self, acao) -> None:
        acao()

    def sair(self) -> None:
        self._saindo = True
        try:
            self._api._encerrar()
        except Exception:
            log.exception("falha ao encerrar os testes")
        if self._descarregar:
            self._descarregar.cancel()
        if self._janela:
            self._janela.destroy()
        else:
            self._pedido.set()


class JanelaTk:
    def __init__(self, app):
        import tkinter as tk
        from .painel import Painel
        self._app = app
        self._fila: queue.Queue = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        try:
            from PIL import ImageTk
            from .bandeja import desenhar_icone
            self._foto = ImageTk.PhotoImage(desenhar_icone("ok"))
            self.root.iconphoto(True, self._foto)
        except (tk.TclError, ImportError):
            pass
        self.painel = Painel(self.root, app.monitor, app.cfg, app.historico, app.verificar_agora)
        self.root.protocol("WM_DELETE_WINDOW", self._esconder)

    def executar(self, mostrar: bool) -> None:
        self.root.after(200, self._processar_fila)
        if mostrar:
            self._mostrar()
        self.root.mainloop()

    def _processar_fila(self) -> None:
        while True:
            try:
                acao = self._fila.get_nowait()
            except queue.Empty:
                break
            acao()
        self.root.after(200, self._processar_fila)

    def _mostrar(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.root.after(50, self.painel.atualizar_agora)

    def _esconder(self) -> None:
        self.root.withdraw()
        self._app.avisar_bandeja()

    def mostrar(self) -> None:
        self._fila.put(self._mostrar)

    def chamar(self, acao) -> None:
        self._fila.put(acao)

    def sair(self) -> None:
        self._fila.put(self.root.destroy)


def criar_janela(app):
    try:
        return JanelaWeb(app)
    except ImportError:
        log.warning("pywebview indisponível; usando o painel simples")
        return JanelaTk(app)
