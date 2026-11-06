"""Painel moderno: página HTML exibida numa janela nativa (WebView2 do Windows 11).

A página chama os métodos de `Api` pelo objeto `window.pywebview.api` do JavaScript.
"""

import logging
import os
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import psutil

from . import (aod, apps, atualizador, bench_gpu, benchmark, biblioteca, config, discos, dispositivos, drivers, energia,
               estresse, gargalo, gpu, inicializacao, maker, memoria, oc, oc_cpu, otimizacao, overlay, quadros, ram_auto, rede,
               relatorio, relatorio_html, rgb, saude, sensores, sistema, windows, windhawk, ram_timings)
from .tarefas import Tarefa
from .programas import DICAS_PROGRAMAS, NOMES_DE_SISTEMA
from .verificacoes import CRITICO, Monitor, Programa

log = logging.getLogger(__name__)

import sys as _sys
if getattr(_sys, 'frozen', False) and hasattr(_sys, '_MEIPASS'):
    PAGINA = Path(_sys._MEIPASS) / 'monitor_lentidao' / 'ui' / 'painel.html'
else:
    PAGINA = Path(__file__).resolve().parent / 'ui' / 'painel.html'
PASTA_MARCA = PAGINA.parent / "marca"  # works for both dev and frozen


def html_pagina() -> str:
    """painel.html com as imagens da marca ("asset:arquivo") embutidas, porque a página é carregada como texto."""
    import base64
    import re

    def embutir(m):
        arq = PASTA_MARCA / m.group(1)
        tipo = {".svg": "image/svg+xml", ".png": "image/png"}.get(arq.suffix.lower(), "application/octet-stream")
        try:
            return f'"data:{tipo};base64,{base64.b64encode(arq.read_bytes()).decode()}"'
        except OSError:
            return '""'
    from . import __version__
    versao = __version__ + (f" (build {atualizador.BUILD})" if atualizador.BUILD else "")
    html = PAGINA.read_text(encoding="utf-8").replace("{{VERSAO}}", versao)
    return re.sub(r'"asset:([\w.-]+)"', embutir, html)
NAO_FECHAR = NOMES_DE_SISTEMA | {"explorer.exe", "dwm.exe", "system", "msmpeng.exe", "monitorlentidao.exe"}


def _nivel(valor: float, aviso: float, critico: float) -> str:
    return "critico" if valor >= critico else "aviso" if valor >= aviso else "ok"


def impacto(p: Programa, cfg: dict) -> float:
    """1.0 = atingiu o limite de alerta em CPU, memória ou disco."""
    return max(p.cpu / cfg["programa_cpu_alerta"], p.ram / (cfg["programa_ram_alerta_mb"] * 2**20),
               p.disco / (cfg["programa_disco_alerta_mb_s"] * 2**20))


def nota_saude(problemas) -> int:
    criticos = sum(p.severidade == CRITICO for p in problemas)
    return max(0, 100 - 20 * criticos - 7 * (len(problemas) - criticos))


def dados_painel(monitor: Monitor, cfg: dict, historico: Path, inicio: float) -> dict:
    r = monitor.resumo_atual()
    problemas = monitor.problemas()
    criticos = sum(p.severidade == CRITICO for p in problemas)
    coletando = time.time() - inicio < cfg["janela_media_s"] * 0.8
    if criticos:
        nivel = "critico"
        titulo = "Encontrei um problema sério" if criticos == 1 else f"Encontrei {criticos} problemas sérios"
    elif problemas:
        nivel = "aviso"
        titulo = ("1 coisa pode estar deixando seu PC lento" if len(problemas) == 1
                  else f"{len(problemas)} coisas podem estar deixando seu PC lento")
    else:
        nivel = "ok"
        titulo = "Analisando seu PC..." if coletando else "Tudo certo com seu PC"

    medidores = []
    u = r["ultima"]
    if u:
        ram = r["ram"]
        medidores.append({"id": "cpu", "titulo": "Processador", "valor": round(u.cpu), "unidade": "%",
                          "pct": u.cpu, "nivel": _nivel(u.cpu, 70, cfg["cpu_total_alerta"]),
                          "sub": f"{r['cpus']} núcleos" + (f" · velocidade {u.desempenho_cpu:.0f}%"
                                                         if u.desempenho_cpu is not None else "")})
        medidores.append({"id": "ram", "titulo": "Memória", "valor": round(u.ram), "unidade": "%",
                          "pct": u.ram, "nivel": _nivel(u.ram, 80, cfg["ram_alerta"]),
                          "sub": f"{ram.used / 2**30:.1f} de {ram.total / 2**30:.1f} GB".replace(".", ",")})
        if u.discos:
            nome, (ocupado, lat) = max(u.discos.items(), key=lambda kv: kv[1][0])
            medidores.append({"id": "disco", "titulo": "Disco", "valor": round(ocupado), "unidade": "%",
                              "pct": ocupado, "nivel": _nivel(ocupado, 70, cfg["disco_ocupado_alerta"]),
                              "sub": f"{nome} · {lat * 1000:.0f} ms por operação"})
    sistema = os.environ.get("SystemDrive", "C:").upper()
    particoes = r["particoes"]
    alvo = next((p for p in particoes if p[0].upper().startswith(sistema)), particoes[0] if particoes else None)
    if alvo:
        ponto, uso = alvo
        minimo = cfg["espaco_livre_min_pct"]
        medidores.append({"id": "espaco", "titulo": "Espaço livre", "valor": f"{uso.free / 2**30:.0f}",
                          "unidade": "GB", "pct": uso.percent,
                          "nivel": _nivel(uso.percent, 100 - 2 * minimo, 100 - minimo),
                          "sub": f"em {ponto.rstrip(chr(92))} · {100 - uso.percent:.0f}% livre"})

    programas = sorted(r["programas"].values(), key=lambda p: impacto(p, cfg), reverse=True)[:80]
    alertas = []
    if historico.exists():
        for linha in reversed(historico.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]):
            quando, _, resto = linha.partition(" [")
            sev, _, texto = resto.partition("] ")
            titulo_alerta, _, detalhe = texto.partition(" - ")
            alertas.append({"quando": quando, "nivel": "critico" if sev == "CRITICO" else "aviso",
                            "titulo": titulo_alerta, "detalhe": detalhe})

    ultima = (datetime.fromtimestamp(monitor.ultima_pesada).strftime("%H:%M")
              if monitor.ultima_pesada else None)
    return {
        "nivel": nivel,
        "titulo": titulo,
        "nota": None if coletando and not problemas else nota_saude(problemas),
        "coletando": coletando,
        "verificando": monitor.pesada_rodando,
        "ultima_verificacao": ultima,
        "ligado_ha_h": r["ligado_ha_h"],
        "sistema": r["sistema"],
        "medidores": medidores,
        "problemas": [{"chave": p.chave, "nivel": "critico" if p.severidade == CRITICO else "aviso",
                       "titulo": p.titulo, "detalhe": p.detalhe, "sugestao": p.sugestao} for p in problemas],
        "programas": [{"nome": p.nome, "cpu": round(p.cpu, 1), "ram_mb": round(p.ram / 2**20),
                       "disco_mb": round(p.disco / 2**20, 1), "processos": p.processos,
                       "impacto": round(impacto(p, cfg), 2), "caminho": p.caminho,
                       "dica": DICAS_PROGRAMAS.get(p.nome.lower(), ""),
                       "protegido": p.nome.lower() in NAO_FECHAR} for p in programas],
        "inicializacao": sorted(r["inicializacao"], key=lambda i: (not i["ativo"], i["nome"].lower())),
        "alertas": alertas,
        "reinicios": [{"quando": r["boot"].strftime("%d/%m %H:%M"), "tipo": r["tipo"], "causa": r["causa"],
                       "fazer": r["fazer"], "inesperado": r["inesperado"]} for r in reversed(monitor.reinicios)],
        "autostart": windows.autostart_ativo(),
        "notificacoes": config.preferencias()["notificacoes"],
        "admin": windows.eh_admin(),
        "tema": config.preferencias().get("tema", "escuro"),
        "avisos_usb": config.preferencias().get("avisos_usb", True),
    }


class Api:
    """Métodos chamados pela página. Atributos com _ não são expostos ao JavaScript."""

    def __init__(self, monitor: Monitor, cfg: dict, historico: Path, ao_verificar, ao_sair=None, notificar=None):
        self._ao_sair = ao_sair
        self._notificar = notificar
        self._monitor = monitor
        self._cfg = cfg
        self._historico = historico
        self._ao_verificar = ao_verificar
        self._inicio = time.time()
        self._teste_memoria = memoria.TesteEmSegundoPlano()
        self._tarefas = {n: Tarefa(n) for n in ("rede", "disco", "carregamento", "saude_discos", "energia", "limpeza", "sistema", "gpu",
                                                "apps", "pawnio", "relatorio", "saude", "windhawk", "drivers", "drivers_wu",
                                                "drivers_wu_instalar", "drivers_backup", "drivers_placa",
                                                "maker_portas", "maker_seriais", "maker_acao", "rgb_conectar", "rgb_sdk")}
        self._sensores = sensores.instancia()
        self._render = benchmark.Render()
        self._sim = benchmark.SimJogo()
        self._estresse = estresse.Estresse(self._avisar)
        self._bench_gpu = bench_gpu.BenchGpu(self._avisar)
        self._sessao_gargalo: gargalo.Sessao | None = None
        self._hardware_jogos: dict | None = None

    def _avisar(self, titulo: str, texto: str) -> None:
        if self._notificar:
            self._notificar(titulo, texto)

    def _encerrar(self) -> None:
        """Chamado ao sair do monitor: nada de carga pesada sobrando."""
        self._estresse.parar("Monitor encerrado")
        self._render.cancelar()
        self._sim.cancelar()
        oc_cpu.encerrar()
        ram_timings.encerrar()
        self._bench_gpu.cancelar()
        rgb.encerrar()
        if self._sessao_gargalo:
            self._sessao_gargalo.cancelada = True

    def estado(self):
        return dados_painel(self._monitor, self._cfg, self._historico, self._inicio)

    def verificar(self):
        self._ao_verificar()
        return True

    def exportar(self):
        arquivo = config.pasta_dados() / f"diagnostico_{datetime.now():%Y-%m-%d_%H%M}.txt"
        arquivo.write_text(relatorio.gerar(self._monitor, self._historico), encoding="utf-8")
        windows.abrir(arquivo)
        return str(arquivo)

    def abrir_pasta(self):
        windows.abrir(config.pasta_dados())

    def definir_autostart(self, ativo):
        try:
            windows.definir_autostart(bool(ativo))
        except OSError:
            pass
        return windows.autostart_ativo()

    def iniciar_teste_memoria(self):
        if not memoria.disponivel():
            return {"ok": False, "mensagem": "O componente do teste de memória não foi encontrado."}
        return {"ok": self._teste_memoria.iniciar(), "mensagem": ""}

    def estado_teste_memoria(self):
        return self._teste_memoria.estado()

    def definir_notificacoes(self, modo):
        if modo in ("todas", "graves", "nenhuma"):
            config.definir_preferencia("notificacoes", modo)
        return config.preferencias()["notificacoes"]

    def pesquisar(self, nome):
        webbrowser.open("https://www.google.com/search?q=" + quote_plus(f"o que é {nome} windows"))

    def abrir_local(self, caminho):
        if caminho and windows.EH_WINDOWS:
            windows.executar(["explorer.exe", "/select,", caminho], timeout=5)
            return True
        return False

    def abrir_inicializacao(self):
        if windows.EH_WINDOWS:
            os.startfile("ms-settings:startupapps")

    def fechar_programa(self, nome):
        if str(nome).lower() in NAO_FECHAR:
            return {"ok": False, "mensagem": f"{nome} faz parte do Windows e não deve ser fechado."}
        fechados, negados = 0, 0
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] == nome:
                try:
                    proc.terminate()
                    fechados += 1
                except psutil.AccessDenied:
                    negados += 1
                except psutil.NoSuchProcess:
                    pass
        msg = f"{fechados} processo(s) de {nome} fechados."
        if negados:
            msg += f" {negados} precisam do monitor aberto como administrador."
        return {"ok": fechados > 0, "mensagem": msg}

    # ------------------------------------------------------------ inicialização

    def _recarregar_inicializacao(self):
        self._monitor.inicializacao = inicializacao.listar()

    def inicializacao_ativar(self, item_id, ativo):
        r = inicializacao.definir_ativo(str(item_id), bool(ativo))
        self._recarregar_inicializacao()
        return r

    def inicializacao_remover(self, item_id):
        r = inicializacao.remover(str(item_id))
        self._recarregar_inicializacao()
        return r

    def inicializacao_removidos(self):
        return inicializacao.removidos()

    def inicializacao_restaurar(self, indice):
        r = inicializacao.restaurar(int(indice))
        self._recarregar_inicializacao()
        return r

    # ------------------------------------------------------------ tarefas demoradas (testes)

    def iniciar_tarefa(self, nome, parametro=None):
        funcoes = {
            "rede": (rede.testar,),
            "disco": (discos.testar_velocidade, str(parametro or ""), 1024),
            "carregamento": (discos.testar_carregamento, str(parametro or "")),
            "saude_discos": (lambda progresso: discos.saude(),),
            "energia": (lambda progresso: energia.ler(),),
            "limpeza": (lambda progresso: otimizacao.limpar_memoria(),),
            "sistema": (lambda progresso: sistema.coletar(),),
            "gpu": (lambda progresso: gpu.coletar(),),
            "apps": (apps.instalar, list(parametro or [])),
            "pawnio": (sensores.instalar_pawnio,),
            "windhawk": (windhawk.instalar,),
            "relatorio": (self._gerar_relatorio, str(parametro or "html")),
            "saude": (lambda progresso: saude.checklist(self._sensores.leitura(False)["diagnostico"]),),
            "drivers": (lambda progresso, p: drivers.estado(progresso, bool(p.get("forcar")), bool(p.get("studio"))),
                        dict(parametro or {})),
            "drivers_wu": (drivers.windows_update_buscar,),
            "drivers_wu_instalar": (lambda progresso, ids: drivers.windows_update_instalar(ids, progresso),
                                    [str(i) for i in (parametro or [])]),
            "drivers_backup": (drivers.backup,),
            "drivers_placa": (drivers.instalar_placa_indice, int(parametro or 0)),
            "maker_portas": (maker.portas,),
            "maker_seriais": (maker.seriais,),
            "maker_acao": (self._maker_acao, dict(parametro or {})),
            "rgb_conectar": (lambda progresso: rgb.instancia().conectar(progresso),),
            "rgb_sdk": (rgb.baixar_sdk_corsair,),
        }
        if nome not in funcoes:
            return False
        funcao, *args = funcoes[nome]
        return self._tarefas[nome].iniciar(funcao, *args)

    def estado_tarefa(self, nome):
        t = self._tarefas.get(nome)
        return t.estado() if t else None

    def unidades_disco(self):
        return {"unidades": discos.unidades(), "historico": list(reversed(discos.historico())),
                "carregamento": list(reversed(discos.historico_carregamento()))}

    @staticmethod
    def _maker_acao(progresso, p: dict) -> dict:
        acao = p.get("acao")
        r = {"fantasmas": lambda: maker.remover_fantasmas([str(i) for i in p.get("ids") or []], progresso),
             "liberar": lambda: maker.liberar_numeros(progresso), "desfazer": lambda: maker.desfazer_numeros(progresso),
             "reinstalar": lambda: maker.reinstalar(str(p.get("id") or ""), progresso),
             "remover_pacote": lambda: maker.remover_pacote(str(p.get("publicado") or ""), progresso),
             "restaurar": lambda: maker.restaurar_pacotes(progresso), "ide": lambda: maker.instalar_ide(progresso)}.get(acao)
        if not r:
            raise ValueError("ação desconhecida")
        return {**r(), "acao": acao}

    def rgb_estado(self):
        return rgb.instancia().estado()

    def rgb_configurar(self, cfg):
        c = dict(cfg or {})
        try:
            return {"ok": True, **rgb.instancia().configurar(c.get("efeito"), c.get("cor"), c.get("velocidade"), c.get("desligados"))}
        except ValueError as e:
            return {"ok": False, "mensagem": str(e)}

    def rgb_iniciar(self):
        return rgb.instancia().iniciar()

    def rgb_parar(self):
        return rgb.instancia().parar()

    def rgb_abrir_openrgb(self):
        return rgb.abrir_openrgb()

    def maker_fechar_prendendo(self):
        return maker.fechar_prendendo()

    def maker_limpar_cache(self):
        return maker.limpar_cache_ide()

    def drivers_abrir_backup(self):
        drivers.abrir_pasta_backup()
        return True

    # ------------------------------------------------------------ otimização

    def otimizacao_estado(self):
        return {"ajustes": otimizacao.ajustes(), "apps": otimizacao.apps_segundo_plano(),
                "tem_backup": otimizacao.tem_backup(), "admin": windows.eh_admin(),
                "ram": round(psutil.virtual_memory().percent)}

    def otimizacao_ajuste(self, id_, ligar):
        return otimizacao.definir_ajuste(str(id_), bool(ligar))

    def otimizacao_restaurar(self):
        return otimizacao.restaurar_originais()

    def otimizacao_fechar_apps(self, nomes):
        return otimizacao.fechar_apps(list(nomes or []))

    def reabrir_admin(self):
        if windows.eh_admin():
            return {"ok": False, "mensagem": "O monitor já está aberto como administrador."}
        if not windows.reabrir_como_admin():
            return {"ok": False, "mensagem": "O Windows não abriu como administrador (a confirmação foi recusada?)."}
        if self._ao_sair:
            threading.Timer(0.5, self._ao_sair).start()
        return {"ok": True, "mensagem": "Abrindo como administrador..."}

    # ------------------------------------------------------------ sensores e placa de vídeo

    def _janela_visivel(self, sim: bool) -> None:
        """Chamado pela janela: escondida, não gasta nada lendo sensores."""
        self._visivel = bool(sim)
        self._sensores.permitir(sim)
        if not sim:
            overlay.controle().acompanhar(False)
            quadros.instancia().reter("gargalo-pagina", False)

    def _precisa_desempenho(self) -> bool:
        """Painel aberto ou teste rodando: o monitor sai do modo leve para não atrapalhar a medição."""
        return (getattr(self, "_visivel", False) or benchmark.em_uso() is not None or oc_cpu.em_uso() or ram_timings.em_uso()
                or self._algo_pesado_rodando() is not None or any(t.rodando for t in self._tarefas.values()))

    def sensores(self, com_linhas=True, desde=0):
        r = self._sensores.leitura(bool(com_linhas), int(desde or 0))
        vm = psutil.virtual_memory()
        r["ram"] = {"pct": vm.percent, "usada_gb": vm.used / 2**30, "total_gb": vm.total / 2**30}
        letra = os.environ.get("SystemDrive", "C:") if windows.EH_WINDOWS else "/"
        try:
            uso = psutil.disk_usage(letra + "\\" if windows.EH_WINDOWS else letra)
            info = self._tarefas["sistema"].estado().get("resultado") or {}
            u = next((x for x in info.get("unidades", []) if x["letra"].upper() == letra.upper()), {})
            r["disco"] = {"letra": letra, "pct": uso.percent, "nome": u.get("disco"),
                          "livre": f"{uso.free / 2**30:.0f} GB" if uso.free >= 2**30 else f"{uso.free / 2**20:.0f} MB"}
        except OSError:
            r["disco"] = None
        return r

    def sensores_zerar(self):
        self._sensores.zerar_min_max()

    def sensores_reiniciar(self):
        self._sensores.reiniciar()

    def gpu_ao_vivo(self, indice):
        placas = self._tarefas["gpu"].estado().get("resultado") or []
        i = int(indice or 0)
        if i >= len(placas):
            return {"extras": [], "linhas": []}
        p = placas[i]
        leitura = self._sensores.leitura(True)
        grupo = next((g for g in leitura.get("grupos", []) if g["nome"] == p["nome"]), None)
        if grupo is None:  # nomes diferentes entre o Windows e a biblioteca: usa a placa principal
            grupo = next((g for g in leitura.get("grupos", []) if g["tipo"] == "Placa de vídeo"), None)
        return {"extras": gpu.ao_vivo(p.get("handle")), "linhas": grupo["linhas"] if grupo else []}

    # ------------------------------------------------------------ apps, eventos, tema, relatório

    def apps_catalogo(self):
        return apps.catalogo()

    def apps_abrir_loja(self):
        apps.abrir_loja()

    # ------------------------------------------------------------ Windhawk

    def windhawk_estado(self):
        return windhawk.estado()

    def windhawk_abrir(self):
        return windhawk.abrir()

    def windhawk_alternar(self, mod_id, ligar):
        return windhawk.alternar(str(mod_id), bool(ligar))

    def windhawk_reiniciar(self):
        return windhawk.reiniciar()

    def abrir_site(self, endereco):
        if str(endereco).startswith("https://"):
            webbrowser.open(str(endereco))

    def eventos_recentes(self):
        return list(dispositivos.eventos)

    def definir_tema(self, tema):
        if tema in ("escuro", "claro", "sistema"):
            config.definir_preferencia("tema", tema)
        return config.preferencias().get("tema", "escuro")

    def definir_avisos_usb(self, ligado):
        config.definir_preferencia("avisos_usb", bool(ligado))
        return bool(ligado)

    # ------------------------------------------------------------ atualização pelo GitHub
    def atualizacao_estado(self):
        return atualizador.estado()

    def atualizacao_verificar(self):
        return {**atualizador.verificar(), "instalado": atualizador.instalado(), "pagina": atualizador.PAGINA}

    def atualizacao_instalar(self):
        rel = atualizador.estado().get("nova")
        if not rel:
            return {"ok": False, "mensagem": "Não há versão nova para instalar."}
        if (benchmark.em_uso() is not None or oc_cpu.em_uso() or ram_timings.em_uso() or self._algo_pesado_rodando() is not None):
            return {"ok": False, "mensagem": "Espere o teste que está rodando terminar."}
        try:
            r = atualizador.instalar(rel, bandeja=False)
        except Exception as e:
            log.exception("atualização falhou")
            return {"ok": False, "mensagem": f"A atualização falhou: {e}"}
        if r.get("ok") and self._ao_sair:
            threading.Timer(1.5, self._ao_sair).start()
        return r

    def definir_atualizar_sozinho(self, ligado):
        config.definir_preferencia("atualizar_sozinho", bool(ligado))
        return bool(ligado)

    def abrir_pagina_versoes(self):
        webbrowser.open(atualizador.PAGINA)

    def _gerar_relatorio(self, progresso, formato: str) -> dict:
        def resultado(nome, funcao):
            r = self._tarefas[nome].estado()
            if r.get("resultado") is not None:
                return r["resultado"]
            try:
                return funcao()
            except Exception:
                return None
        progresso(0.1, "Lendo o sistema...")
        dados = {"problemas": dados_painel(self._monitor, self._cfg, self._historico, self._inicio)["problemas"],
                 "sistema": resultado("sistema", sistema.coletar)}
        progresso(0.35, "Lendo a placa de vídeo...")
        dados["gpus"] = [{k: v for k, v in g.items() if k != "handle"} for g in resultado("gpu", gpu.coletar) or []]
        progresso(0.55, "Lendo a saúde dos discos...")
        dados["discos"] = resultado("saude_discos", discos.saude)
        progresso(0.75, "Lendo energia e sensores...")
        dados["energia"] = resultado("energia", energia.ler)
        dados["rede"] = self._tarefas["rede"].estado().get("resultado")
        dados["sensores"] = self._sensores.snapshot()
        dados["testes"] = relatorio_html.testes_recentes(list(reversed(memoria.historico()))[:5],
                                                         list(reversed(discos.historico()))[:5],
                                                         list(reversed(benchmark.historico()))[:6])
        pasta = config.pasta_dados()
        if formato == "json":
            arquivo = pasta / "relatorio.json"
            arquivo.write_text(relatorio_html.json_(dados), encoding="utf-8")
        else:
            arquivo = pasta / "relatorio.html"
            arquivo.write_text(relatorio_html.html(dados), encoding="utf-8")
        windows.abrir(arquivo)
        return {"arquivo": str(arquivo)}

    # ------------------------------------------------------------ benchmark, estresse, gargalo e FPS

    def _algo_pesado_rodando(self) -> str | None:
        if self._teste_memoria.rodando:
            return "o teste de memória"
        if self._tarefas["disco"].rodando:
            return "o teste de disco"
        if self._bench_gpu.rodando:
            return "o benchmark da placa de vídeo"
        return None

    def bench_iniciar(self, modo="ambos", sustentado=False):
        outro = self._algo_pesado_rodando()
        if outro:
            return {"ok": False, "mensagem": f"Espere terminar {outro}."}
        return self._render.iniciar(str(modo or "ambos"), bool(sustentado))

    def bench_cancelar(self):
        self._render.cancelar()

    def bench_estado(self, com_imagem=True):
        return self._render.estado(bool(com_imagem))

    def sim_iniciar(self, segundos=20):
        outro = self._algo_pesado_rodando()
        if outro:
            return {"ok": False, "mensagem": f"Espere terminar {outro}."}
        return self._sim.iniciar(int(segundos or 20))

    def sim_cancelar(self):
        self._sim.cancelar()

    def sim_estado(self):
        return self._sim.estado()

    def bench_gpu_iniciar(self, cenas=None, resolucao="1080p", qualidade="alta"):
        outro = self._algo_pesado_rodando()
        if outro:
            return {"ok": False, "mensagem": f"Espere terminar {outro}."}
        if self._estresse.rodando or self._render.rodando:
            return {"ok": False, "mensagem": "Pare o teste de estresse ou o benchmark do processador antes."}
        return self._bench_gpu.iniciar(list(cenas or ["jogo", "rt"]), str(resolucao), str(qualidade))

    def bench_gpu_cancelar(self):
        self._bench_gpu.cancelar()
        return True

    def bench_gpu_estado(self):
        return self._bench_gpu.estado()

    def bench_gpu_apagar_historico(self):
        bench_gpu.apagar_historico()
        return True

    def estresse_preparar(self):
        return self._estresse.preparar()

    def estresse_iniciar(self, cfg):
        outro = self._algo_pesado_rodando()
        if outro:
            return {"ok": False, "mensagem": f"Espere terminar {outro}."}
        return self._estresse.iniciar(dict(cfg or {}))

    def estresse_parar(self):
        threading.Thread(target=self._estresse.parar, daemon=True).start()
        return True

    def estresse_estado(self):
        return self._estresse.estado()

    def desempenho_selos(self):
        """Estado leve para os selos do menu (sem ler sensores)."""
        s = self._sessao_gargalo
        return {"render": self._render.rodando, "sim": self._sim.rodando, "estresse": self._estresse.rodando,
                "estresse_alerta": self._estresse.estado_curto(), "gargalo": bool(s and s.estado()["rodando"]),
                "overlay": overlay.controle().ligado, "cpu": oc_cpu.em_uso(), "ram": ram_timings.em_uso(),
                "bench_gpu": self._bench_gpu.rodando, "rgb": bool(rgb._instancia and rgb._instancia.rodando)}

    def _hardware(self) -> dict:
        if self._hardware_jogos is None:
            self._hardware_jogos = gargalo.hardware(self._sensores)
        return self._hardware_jogos

    def gargalo_ao_vivo(self):
        quadros.instancia().reter("gargalo-pagina", True)  # FPS do jogo enquanto a página está aberta
        a = gargalo.amostra(self._sensores)
        hw = self._hardware()
        eq = gargalo.equilibrio(hw["idx_gpu"] or 50, hw["idx_cpu"] or 80) if hw["idx_gpu"] or hw["idx_cpu"] else None
        return {"amostra": a, "titulo": gargalo.TITULOS[a["limite"]], "conselho": gargalo.CONSELHOS[a["limite"]],
                "hardware": hw, "equilibrio": eq, "fps_erro": quadros.instancia().erro,
                "sessao": self._sessao_gargalo.estado() if self._sessao_gargalo else None}

    def gargalo_sessao(self, minutos=1):
        s = self._sessao_gargalo
        if s and s.estado()["rodando"]:
            s.cancelada = True
            return {"rodando": False}
        if not minutos:
            return {"rodando": False}

        def terminou(r):
            self._avisar("Análise de gargalo concluída",
                         f"{r['titulo']}. Abra o painel em Gargalo para ver o resultado completo.")
        self._sessao_gargalo = gargalo.Sessao(self._sensores, float(minutos), terminou)
        return {"rodando": True}

    def gargalo_sair(self):
        quadros.instancia().reter("gargalo-pagina", False)

    # ------------------------------------------------------------ abas NVIDIA e AMD (overclock e perfis)
    def oc_presenca(self):
        p = oc.presenca()
        c = oc_cpu.presenca()
        p.update(cpu=c["cpu"], cpu_marca=c["marca"])
        return p

    def oc_estado(self, fabricante):
        return oc.estado(str(fabricante))

    def oc_aplicar(self, fabricante, indice, valores):
        return oc.aplicar(str(fabricante), int(indice or 0), dict(valores or {}))

    def oc_padrao(self, fabricante, indice):
        return oc.padrao(str(fabricante), int(indice or 0))

    def oc_salvar_perfil(self, slot, fabricante, indice, valores, nome=""):
        return oc.salvar_perfil(str(slot), str(fabricante), int(indice or 0), dict(valores or {}), str(nome or ""))

    def oc_aplicar_perfil(self, slot, fabricante="gpu"):
        return oc.aplicar_perfil(str(slot), str(fabricante or "gpu"))

    def oc_apagar_perfil(self, slot, fabricante="gpu"):
        return oc.apagar_perfil(str(slot), str(fabricante or "gpu"))

    def oc_inicio(self, slot, fabricante="gpu"):
        return oc.definir_inicio(str(slot) if slot else None, str(fabricante or "gpu"))

    def oc_limpar_aviso(self, fabricante="gpu"):
        return oc.limpar_aviso(str(fabricante or "gpu"))

    # ------------------------------------------------------------ processador: teste por núcleo e ajuste automático
    def cpu_ajuste_iniciar(self, objetivo, duracao):
        return oc_cpu.iniciar_ajuste(str(objetivo), str(duracao))

    def cpu_ajuste_continuar(self):
        return oc_cpu.continuar_ajuste()

    def cpu_ajuste_cancelar(self):
        return oc_cpu.cancelar_ajuste()

    def cpu_ajuste_descartar(self):
        return oc_cpu.descartar_ajuste()

    def cpu_ajuste_estado(self):
        return {"ajuste": oc_cpu.estado_ajuste(), "teste": oc_cpu.estado_teste()}

    def cpu_teste_iniciar(self, segundos, indices=None):
        return oc_cpu.iniciar_teste(float(segundos or 30), [int(i) for i in indices] if indices else None)

    def cpu_teste_cancelar(self):
        return oc_cpu.cancelar_teste()

    # ------------------------------------------------------------ timings da RAM
    def ram_estado(self, forcar=False):
        return ram_timings.estado(bool(forcar))

    def ram_teste_iniciar(self, minutos, rotulo="", medir=True):
        return ram_timings.iniciar_teste(float(minutos or 15), str(rotulo or ""), bool(medir))

    def ram_teste_cancelar(self):
        return ram_timings.cancelar_teste()

    def ram_teste_estado(self):
        return {"teste": ram_timings.estado_teste(), "melhor": ram_timings.melhor(),
                "historico": list(reversed(ram_timings.historico()))}

    def ram_historico_apagar(self):
        ram_timings.apagar_historico()
        return {"ok": True, "mensagem": "Histórico apagado."}

    # gravação na BIOS pelo Windows (interface AOD da AMD, como o Ryzen Master)
    def ram_bios_estado(self, forcar=False):
        return aod.estado(ram_timings.leitura_atual(), bool(forcar))

    def ram_bios_aplicar(self, mudancas, origem=""):
        if ram_timings.em_uso():
            return {"ok": False, "mensagem": "Espere o teste de estabilidade terminar."}
        atual = ram_timings.leitura_atual()
        return aod.aplicar(dict(mudancas or {}), (atual.get("timings") if atual.get("ok") else None), str(origem or ""))

    def ram_bios_restaurar(self, modo="antes"):
        return aod.restaurar("padrao" if modo == "padrao" else "antes")

    def ram_bios_reiniciar(self):
        return aod.reiniciar_pc()

    # ------------------------------------------------------------ auto-tune de RAM (loop automático via AOD)
    def ram_auto_estado(self):
        return ram_auto.estado()

    def ram_auto_iniciar(self, niveis=None):
        return ram_auto.iniciar(niveis)

    def ram_auto_pausar(self):
        return ram_auto.pausar()

    def ram_auto_cancelar(self):
        return ram_auto.cancelar()

    def ram_auto_continuar(self):
        return ram_auto.continuar()

    def ram_auto_pular(self):
        return ram_auto.pular()

    def ram_auto_relatorio(self):
        return ram_auto.relatorio()

    def oc_erros(self):
        return oc.erros_recentes()

    # ------------------------------------------------------------ biblioteca de jogos (só lê e abre)
    def biblioteca_listar(self, forcar=False):
        try:
            tabela = gargalo.tabela_fps(self._sensores, "1920x1080", "Alto", "Nativo")
        except Exception:
            tabela = None
        return biblioteca.listar(bool(forcar), tabela)

    def biblioteca_rodando(self):
        return biblioteca.rodando()

    def biblioteca_capa(self, chave):
        return biblioteca.capa(str(chave))

    def biblioteca_tamanho(self, chave):
        return biblioteca.tamanho(str(chave))

    def biblioteca_jogar(self, chave):
        return biblioteca.jogar(str(chave))

    def biblioteca_pasta(self, chave):
        return biblioteca.abrir_pasta(str(chave))

    def biblioteca_verificar(self, chave):
        return biblioteca.verificar_arquivos(str(chave))

    def biblioteca_ocultar(self, chave, sim):
        return biblioteca.ocultar(str(chave), bool(sim))

    def biblioteca_remover(self, chave):
        return biblioteca.remover_manual(str(chave))

    def biblioteca_renomear(self, chave, nome):
        return biblioteca.renomear(str(chave), str(nome or ""))

    def biblioteca_adicionar(self):
        """Abre a janela "Abrir arquivo" do Windows para escolher o .exe do jogo."""
        try:
            import webview
            tipo = getattr(getattr(webview, "FileDialog", None), "OPEN", None) or webview.OPEN_DIALOG
            janela = webview.windows[0]
            escolha = janela.create_file_dialog(tipo, allow_multiple=False, file_types=("Programas (*.exe)",))
        except Exception:
            log.exception("falha ao abrir a janela de arquivo")
            return {"ok": False, "erro": "Não consegui abrir a janela para escolher o arquivo."}
        if not escolha:
            return {"ok": False, "cancelado": True}
        caminho = escolha[0] if isinstance(escolha, (list, tuple)) else escolha
        return biblioteca.adicionar(str(caminho))

    # ------------------------------------------------------------ overlay de FPS
    def overlay_estado(self):
        e = overlay.controle().estado()
        e["admin"] = windows.eh_admin()
        return e

    def overlay_ligar(self, sim):
        overlay.controle().ligar(bool(sim))
        return self.overlay_estado()

    def overlay_definir(self, chave, valor):
        overlay.controle().definir(str(chave), valor)
        return self.overlay_estado()

    def overlay_acompanhar(self, sim):
        overlay.controle().acompanhar(bool(sim))
        return self.overlay_estado()

    def jogos_fps(self, resolucao="1920x1080", qualidade="Alto", upscaling="Nativo"):
        return gargalo.tabela_fps(self._sensores, str(resolucao), str(qualidade), str(upscaling))

