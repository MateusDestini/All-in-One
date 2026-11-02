"""Janela do painel: mostra o estado do PC de forma visual."""

import os
import time
import tkinter as tk
import webbrowser
from collections import deque
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.parse import quote_plus

import psutil

from . import config, relatorio, windows
from .programas import DICAS_PROGRAMAS, NOMES_DE_SISTEMA
from .verificacoes import CRITICO, Monitor, Programa, _gb, _mb

FONTE = "Segoe UI" if windows.EH_WINDOWS else "DejaVu Sans"
FUNDO, CARTAO, TEXTO, SUAVE, BORDA = "#f3f3f3", "#ffffff", "#1f2328", "#59636e", "#e1e4e8"
COR = {"ok": "#2ea043", "aviso": "#d29922", "critico": "#d1242f", "azul": "#0969da"}
COR_CLARA = {"ok": "#dafbe1", "aviso": "#fff8c5", "critico": "#ffebe9"}
NAO_FECHAR = NOMES_DE_SISTEMA | {"explorer.exe", "dwm.exe", "system", "msmpeng.exe", "monitorlentidao.exe"}
PONTOS_GRAFICO = 60  # 2 min com atualização a cada 2 s


def _nivel(valor: float, aviso: float, critico: float) -> str:
    return "critico" if valor >= critico else "aviso" if valor >= aviso else "ok"


class Medidor(tk.Frame):
    """Cartão com título, valor grande, barra e mini-gráfico do histórico."""

    def __init__(self, pai, titulo: str):
        super().__init__(pai, bg=CARTAO, highlightbackground=BORDA, highlightthickness=1, padx=14, pady=10)
        tk.Label(self, text=titulo, bg=CARTAO, fg=SUAVE, font=(FONTE, 10)).pack(anchor="w")
        self.valor = tk.Label(self, text="--", bg=CARTAO, fg=TEXTO, font=(FONTE, 22, "bold"))
        self.valor.pack(anchor="w")
        self.sub = tk.Label(self, text="", bg=CARTAO, fg=SUAVE, font=(FONTE, 9))
        self.sub.pack(anchor="w")
        self.grafico = tk.Canvas(self, height=42, width=190, bg=CARTAO, highlightthickness=0)
        self.grafico.pack(fill="x", pady=(6, 0))
        self.historico: deque[float] = deque(maxlen=PONTOS_GRAFICO)

    def atualizar(self, texto: str, sub: str, pct: float | None, nivel: str):
        self.valor.config(text=texto, fg=COR[nivel] if nivel != "ok" else TEXTO)
        self.sub.config(text=sub)
        if pct is None:
            return
        self.historico.append(max(0.0, min(100.0, pct)))
        c = self.grafico
        c.delete("all")
        w, h = max(c.winfo_width(), 50), 42
        c.create_line(0, h - 1, w, h - 1, fill=BORDA)
        if len(self.historico) < 2:
            return
        passo = w / (PONTOS_GRAFICO - 1)
        inicio = w - passo * (len(self.historico) - 1)
        pts = [(inicio + i * passo, h - 2 - v / 100 * (h - 4)) for i, v in enumerate(self.historico)]
        c.create_polygon([(inicio, h)] + pts + [(w, h)], fill=COR_CLARA[nivel], outline="")
        c.create_line(*[x for p in pts for x in p], fill=COR[nivel] if nivel != "ok" else COR["azul"], width=2)


class AreaRolavel(tk.Frame):
    def __init__(self, pai):
        super().__init__(pai, bg=FUNDO)
        self.canvas = tk.Canvas(self, bg=FUNDO, highlightthickness=0)
        barra = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.conteudo = tk.Frame(self.canvas, bg=FUNDO)
        self.conteudo.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        janela = self.canvas.create_window((0, 0), window=self.conteudo, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(janela, width=e.width))
        self.canvas.configure(yscrollcommand=barra.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._rolar))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _rolar(self, evento):
        self.canvas.yview_scroll(int(-evento.delta / 120), "units")


class Painel:
    def __init__(self, root: tk.Tk, monitor: Monitor, cfg: dict, historico, ao_verificar):
        self.root, self.monitor, self.cfg = root, monitor, cfg
        self.historico_alertas = historico
        self.ao_verificar = ao_verificar
        self.inicio = time.time()
        self._assinatura_problemas = None
        self._cartoes: dict = {}
        self._assinatura_inicializacao = None
        self._ordem = ("impacto", True)
        self._programas: dict[str, Programa] = {}

        root.title("Monitor de Lentidão")
        escala = max(1.0, root.winfo_fpixels("1i") / 96)  # telas com zoom de 125%, 150%...
        root.geometry(f"{int(980 * escala)}x{int(700 * escala)}")
        root.minsize(int(820 * escala), int(560 * escala))
        root.configure(bg=FUNDO)
        self._estilos()

        self.cabecalho = tk.Frame(root, bg=COR["ok"], padx=20, pady=14)
        self.cabecalho.pack(fill="x")
        self.simbolo = tk.Label(self.cabecalho, text="✔", bg=COR["ok"], fg="white", font=(FONTE, 30, "bold"))
        self.simbolo.pack(side="left", padx=(0, 14))
        textos = tk.Frame(self.cabecalho, bg=COR["ok"])
        textos.pack(side="left", fill="x")
        self.status = tk.Label(textos, text="Analisando seu PC...", bg=COR["ok"], fg="white",
                               font=(FONTE, 17, "bold"), anchor="w")
        self.status.pack(anchor="w")
        self.substatus = tk.Label(textos, text="Coletando dados dos primeiros minutos.", bg=COR["ok"],
                                  fg="white", font=(FONTE, 10), anchor="w")
        self.substatus.pack(anchor="w")

        linha = tk.Frame(root, bg=FUNDO, padx=16, pady=12)
        linha.pack(fill="x")
        self.medidores = {}
        for i, (chave, titulo) in enumerate([("cpu", "Processador"), ("ram", "Memória RAM"),
                                             ("disco", "Uso do disco"), ("espaco", "Espaço livre")]):
            m = Medidor(linha, titulo)
            m.grid(row=0, column=i, sticky="nsew", padx=4)
            linha.columnconfigure(i, weight=1)
            self.medidores[chave] = m

        rodape = tk.Frame(root, bg=FUNDO, padx=20, pady=10)
        rodape.pack(side="bottom", fill="x")
        ttk.Button(rodape, text="Verificar tudo agora", style="Destaque.TButton",
                   command=self._verificar).pack(side="left")
        ttk.Button(rodape, text="Exportar relatório", command=self._exportar).pack(side="left", padx=8)
        ttk.Button(rodape, text="Pasta de dados", command=lambda: windows.abrir(config.pasta_dados())).pack(side="left")
        self.autostart = tk.BooleanVar(value=windows.autostart_ativo())
        ttk.Checkbutton(rodape, text="Iniciar com o Windows", variable=self.autostart,
                        command=self._alternar_autostart).pack(side="right")

        self.abas = ttk.Notebook(root)
        self.abas.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        self._aba_problemas()
        self._aba_programas()
        self._aba_inicializacao()
        self._aba_historico()

        self.atualizar()

    # ------------------------------------------------------------ montagem

    def _estilos(self):
        s = ttk.Style(self.root)
        if "vista" in s.theme_names():
            s.theme_use("vista")
        elif "clam" in s.theme_names():
            s.theme_use("clam")
        s.configure("TNotebook", background=FUNDO, borderwidth=0)
        s.configure("TNotebook.Tab", font=(FONTE, 10), padding=(14, 6))
        s.configure("Treeview", font=(FONTE, 10), rowheight=26)
        s.configure("Treeview.Heading", font=(FONTE, 10, "bold"))
        s.configure("TButton", font=(FONTE, 10), padding=(10, 4))
        s.configure("Destaque.TButton", font=(FONTE, 10, "bold"))
        s.configure("TCheckbutton", background=FUNDO, font=(FONTE, 10))

    def _aba_problemas(self):
        self.area_problemas = AreaRolavel(self.abas)
        self.abas.add(self.area_problemas, text="Problemas")

    def _aba_programas(self):
        aba = tk.Frame(self.abas, bg=FUNDO, padx=10, pady=10)
        self.abas.add(aba, text="Programas")
        tk.Label(aba, text="Programas abertos agora, do que mais pesa para o que menos pesa (média do último minuto).",
                 bg=FUNDO, fg=SUAVE, font=(FONTE, 9)).pack(anchor="w", pady=(0, 6))
        colunas = {"nome": ("Programa", 230, "w"), "impacto": ("Impacto", 90, "center"),
                   "cpu": ("Processador", 115, "e"), "ram": ("Memória", 100, "e"),
                   "disco": ("Disco", 100, "e"), "processos": ("Processos", 95, "e")}
        # Botões e dica são empacotados antes para nunca serem cortados em telas pequenas.
        botoes = tk.Frame(aba, bg=FUNDO)
        botoes.pack(side="bottom", fill="x")
        ttk.Button(botoes, text="O que é este programa?", command=self._pesquisar).pack(side="left")
        ttk.Button(botoes, text="Abrir local do arquivo", command=self._abrir_local).pack(side="left", padx=8)
        ttk.Button(botoes, text="Fechar programa", command=self._fechar_programa).pack(side="left")
        self.dica = tk.Label(aba, text="Selecione um programa para ver detalhes.", bg=FUNDO, fg=TEXTO,
                             font=(FONTE, 10), justify="left", anchor="w", wraplength=880)
        self.dica.pack(side="bottom", fill="x", pady=(8, 4))
        quadro = tk.Frame(aba, bg=FUNDO)
        quadro.pack(fill="both", expand=True)
        self.tabela = ttk.Treeview(quadro, columns=list(colunas), show="headings", selectmode="browse")
        for chave, (titulo, largura, ancora) in colunas.items():
            self.tabela.heading(chave, text=titulo, command=lambda c=chave: self._ordenar(c))
            self.tabela.column(chave, width=largura, anchor=ancora, stretch=chave == "nome")
        for nivel in ("critico", "aviso"):
            self.tabela.tag_configure(nivel, background=COR_CLARA[nivel])
        barra = ttk.Scrollbar(quadro, orient="vertical", command=self.tabela.yview)
        self.tabela.configure(yscrollcommand=barra.set)
        self.tabela.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.tabela.bind("<<TreeviewSelect>>", lambda e: self._mostrar_dica())

    def _aba_inicializacao(self):
        aba = tk.Frame(self.abas, bg=FUNDO, padx=10, pady=10)
        self.abas.add(aba, text="Inicialização")
        tk.Label(aba, text="Programas que abrem sozinhos quando o Windows liga. Quanto menos, mais rápido o PC inicia.",
                 bg=FUNDO, fg=SUAVE, font=(FONTE, 9)).pack(anchor="w", pady=(0, 6))
        self.tabela_ini = ttk.Treeview(aba, columns=("nome", "status", "origem", "comando"), show="headings")
        for chave, titulo, largura in (("nome", "Programa", 200), ("status", "Situação", 100),
                                       ("origem", "Para", 80), ("comando", "Comando", 480)):
            self.tabela_ini.heading(chave, text=titulo)
            self.tabela_ini.column(chave, width=largura, stretch=chave == "comando")
        self.tabela_ini.tag_configure("desativado", foreground=SUAVE)
        ttk.Button(aba, text="Desativar programas de inicialização...",
                   command=lambda: self._abrir_uri("ms-settings:startupapps")).pack(side="bottom", anchor="w", pady=(8, 0))
        self.tabela_ini.pack(fill="both", expand=True)

    def _aba_historico(self):
        aba = tk.Frame(self.abas, bg=FUNDO, padx=10, pady=10)
        self.abas.add(aba, text="Histórico")
        self.texto_hist = tk.Text(aba, font=(FONTE, 10), wrap="word", relief="flat", bg=CARTAO, padx=10, pady=8)
        self.texto_hist.pack(fill="both", expand=True)
        self.texto_hist.tag_configure("critico", foreground=COR["critico"])
        self.abas.bind("<<NotebookTabChanged>>", lambda e: self._carregar_historico())

    # ------------------------------------------------------------ atualização

    def atualizar(self):
        """Laço de atualização da janela (a cada 2 s, só quando visível)."""
        if self.root.state() != "withdrawn":
            try:
                self.atualizar_agora()
            except tk.TclError:
                return
        self.root.after(2000, self.atualizar)

    def atualizar_agora(self):
        self._atualizar_cabecalho_e_medidores()
        self._atualizar_problemas()
        self._atualizar_programas()
        self._atualizar_inicializacao()

    def _atualizar_cabecalho_e_medidores(self):
        r = self.monitor.resumo_atual()
        problemas = self.monitor.problemas()
        criticos = sum(p.severidade == CRITICO for p in problemas)
        coletando = time.time() - self.inicio < self.cfg["janela_media_s"] * 0.8
        if criticos:
            nivel, simbolo = "critico", "✖"
            titulo = "Encontrei um problema sério" if criticos == 1 else f"Encontrei {criticos} problemas sérios"
        elif problemas:
            nivel, simbolo = "aviso", "!"
            titulo = (f"{len(problemas)} coisa pode estar deixando seu PC lento" if len(problemas) == 1
                      else f"{len(problemas)} coisas podem estar deixando seu PC lento")
        else:
            nivel, simbolo = "ok", "✔"
            titulo = "Analisando seu PC..." if coletando else "Tudo certo com seu PC"
        ultima = (datetime.fromtimestamp(self.monitor.ultima_pesada).strftime("%H:%M")
                  if self.monitor.ultima_pesada else "em andamento")
        avisos = len(problemas) - criticos
        contagem = ""
        if criticos and avisos:
            contagem = f"Mais {avisos} aviso(s) na lista abaixo  ·  "
        sub = (contagem + f"Última verificação completa: {ultima}  ·  ligado há {self._duracao(r['ligado_ha_h'])}"
               + ("  ·  coletando dados dos primeiros minutos" if coletando else ""))
        for w in (self.cabecalho, self.simbolo, self.status, self.substatus, self.status.master):
            w.config(bg=COR[nivel])
        self.simbolo.config(text=simbolo)
        self.status.config(text=titulo)
        self.substatus.config(text=sub)
        self.abas.tab(0, text=f"Problemas ({len(problemas)})" if problemas else "Problemas")

        u = r["ultima"]
        if u:
            self.medidores["cpu"].atualizar(f"{u.cpu:.0f}%",
                                            f"{r['cpus']} núcleos" + (f" · velocidade {u.desempenho_cpu:.0f}%"
                                                                     if u.desempenho_cpu is not None else ""),
                                            u.cpu, _nivel(u.cpu, 70, self.cfg["cpu_total_alerta"]))
            ram = r["ram"]
            self.medidores["ram"].atualizar(f"{u.ram:.0f}%", f"{_gb(ram.used)} de {_gb(ram.total)}",
                                            u.ram, _nivel(u.ram, 80, self.cfg["ram_alerta"]))
            if u.discos:
                nome, (ocupado, lat) = max(u.discos.items(), key=lambda kv: kv[1][0])
                self.medidores["disco"].atualizar(f"{ocupado:.0f}%", f"Disco {nome} · {lat * 1000:.0f} ms",
                                                  ocupado, _nivel(ocupado, 70, self.cfg["disco_ocupado_alerta"]))
            else:
                self.medidores["disco"].atualizar("--", "indisponível", None, "ok")
        sistema = os.environ.get("SystemDrive", "C:").upper()
        particoes = r["particoes"]
        alvo = next((p for p in particoes if p[0].upper().startswith(sistema)), particoes[0] if particoes else None)
        if alvo:
            ponto, uso = alvo
            livre = 100 - uso.percent
            nivel_espaco = _nivel(100 - livre, 100 - 2 * self.cfg["espaco_livre_min_pct"],
                                  100 - self.cfg["espaco_livre_min_pct"])
            self.medidores["espaco"].atualizar(_gb(uso.free), f"livres em {ponto} ({livre:.0f}%)",
                                               uso.percent, nivel_espaco)
        else:
            self.medidores["espaco"].atualizar("--", "verificando...", None, "ok")

    def _atualizar_problemas(self):
        problemas = self.monitor.problemas()
        assinatura = [p.chave for p in problemas]
        if assinatura == self._assinatura_problemas:
            # Mesmos problemas: só atualiza os números, sem redesenhar (evita piscar).
            for p in problemas:
                titulo, detalhe = self._cartoes[p.chave]
                titulo.config(text=p.titulo)
                detalhe.config(text=p.detalhe)
            return
        self._assinatura_problemas = assinatura
        self._cartoes = {}
        area = self.area_problemas.conteudo
        for w in area.winfo_children():
            w.destroy()
        if not problemas:
            vazio = tk.Frame(area, bg=FUNDO, pady=60)
            vazio.pack(fill="x")
            tk.Label(vazio, text="✔", fg=COR["ok"], bg=FUNDO, font=(FONTE, 40)).pack()
            tk.Label(vazio, text="Nenhum problema encontrado", fg=TEXTO, bg=FUNDO,
                     font=(FONTE, 14, "bold")).pack()
            tk.Label(vazio, text="O monitor continua de olho e avisa se algo mudar.", fg=SUAVE, bg=FUNDO,
                     font=(FONTE, 10)).pack()
            return
        for p in problemas:
            self._cartao_problema(area, p)

    def _cartao_problema(self, area, p):
        nivel = "critico" if p.severidade == CRITICO else "aviso"
        externo = tk.Frame(area, bg=COR[nivel])
        externo.pack(fill="x", padx=10, pady=5)
        cartao = tk.Frame(externo, bg=CARTAO, padx=14, pady=10)
        cartao.pack(fill="x", padx=(5, 0))
        topo = tk.Frame(cartao, bg=CARTAO)
        topo.pack(fill="x")
        tk.Label(topo, text=" GRAVE " if nivel == "critico" else " ATENÇÃO ", bg=COR[nivel], fg="white",
                 font=(FONTE, 8, "bold")).pack(side="left", padx=(0, 8))
        titulo = tk.Label(topo, text=p.titulo, bg=CARTAO, fg=TEXTO, font=(FONTE, 12, "bold"), anchor="w",
                          justify="left")
        titulo.pack(side="left", fill="x")
        largura = max(self.root.winfo_width() - 120, 600)
        detalhe = tk.Label(cartao, text=p.detalhe, bg=CARTAO, fg=SUAVE, font=(FONTE, 10), justify="left",
                           anchor="w", wraplength=largura)
        detalhe.pack(fill="x", pady=(6, 0))
        self._cartoes[p.chave] = (titulo, detalhe)
        if p.sugestao:
            dica = tk.Frame(cartao, bg=COR_CLARA["ok"], padx=10, pady=6)
            dica.pack(fill="x", pady=(8, 0))
            tk.Label(dica, text="💡 O que fazer: " + p.sugestao, bg=COR_CLARA["ok"], fg=TEXTO,
                     font=(FONTE, 10), justify="left", anchor="w", wraplength=largura - 20).pack(fill="x")

    def _impacto(self, p: Programa) -> float:
        c = self.cfg
        return max(p.cpu / c["programa_cpu_alerta"], p.ram / (c["programa_ram_alerta_mb"] * 2**20),
                   p.disco / (c["programa_disco_alerta_mb_s"] * 2**20))

    def _atualizar_programas(self):
        self._programas = self.monitor.resumo_atual()["programas"]
        coluna, desc = self._ordem
        chaves = {"impacto": self._impacto, "nome": lambda p: p.nome.lower(), "cpu": lambda p: p.cpu,
                  "ram": lambda p: p.ram, "disco": lambda p: p.disco, "processos": lambda p: p.processos}
        ordenados = sorted(self._programas.values(), key=chaves[coluna], reverse=desc)[:150]
        # Atualiza as linhas no lugar para não perder a seleção nem a posição da rolagem.
        visiveis = {p.nome for p in ordenados}
        for iid in self.tabela.get_children():
            if iid not in visiveis:
                self.tabela.delete(iid)
        for posicao, p in enumerate(ordenados):
            imp = self._impacto(p)
            nivel = "critico" if imp >= 1 else "aviso" if imp >= 0.4 else ""
            rotulo = "Alto" if imp >= 1 else "Médio" if imp >= 0.4 else "Baixo"
            valores = (p.nome, rotulo, f"{p.cpu:.1f}%", _mb(p.ram), f"{p.disco / 2**20:.1f} MB/s", p.processos)
            tags = (nivel,) if nivel else ()
            if self.tabela.exists(p.nome):
                self.tabela.item(p.nome, values=valores, tags=tags)
                self.tabela.move(p.nome, "", posicao)
            else:
                self.tabela.insert("", posicao, iid=p.nome, values=valores, tags=tags)

    def _ordenar(self, coluna):
        atual, desc = self._ordem
        self._ordem = (coluna, not desc if coluna == atual else coluna != "nome")
        self._atualizar_programas()

    def _atualizar_inicializacao(self):
        itens = self.monitor.inicializacao
        assinatura = [(i["nome"], i["ativo"]) for i in itens]
        if assinatura == self._assinatura_inicializacao:
            return
        self._assinatura_inicializacao = assinatura
        self.tabela_ini.delete(*self.tabela_ini.get_children())
        for i in sorted(itens, key=lambda i: (not i["ativo"], i["nome"].lower())):
            self.tabela_ini.insert("", "end", tags=() if i["ativo"] else ("desativado",), values=(
                i["nome"], "Ativo" if i["ativo"] else "Desativado",
                "Você" if i["origem"] == "Usuário" else "Todos", i["comando"]))
        ativos = sum(i["ativo"] for i in itens)
        self.abas.tab(2, text=f"Inicialização ({ativos})" if itens else "Inicialização")

    def _carregar_historico(self):
        self.texto_hist.config(state="normal")
        self.texto_hist.delete("1.0", "end")
        linhas = []
        if self.historico_alertas.exists():
            linhas = self.historico_alertas.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        if not linhas:
            self.texto_hist.insert("end", "Nenhum alerta registrado ainda.")
        for linha in reversed(linhas):
            self.texto_hist.insert("end", linha + "\n", ("critico",) if "[CRITICO]" in linha else ())
        self.texto_hist.config(state="disabled")

    # ------------------------------------------------------------ ações

    def _selecionado(self) -> Programa | None:
        sel = self.tabela.selection()
        return self._programas.get(sel[0]) if sel else None

    def _mostrar_dica(self):
        p = self._selecionado()
        if not p:
            return
        partes = [f"{p.nome}: {p.processos} processo(s), {p.cpu:.1f}% do processador, {_mb(p.ram)} de memória."]
        if p.caminho:
            partes.append(f"Local: {p.caminho}")
        dica = DICAS_PROGRAMAS.get(p.nome.lower())
        if dica:
            partes.append("💡 " + dica)
        self.dica.config(text="\n".join(partes))

    def _pesquisar(self):
        p = self._selecionado()
        if p:
            webbrowser.open("https://www.google.com/search?q=" + quote_plus(f"o que é {p.nome} windows"))

    def _abrir_local(self):
        p = self._selecionado()
        if not p or not p.caminho:
            messagebox.showinfo("Monitor de Lentidão", "O local deste programa não está disponível "
                                "(programas do sistema podem exigir administrador).", parent=self.root)
            return
        if windows.EH_WINDOWS:
            windows.executar(["explorer.exe", "/select,", p.caminho], timeout=5)

    def _fechar_programa(self):
        p = self._selecionado()
        if not p:
            return
        if p.nome.lower() in NAO_FECHAR:
            messagebox.showwarning("Monitor de Lentidão",
                                   f"{p.nome} faz parte do Windows e não deve ser fechado.", parent=self.root)
            return
        if not messagebox.askyesno("Fechar programa",
                                   f"Fechar {p.nome} ({p.processos} processo(s))?\n\n"
                                   "Trabalho não salvo nesse programa será perdido.", parent=self.root):
            return
        fechados, negados = 0, 0
        for proc in psutil.process_iter(["name"]):
            if proc.info["name"] == p.nome:
                try:
                    proc.terminate()
                    fechados += 1
                except psutil.AccessDenied:
                    negados += 1
                except psutil.NoSuchProcess:
                    pass
        msg = f"{fechados} processo(s) de {p.nome} fechados."
        if negados:
            msg += f"\n{negados} não puderam ser fechados (abra o monitor como administrador)."
        messagebox.showinfo("Monitor de Lentidão", msg, parent=self.root)

    def _verificar(self):
        self.ao_verificar()
        self.substatus.config(text="Fazendo verificação completa (pode levar até 1 minuto)...")

    def _exportar(self):
        arquivo = config.pasta_dados() / f"diagnostico_{datetime.now():%Y-%m-%d_%H%M}.txt"
        arquivo.write_text(relatorio.gerar(self.monitor, self.historico_alertas), encoding="utf-8")
        windows.abrir(arquivo)

    def _alternar_autostart(self):
        try:
            windows.definir_autostart(self.autostart.get())
        except OSError:
            messagebox.showerror("Monitor de Lentidão", "Não consegui alterar a inicialização automática.",
                                 parent=self.root)
        self.autostart.set(windows.autostart_ativo())

    def _abrir_uri(self, uri):
        if windows.EH_WINDOWS:
            os.startfile(uri)

    @staticmethod
    def _duracao(horas: float) -> str:
        if horas < 1:
            return f"{horas * 60:.0f} min"
        if horas < 48:
            return f"{horas:.0f} h"
        return f"{horas / 24:.0f} dias"
