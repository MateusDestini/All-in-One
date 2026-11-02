"""Coleta de dados e regras que identificam causas de lentidão."""

import logging
import os
import platform
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

import psutil

from . import windows
from .programas import (ANTIVIRUS_TERCEIROS, DICAS_PROGRAMAS, local_suspeito,
                        nome_de_sistema_fora_do_lugar)

log = logging.getLogger(__name__)

CRITICO, AVISO = "critico", "aviso"
UM_DIA_MIN = 24 * 60


@dataclass
class Problema:
    chave: str  # identifica o problema (mesma chave = mesmo problema)
    severidade: str
    titulo: str
    detalhe: str
    sugestao: str = ""
    # Alerta de novo quando a assinatura muda (ex.: evento novo) ou após `renotificar_min`.
    assinatura: str = ""
    renotificar_min: int | None = None  # None = padrão da configuração; 0 = só quando a assinatura mudar


@dataclass
class Programa:
    """Soma de todos os processos com o mesmo executável (ex.: 20 processos chrome.exe)."""
    nome: str
    cpu: float = 0.0  # % da CPU total
    ram: int = 0  # bytes
    disco: float = 0.0  # bytes/s
    processos: int = 0
    caminho: str = ""


@dataclass
class Amostra:
    t: float
    cpu: float
    ram: float
    discos: dict  # instância -> (% ocupado, latência em s)
    desempenho_cpu: float | None
    programas: dict = field(default_factory=dict)  # nome -> Programa


def _mb(b: float) -> str:
    return f"{b / 2**20:,.0f} MB".replace(",", ".")


def _gb(b: float) -> str:
    return f"{b / 2**30:.1f} GB".replace(".", ",")


def _media(valores) -> float:
    valores = list(valores)
    return sum(valores) / len(valores) if valores else 0.0


def _lista(nomes, limite=5) -> str:
    nomes = list(nomes)
    texto = ", ".join(nomes[:limite])
    return texto + (f" e mais {len(nomes) - limite}" if len(nomes) > limite else "")


class Monitor:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.n_cpus = psutil.cpu_count() or 1
        self.amostras: deque[Amostra] = deque()
        self.pdh = windows.criar_contadores()
        self.trava = threading.Lock()
        self.problemas_rapidos: dict[str, Problema] = {}
        self.problemas_pesados: dict[str, Problema] = {}
        self.ultima_pesada = 0.0
        self.pesada_rodando = False
        self.inicializacao: list[dict] = []
        self.discos: list[dict] = []
        self.reinicios: list[dict] = []
        self.particoes: list[tuple] = []
        # estado por processo
        self._io_anterior: dict[int, tuple[float, int]] = {}
        self._caminhos: dict[int, str] = {}
        self._historico_mem: dict[int, deque] = defaultdict(deque)
        self._ultima_coleta_lenta = 0.0
        self._handles: dict[int, tuple[str, int]] = {}
        self._suspeitos: dict[int, tuple[str, str, str]] = {}
        self._cpu_anterior: dict[int, tuple[float, int]] = {}
        self._rapido: list[tuple] | None = None  # última leitura de processos numa chamada só (Windows)
        psutil.cpu_percent(None)

    # ------------------------------------------------------------ coleta rápida (a cada poucos segundos)

    def amostrar(self) -> None:
        agora = time.time()
        discos, desempenho = {}, None
        if self.pdh:
            try:
                v = self.pdh.ler()
                latencias = v.get("disco_latencia", {})
                for inst, ocioso in v.get("disco_ocioso", {}).items():
                    if inst != "_Total":
                        discos[inst] = (max(0.0, 100.0 - ocioso), latencias.get(inst, 0.0))
                desempenho = v.get("cpu_desempenho", {}).get("_Total")
            except OSError:
                log.warning("falha lendo contadores", exc_info=True)

        amostra = Amostra(agora, psutil.cpu_percent(None), psutil.virtual_memory().percent,
                          discos, desempenho, self._coletar_programas(agora))
        coleta_lenta = agora - self._ultima_coleta_lenta >= 60
        if coleta_lenta:
            self._ultima_coleta_lenta = agora
            self._coleta_lenta(agora)

        with self.trava:
            self.amostras.append(amostra)
            while self.amostras and agora - self.amostras[0].t > self.cfg["janela_media_s"]:
                self.amostras.popleft()
            self.problemas_rapidos = self._avaliar()

    def _caminho(self, pid: int) -> str:
        if pid not in self._caminhos:
            try:
                self._caminhos[pid] = psutil.Process(pid).exe()
            except (psutil.Error, OSError):
                self._caminhos[pid] = ""
        return self._caminhos[pid]

    def _coletar_rapido(self, agora: float, lista: list[tuple]) -> dict[str, Programa]:
        """Mesma soma por programa, a partir de uma única consulta ao Windows (sem abrir cada processo)."""
        ignorar = self.cfg["ignorar_processos"]
        programas: dict[str, Programa] = {}
        vivos = set()
        for pid, nome, cpu_100ns, ws, io, _handles in lista:
            if pid in (0, 4) or not nome or nome.lower() in ignorar:
                continue
            vivos.add(pid)
            cpu = disco = 0.0
            anterior = self._cpu_anterior.get(pid)
            if anterior and agora > anterior[0]:
                cpu = max(0, cpu_100ns - anterior[1]) / ((agora - anterior[0]) * 1e7) / self.n_cpus * 100
            self._cpu_anterior[pid] = (agora, cpu_100ns)
            anterior = self._io_anterior.get(pid)
            if anterior and agora > anterior[0]:
                disco = max(0, io - anterior[1]) / (agora - anterior[0])
            self._io_anterior[pid] = (agora, io)
            prog = programas.setdefault(nome, Programa(nome, caminho=self._caminho(pid)))
            prog.cpu += cpu
            prog.ram += ws
            prog.disco += disco
            prog.processos += 1
        for cache in (self._io_anterior, self._cpu_anterior, self._caminhos, self._historico_mem, self._handles, self._suspeitos):
            for pid in [pid for pid in cache if pid not in vivos]:
                del cache[pid]
        return programas

    def _coletar_programas(self, agora: float) -> dict[str, Programa]:
        try:
            self._rapido = windows.processos_rapido()
        except Exception:
            log.warning("leitura rápida de processos falhou; usando psutil", exc_info=True)
            self._rapido = None
        if self._rapido:
            return self._coletar_rapido(agora, self._rapido)
        ignorar = self.cfg["ignorar_processos"]
        programas: dict[str, Programa] = {}
        vivos = set()
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                nome = p.info["name"] or ""
                if p.pid in (0, 4) or nome.lower() in ignorar:
                    continue
                vivos.add(p.pid)
                cpu = p.cpu_percent(None) / self.n_cpus
                ram = p.info["memory_info"].rss if p.info["memory_info"] else 0
                disco = 0.0
                try:
                    io = p.io_counters()
                    total = io.read_bytes + io.write_bytes
                    anterior = self._io_anterior.get(p.pid)
                    if anterior and agora > anterior[0]:
                        disco = max(0, total - anterior[1]) / (agora - anterior[0])
                    self._io_anterior[p.pid] = (agora, total)
                except (psutil.AccessDenied, AttributeError):
                    pass
                if p.pid not in self._caminhos:
                    try:
                        self._caminhos[p.pid] = p.exe()
                    except (psutil.AccessDenied, OSError):
                        self._caminhos[p.pid] = ""
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
            except psutil.AccessDenied:
                continue
            prog = programas.setdefault(nome, Programa(nome, caminho=self._caminhos.get(p.pid, "")))
            prog.cpu += cpu
            prog.ram += ram
            prog.disco += disco
            prog.processos += 1
        for cache in (self._io_anterior, self._caminhos, self._historico_mem, self._handles, self._suspeitos):
            for pid in [pid for pid in cache if pid not in vivos]:
                del cache[pid]
        return programas

    def _coleta_lenta(self, agora: float) -> None:
        """Uma vez por minuto: memória por processo (vazamentos), handles e executáveis suspeitos."""
        janela = self.cfg["janela_vazamento_min"] * 60
        if self._rapido:
            for pid, nome, _cpu, ws, _io, handles in self._rapido:
                if pid in (0, 4) or not nome:
                    continue
                hist = self._historico_mem[pid]
                hist.append((agora, ws, nome))
                while hist and agora - hist[0][0] > janela + 30:
                    hist.popleft()
                self._handles[pid] = (nome, handles)
                caminho = self._caminhos.get(pid, "")
                motivo = local_suspeito(caminho) or nome_de_sistema_fora_do_lugar(nome, caminho)
                if motivo:
                    self._suspeitos[pid] = (nome, caminho, motivo)
            return
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                if p.pid in (0, 4) or not p.info["memory_info"]:
                    continue
                hist = self._historico_mem[p.pid]
                hist.append((agora, p.info["memory_info"].rss, p.info["name"]))
                while hist and agora - hist[0][0] > janela + 30:
                    hist.popleft()
                if windows.EH_WINDOWS:
                    self._handles[p.pid] = (p.info["name"], p.num_handles())
                caminho = self._caminhos.get(p.pid, "")
                motivo = local_suspeito(caminho) or nome_de_sistema_fora_do_lugar(p.info["name"], caminho)
                if motivo:
                    self._suspeitos[p.pid] = (p.info["name"], caminho, motivo)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue

    # ------------------------------------------------------------ regras sobre as amostras

    def _avaliar(self) -> dict[str, Problema]:
        cfg = self.cfg
        a = list(self.amostras)
        probs: dict[str, Problema] = {}

        def add(p: Problema):
            probs[p.chave] = p

        # Só julga "sustentado" com pelo menos 80% da janela coletada.
        if not a or a[-1].t - a[0].t < cfg["janela_media_s"] * 0.8:
            return probs
        minutos = cfg["janela_media_s"] / 60
        media_programas = self._media_programas(a)

        def top(chave_ordem, formatar, n=3):
            lista = sorted(media_programas.values(), key=chave_ordem, reverse=True)[:n]
            return "; ".join(formatar(p) for p in lista)

        cpu = _media(x.cpu for x in a)
        if cpu >= cfg["cpu_total_alerta"]:
            add(Problema("cpu_alta", CRITICO if cpu >= 95 else AVISO,
                         f"Processador sobrecarregado ({cpu:.0f}%)",
                         f"Média de {cpu:.0f}% nos últimos {minutos:.0f} min. Quem mais usa: "
                         + top(lambda p: p.cpu, lambda p: f"{p.nome} {p.cpu:.0f}%"),
                         "Feche os programas listados ou veja-os no Gerenciador de Tarefas (Ctrl+Shift+Esc)."))

        ram = _media(x.ram for x in a)
        if ram >= cfg["ram_alerta"]:
            add(Problema("ram_alta", CRITICO if ram >= 95 else AVISO,
                         f"Memória RAM quase cheia ({ram:.0f}%)",
                         "Com a RAM cheia o Windows passa a usar o disco e tudo fica lento. Quem mais usa: "
                         + top(lambda p: p.ram, lambda p: f"{p.nome} {_mb(p.ram)}"),
                         "Feche abas/programas que não está usando. Se acontece sempre, considere mais RAM."))

        for disco in sorted({d for x in a for d in x.discos}):
            ocupado = _media(x.discos[disco][0] for x in a if disco in x.discos)
            ativos = [x.discos[disco][1] for x in a if disco in x.discos and x.discos[disco][1] > 0]
            if ocupado >= cfg["disco_ocupado_alerta"]:
                add(Problema(f"disco_ocupado_{disco}", AVISO, f"Disco {disco} 100% ocupado ({ocupado:.0f}%)",
                             "Quem mais lê/grava: " + top(lambda p: p.disco,
                                                          lambda p: f"{p.nome} {_mb(p.disco)}/s"),
                             "Comum durante atualizações ou antivírus. Se for constante em HD mecânico, "
                             "trocar por SSD resolve a maior parte da lentidão."))
            if len(ativos) >= len(a) / 2:
                lat = _media(ativos) * 1000
                if lat >= cfg["disco_latencia_alerta_ms"]:
                    add(Problema(f"disco_lento_{disco}", CRITICO if lat >= 200 else AVISO,
                                 f"Disco {disco} respondendo devagar ({lat:.0f} ms)",
                                 f"Cada leitura/gravação leva em média {lat:.0f} ms (o normal é < 20 ms).",
                                 "Pode ser disco sobrecarregado ou começando a falhar. Verifique a saúde "
                                 "com o CrystalDiskInfo e faça backup."))

        desempenho = [x.desempenho_cpu for x in a if x.desempenho_cpu is not None and x.cpu > 50]
        if len(desempenho) >= len(a) / 2:
            perf = _media(desempenho)
            if perf < cfg["cpu_desempenho_minimo"]:
                bateria = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
                na_bateria = bateria is not None and not bateria.power_plugged
                add(Problema("cpu_limitada", AVISO,
                             f"Processador trabalhando abaixo da velocidade ({perf:.0f}%)",
                             "Mesmo ocupado, o processador está rodando em "
                             f"{perf:.0f}% da frequência normal"
                             + (" (notebook na bateria)." if na_bateria else "."),
                             "Ligue na tomada e use o modo de energia 'Equilibrado' ou 'Melhor desempenho'."
                             if na_bateria else
                             "Sinal de superaquecimento: limpe a poeira das saídas de ar, verifique a "
                             "pasta térmica e se as ventoinhas funcionam."))

        self._avaliar_programas(media_programas, a, add)
        return probs

    def _media_programas(self, a: list[Amostra]) -> dict[str, Programa]:
        soma: dict[str, Programa] = {}
        for x in a:
            for nome, p in x.programas.items():
                s = soma.setdefault(nome, Programa(nome, caminho=p.caminho))
                s.cpu += p.cpu / len(a)
                s.ram += p.ram / len(a)
                s.disco += p.disco / len(a)
                s.processos = max(s.processos, p.processos)
        for s in soma.values():
            s.ram = int(s.ram)
        return soma

    def _avaliar_programas(self, programas: dict[str, Programa], a: list[Amostra], add) -> None:
        cfg = self.cfg
        minutos = cfg["janela_media_s"] / 60

        for p in programas.values():
            dica = DICAS_PROGRAMAS.get(p.nome.lower(), "")
            qtd = f" ({p.processos} processos)" if p.processos > 1 else ""
            if p.cpu >= cfg["programa_cpu_alerta"]:
                add(Problema(f"prog_cpu_{p.nome}", CRITICO if p.cpu >= 80 else AVISO,
                             f"{p.nome} usando {p.cpu:.0f}% da CPU",
                             f"Média dos últimos {minutos:.0f} min{qtd}.",
                             dica or "Se não reconhece ou não está usando, feche-o pelo Gerenciador de Tarefas."))
            if p.ram >= cfg["programa_ram_alerta_mb"] * 2**20:
                add(Problema(f"prog_ram_{p.nome}", AVISO, f"{p.nome} ocupando {_mb(p.ram)} de RAM",
                             f"Memória somada{qtd}.",
                             dica or "Reinicie o programa; se continuar crescendo, pode ter vazamento."))
            if p.disco >= cfg["programa_disco_alerta_mb_s"] * 2**20:
                add(Problema(f"prog_disco_{p.nome}", AVISO, f"{p.nome} usando muito o disco ({_mb(p.disco)}/s)",
                             f"Leitura+gravação média nos últimos {minutos:.0f} min{qtd}.",
                             dica or "Pode estar sincronizando, atualizando ou baixando algo."))

        # Vazamento de memória: cresce de forma contínua ao longo da janela.
        limite = cfg["vazamento_memoria_mb"] * 2**20
        janela = cfg["janela_vazamento_min"] * 60
        for pid, hist in self._historico_mem.items():
            if len(hist) < 5 or hist[-1][0] - hist[0][0] < janela * 0.9:
                continue
            valores = [m for _, m, _ in hist]
            subidas = sum(1 for x, y in zip(valores, valores[1:]) if y >= x)
            if valores[-1] - valores[0] >= limite and subidas >= 0.8 * (len(valores) - 1):
                nome = hist[-1][2]
                add(Problema(f"vazamento_{nome}", AVISO, f"{nome} pode estar com vazamento de memória",
                             f"Cresceu {_mb(valores[-1] - valores[0])} em {janela / 60:.0f} min sem parar "
                             f"(agora {_mb(valores[-1])}).",
                             "Feche e abra o programa de novo; atualize-o para a versão mais recente.",
                             renotificar_min=120))

        for nome, handles in self._handles.values():
            if handles >= cfg["processo_handles_alerta"]:
                add(Problema(f"handles_{nome}", AVISO, f"{nome} com recursos presos ({handles:,} handles)".replace(",", "."),
                             "Programas que acumulam handles costumam deixar o Windows lento com o tempo.",
                             "Reinicie o programa. Se for um driver/serviço, atualize-o.",
                             renotificar_min=UM_DIA_MIN))

        for nome, caminho, motivo in self._suspeitos.values():
            cpu = programas.get(nome, Programa(nome)).cpu
            add(Problema(f"suspeito_{nome}_{caminho}", CRITICO if cpu >= 20 else AVISO,
                         f"Programa suspeito: {nome}",
                         f"{motivo}. Local: {caminho}" + (f". Usando {cpu:.0f}% da CPU" if cpu >= 1 else ""),
                         "Malware (ex.: mineradores) costuma rodar assim e consome desempenho. Faça uma "
                         "verificação completa no Segurança do Windows ou com o Malwarebytes.",
                         renotificar_min=UM_DIA_MIN))

        antivirus = sorted({ANTIVIRUS_TERCEIROS[n.lower()] for n in programas if n.lower() in ANTIVIRUS_TERCEIROS})
        if len(antivirus) >= 2:
            add(Problema("varios_antivirus", CRITICO, "Mais de um antivírus rodando ao mesmo tempo",
                         "Encontrados: " + ", ".join(antivirus) + ".",
                         "Dois antivírus disputam cada arquivo aberto e deixam o PC muito lento. "
                         "Desinstale um deles (Configurações > Aplicativos).",
                         renotificar_min=UM_DIA_MIN))

    # ------------------------------------------------------------ verificações pesadas (a cada ~10 min)

    def verificacao_pesada_pendente(self) -> bool:
        return (not self.pesada_rodando
                and time.time() - self.ultima_pesada >= self.cfg["intervalo_verificacao_pesada_s"])

    # De quanto em quanto tempo cada verificação pesada roda sozinha (segundos). O que quase não muda roda menos,
    # para o monitor não abrir PowerShell à toa num PC lento. "Verificar agora" roda tudo.
    INTERVALO_ETAPA = {"_espaco_disco": 600, "_tempo_ligado": 600, "_inicializacao": 1800, "_eventos": 1800,
                       "_reinicios": 3600, "_saude_discos": 3600, "_energia": 3600,
                       "_boot": float("inf"), "_memoria_fisica": float("inf")}  # só mudam reiniciando o PC

    def verificacao_pesada(self, tudo: bool = False) -> None:
        self.pesada_rodando = True
        if not hasattr(self, "_por_etapa"):
            self._por_etapa: dict[str, tuple[float, list]] = {}
        try:
            probs: dict[str, Problema] = {}
            agora = time.time()
            for etapa in (self._espaco_disco, self._inicializacao, self._eventos, self._reinicios, self._boot, self._memoria_fisica,
                          self._saude_discos, self._energia, self._tempo_ligado):
                nome = etapa.__name__
                anterior = self._por_etapa.get(nome)
                if not tudo and anterior and agora - anterior[0] < self.INTERVALO_ETAPA.get(nome, 600):
                    lista = anterior[1]
                else:
                    try:
                        lista = list(etapa())
                        self._por_etapa[nome] = (agora, lista)
                    except Exception:
                        log.exception("falha na verificação %s", nome)
                        lista = anterior[1] if anterior else []
                for p in lista:
                    probs[p.chave] = p
            with self.trava:
                self.problemas_pesados = probs
        finally:
            self.ultima_pesada = time.time()
            self.pesada_rodando = False

    def _espaco_disco(self):
        cfg = self.cfg
        sistema = os.environ.get("SystemDrive", "C:").upper()
        self.particoes = []
        for part in psutil.disk_partitions(all=False):
            if "cdrom" in part.opts or not part.fstype or part.fstype.lower() in ("squashfs", "tmpfs", "overlay"):
                continue
            try:
                uso = psutil.disk_usage(part.mountpoint)
            except OSError:
                continue
            if uso.total < 2**30:
                continue
            self.particoes.append((part.mountpoint, uso))
            livre_pct = 100 - uso.percent
            eh_sistema = part.mountpoint.upper().startswith(sistema)
            if livre_pct < cfg["espaco_livre_min_pct"] or (eh_sistema and uso.free < cfg["espaco_livre_min_gb"] * 2**30):
                yield Problema(f"espaco_{part.mountpoint}",
                               CRITICO if eh_sistema and livre_pct < 5 else AVISO,
                               f"Pouco espaço livre em {part.mountpoint} ({_gb(uso.free)})",
                               f"Livre: {livre_pct:.0f}% de {_gb(uso.total)}."
                               + (" O Windows precisa de espaço para memória virtual e atualizações." if eh_sistema else ""),
                               "Use Configurações > Sistema > Armazenamento > Recomendações de limpeza, "
                               "esvazie a Lixeira e a pasta Downloads.",
                               assinatura=f"{livre_pct:.0f}", renotificar_min=UM_DIA_MIN)

    def _inicializacao(self):
        self.inicializacao = windows.programas_inicializacao()
        ativos = [i["nome"] for i in self.inicializacao if i["ativo"]]
        if len(ativos) >= self.cfg["programas_inicializacao_alerta"]:
            yield Problema("inicializacao", AVISO, f"{len(ativos)} programas abrem com o Windows",
                           "Programas na inicialização: " + _lista(ativos, 8) + ".",
                           "Desative os desnecessários na página Inicialização do painel do monitor.",
                           assinatura=str(len(ativos)), renotificar_min=UM_DIA_MIN)

    def _memoria_fisica(self):
        from .memoria import problemas_modulos
        yield from problemas_modulos()

    def _reinicios(self):
        from . import reinicios
        self.reinicios = reinicios.coletar(7)
        yield from reinicios.problemas(self.reinicios, 7)

    def _eventos(self):
        from .eventos import analisar
        yield from analisar(windows.eventos_recentes(24))

    def _boot(self):
        dados = windows.diagnostico_inicializacao()
        boot = next((d for d in dados if d.get("Tipo") == "boot"), None)
        if not boot or not boot.get("Ms"):
            return
        segundos = boot["Ms"] / 1000
        if segundos >= self.cfg["inicializacao_lenta_s"]:
            culpados = {}
            for d in dados:
                if d.get("Tipo") == "culpado" and d.get("Nome"):
                    culpados[d["Nome"]] = max(culpados.get(d["Nome"], 0), d.get("Ms") or 0)
            ordem = sorted(culpados.items(), key=lambda kv: kv[1], reverse=True)
            yield Problema("boot_lento", AVISO, f"Windows demorou {segundos:.0f} s para iniciar",
                           ("Atrasaram a inicialização: " + _lista(f"{n} (+{ms / 1000:.0f} s)" for n, ms in ordem) + "."
                            if ordem else "Inicialização registrada em " + boot.get("Hora", "") + "."),
                           "Desative programas de inicialização e atualize os drivers listados.",
                           assinatura=boot.get("Hora", ""), renotificar_min=0)

    def _saude_discos(self):
        from . import discos
        self.discos = discos.saude()
        yield from discos.problemas(self.discos)

    def _energia(self):
        plano = windows.plano_energia()
        if plano and plano[0] == windows.ECONOMIA_ENERGIA:
            yield Problema("energia", AVISO, "Plano de energia em modo Economia",
                           f"Plano ativo: {plano[1] or 'Economia de energia'}. Ele limita o processador.",
                           "No painel do monitor, abra Otimização e ligue o Alto desempenho (ou escolha Equilibrado nas Opções de Energia).",
                           renotificar_min=UM_DIA_MIN)

    def _tempo_ligado(self):
        dias = (time.time() - psutil.boot_time()) / 86400
        if dias >= self.cfg["uptime_alerta_dias"]:
            yield Problema("uptime", AVISO, f"PC sem reiniciar há {dias:.0f} dias",
                           "O 'Desligar' do Windows 11 hiberna o sistema (Inicialização rápida) e não limpa a memória.",
                           "Use 'Reiniciar' de vez em quando para liberar memória e concluir atualizações.",
                           renotificar_min=UM_DIA_MIN)

    # ------------------------------------------------------------ consulta

    def problemas(self) -> list[Problema]:
        with self.trava:
            todos = list(self.problemas_rapidos.values()) + list(self.problemas_pesados.values())
        return sorted(todos, key=lambda p: (p.severidade != CRITICO, p.titulo))

    def resumo_atual(self) -> dict:
        with self.trava:
            a = list(self.amostras)
        ultima = a[-1] if a else None
        programas = self._media_programas(a[-12:]) if a else {}
        return {
            "sistema": f"{platform.system()} {platform.release()} ({platform.version()})",
            "cpus": self.n_cpus,
            "ram": psutil.virtual_memory(),
            "ligado_ha_h": (time.time() - psutil.boot_time()) / 3600,
            "ultima": ultima,
            "programas": programas,
            "particoes": self.particoes,
            "inicializacao": self.inicializacao,
        }
