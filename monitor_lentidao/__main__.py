"""Ponto de entrada.

    python -m monitor_lentidao                 abre o painel e fica na bandeja monitorando
    python -m monitor_lentidao --bandeja       começa só na bandeja (usado ao iniciar com o Windows)
    python -m monitor_lentidao --diagnostico   faz um diagnóstico único no console
    python -m monitor_lentidao --sensores      (interno) processo que lê os sensores e escreve JSON
    python -m monitor_lentidao --aplicar-perfil-gpu  (interno) aplica o perfil da placa no logon e sai
"""

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from . import __version__, config, windows


def _configurar_log(console: bool, arquivo: str = "monitor.log") -> None:
    handlers = [RotatingFileHandler(config.pasta_dados() / arquivo, maxBytes=1_000_000,
                                    backupCount=2, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def diagnostico(cfg: dict, segundos: int) -> None:
    from . import relatorio
    from .verificacoes import Monitor

    # Janela menor para o diagnóstico rápido conseguir avaliar médias.
    cfg = dict(cfg, janela_media_s=segundos, intervalo_amostra_s=min(cfg["intervalo_amostra_s"], 3))
    monitor = Monitor(cfg)
    print(f"Coletando dados por {segundos} s e verificando o sistema...")
    monitor.verificacao_pesada()
    fim = time.time() + segundos + cfg["intervalo_amostra_s"]
    while time.time() < fim:
        monitor.amostrar()
        time.sleep(cfg["intervalo_amostra_s"])
    monitor.amostrar()
    print(relatorio.gerar(monitor))


def main() -> None:
    parser = argparse.ArgumentParser(prog="MonitorLentidao", description="Monitor de lentidão do Windows")
    parser.add_argument("--diagnostico", action="store_true", help="faz um diagnóstico único e sai")
    parser.add_argument("--bandeja", action="store_true", help="inicia minimizado na bandeja")
    parser.add_argument("--aguardar", action="store_true", help=argparse.SUPPRESS)  # reaberto como administrador
    parser.add_argument("--sensores", action="store_true", help=argparse.SUPPRESS)  # processo de sensores
    parser.add_argument("--aplicar-perfil-gpu", action="store_true", help=argparse.SUPPRESS)  # tarefa do logon
    parser.add_argument("--ram-auto", action="store_true", help=argparse.SUPPRESS)  # tarefa do auto-tune da RAM
    parser.add_argument("--segundos", type=int, default=60, help="duração da coleta no diagnóstico")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args()

    if args.sensores:
        _configurar_log(console=False, arquivo="sensores.log")
        from . import sensores
        sensores.servidor()
        return

    if args.aplicar_perfil_gpu:
        _configurar_log(console=False, arquivo="perfil-gpu.log")
        from . import oc
        try:
            oc.aplicar_no_inicio()
        except Exception:
            logging.exception("falha ao aplicar o perfil da placa de vídeo no logon")
        return

    _configurar_log(console=args.diagnostico)
    cfg = config.carregar()

    if args.diagnostico:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        diagnostico(cfg, args.segundos)
        return

    if args.bandeja and not args.ram_auto:
        from . import ram_auto
        if ram_auto.deve_ceder_ao_iniciar():
            # auto-tune da RAM em andamento: quem continua é a cópia administradora aberta pela tarefa de logon
            logging.info("auto-tune da RAM em andamento: deixando a tarefa de logon abrir o monitor como administrador")
            return

    if not (windows.aguardar_instancia(60) if (args.aguardar or args.ram_auto) else windows.instancia_unica()):
        if args.ram_auto:
            logging.warning("auto-tune da RAM: outra cópia do monitor (sem administrador?) está aberta")
            return
        windows.mensagem("Monitor de Lentidão", "O monitor já está rodando. Procure o ícone perto do relógio.")
        return

    windows.ativar_dpi()
    from .bandeja import App
    logging.info("Monitor de Lentidão %s iniciado", __version__)
    App(cfg).executar(mostrar_janela=not args.bandeja)
    logging.shutdown()
    os._exit(0)  # encerra também a thread interna do ícone da bandeja


if __name__ == "__main__":
    main()
