"""Gargalo (o que limita o FPS agora) e estimativa de FPS em jogos conhecidos.

Regras de classificação, catálogo de jogos e índices de desempenho adaptados do ResetExplode
(BottleneckAnalyzer.cs e GameCatalog.cs; licença MIT, © 2026 Gustavo). Os índices e os FPS de referência são
APROXIMADOS, compilados de benchmarks públicos: servem para ter uma ideia, não substituem medir no jogo.
"""

import logging
import math
import re
import threading
import time

import psutil

from . import gpu, quadros

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ catálogo de jogos
# (nome, gênero, FPS em 1080p/Alto numa RTX 4070, teto de FPS pela CPU num Ryzen 7 7700X, trava do jogo)
JOGOS = [
    ("Counter-Strike 2", "FPS competitivo", 340, 420, 0), ("Valorant", "FPS competitivo", 600, 650, 0),
    ("Fortnite", "Battle royale", 160, 250, 0), ("League of Legends", "MOBA", 420, 480, 0),
    ("Dota 2", "MOBA", 260, 280, 0), ("Apex Legends", "Battle royale", 220, 300, 0),
    ("PUBG: Battlegrounds", "Battle royale", 180, 220, 0), ("Call of Duty: Black Ops 6", "FPS", 140, 220, 0),
    ("Call of Duty: Warzone", "Battle royale", 130, 200, 0), ("Battlefield 6", "FPS", 120, 170, 0),
    ("Rainbow Six Siege", "FPS tático", 350, 430, 0), ("Overwatch 2", "FPS de heróis", 280, 380, 0),
    ("Marvel Rivals", "FPS de heróis", 110, 160, 0), ("Rocket League", "Esporte", 400, 450, 0),
    ("EA Sports FC 25", "Esporte", 220, 260, 0), ("Minecraft", "Sandbox", 300, 340, 0),
    ("GTA V", "Mundo aberto", 160, 190, 0), ("Red Dead Redemption 2", "Mundo aberto", 95, 160, 0),
    ("Cyberpunk 2077", "RPG mundo aberto", 95, 150, 0), ("The Witcher 3: Wild Hunt", "RPG", 120, 160, 0),
    ("Elden Ring", "RPG de ação", 110, 140, 60), ("Baldur's Gate 3", "RPG", 110, 120, 0),
    ("Hogwarts Legacy", "RPG de ação", 90, 120, 0), ("Starfield", "RPG espacial", 65, 90, 0),
    ("Black Myth: Wukong", "Ação", 70, 140, 0), ("Alan Wake 2", "Terror e ação", 60, 140, 0),
    ("God of War Ragnarök", "Ação", 110, 170, 0), ("Forza Horizon 5", "Corrida", 140, 200, 0),
    ("Helldivers 2", "Cooperativo", 90, 110, 0), ("Monster Hunter Wilds", "Ação", 60, 85, 0),
    ("Assassin's Creed Shadows", "Ação mundo aberto", 65, 110, 0), ("Borderlands 4", "Looter shooter", 55, 110, 0),
]

# Desempenho relativo em jogos (raster), RTX 4070 = 100. A chave mais longa contida no nome vence.
INDICE_GPU = {
    "RTX 5090": 265, "RTX 5080": 175, "RTX 5070 Ti": 150, "RTX 5070": 118, "RTX 5060 Ti": 95, "RTX 5060": 85,
    "RTX 4090": 210, "RTX 4080 SUPER": 160, "RTX 4080": 157, "RTX 4070 Ti SUPER": 136, "RTX 4070 Ti": 124,
    "RTX 4070 SUPER": 116, "RTX 4070": 100, "RTX 4060 Ti": 82, "RTX 4060": 70,
    "RTX 3090 Ti": 135, "RTX 3090": 125, "RTX 3080 Ti": 122, "RTX 3080": 112, "RTX 3070 Ti": 92, "RTX 3070": 86,
    "RTX 3060 Ti": 78, "RTX 3060": 60, "RTX 3050": 40,
    "RTX 2080 Ti": 85, "RTX 2080 SUPER": 72, "RTX 2080": 68, "RTX 2070 SUPER": 64, "RTX 2070": 57,
    "RTX 2060 SUPER": 54, "RTX 2060": 46, "GTX 1660 Ti": 42, "GTX 1660 SUPER": 41, "GTX 1660": 38,
    "GTX 1650 SUPER": 32, "GTX 1650": 28, "GTX 1080 Ti": 63, "GTX 1080": 48, "GTX 1070 Ti": 44, "GTX 1070": 40,
    "GTX 1060": 30, "GTX 1050 Ti": 18,
    "RX 9070 XT": 160, "RX 9070": 140, "RX 9060 XT": 92, "RX 7900 XTX": 170, "RX 7900 XT": 150, "RX 7900 GRE": 128,
    "RX 7800 XT": 115, "RX 7700 XT": 98, "RX 7600 XT": 72, "RX 7600": 70,
    "RX 6950 XT": 125, "RX 6900 XT": 118, "RX 6800 XT": 112, "RX 6800": 100, "RX 6750 XT": 87, "RX 6700 XT": 82,
    "RX 6700": 76, "RX 6650 XT": 70, "RX 6600 XT": 66, "RX 6600": 58, "RX 6500 XT": 24,
    "RX 5700 XT": 60, "RX 5700": 54, "RX 5600 XT": 48, "RX 5500 XT": 33, "RX 590": 33, "RX 580": 31, "RX 570": 27,
    "Arc B580": 80, "Arc B570": 70, "Arc A770": 68, "Arc A750": 62, "Arc A580": 55,
    "Radeon(TM) Graphics": 6, "Radeon Graphics": 6, "Radeon 780M": 22, "Radeon 760M": 18, "Vega 8": 7,
    "Iris Xe": 7, "UHD Graphics": 3,
}

# Desempenho relativo em jogos limitados pela CPU, Ryzen 7 7700X = 100. Casa pelo modelo exato
# ("7700X" não confunde com "7700", nem um Intel i7-7700K com um Ryzen 7 7700).
INDICE_CPU_AMD = {
    "9950X3D": 138, "9800X3D": 140, "7950X3D": 126, "7800X3D": 128, "5800X3D": 95, "5700X3D": 90,
    "9950X": 112, "9900X": 110, "9700X": 108, "9600X": 104, "9600": 101, "7950X": 104, "7900X": 102, "7900": 99,
    "7700X": 100, "7700": 97, "7600X": 96, "7600": 93, "8700G": 84, "8600G": 80, "8500G": 70,
    "5950X": 82, "5900X": 81, "5800X": 80, "5800": 78, "5700X": 77, "5700G": 70, "5700": 68, "5600X": 76,
    "5600": 73, "5600G": 65, "5500": 66, "4600G": 58, "4500": 56,
    "3950X": 66, "3900X": 66, "3800X": 65, "3700X": 64, "3600X": 62, "3600": 60, "3400G": 42, "3200G": 38,
    "2700X": 54, "2600": 50, "1600": 42,
}
INDICE_CPU_INTEL = {
    "285K": 112, "265K": 108, "245K": 100,
    "14900K": 116, "14700K": 112, "14700": 108, "14600K": 104, "14600": 100, "14400": 86, "14100": 76,
    "13900K": 113, "13700K": 108, "13700": 104, "13600K": 101, "13600": 97, "13500": 92, "13400": 84, "13100": 74,
    "12900K": 98, "12700K": 95, "12700": 92, "12600K": 90, "12600": 86, "12400": 78, "12100": 72,
    "11900K": 80, "11700K": 76, "11600K": 74, "11400": 68, "10900K": 74, "10700K": 70, "10700": 68, "10600K": 66,
    "10400": 60, "10100": 50, "9900K": 68, "9700K": 64, "9600K": 56, "9400": 52, "8700K": 60, "8400": 50,
}

RESOLUCOES = {"1920x1080": 1.0, "2560x1440": 0.72, "3840x2160": 0.42}
QUALIDADES = {"Baixo": 1.6, "Médio": 1.3, "Alto": 1.0, "Ultra": 0.82}


def indice_gpu(nome: str) -> float | None:
    n = nome.replace("(R)", "").replace("(TM)", "").replace("NVIDIA ", "").replace("GeForce ", "")
    achados = [k for k in INDICE_GPU if k.lower() in n.lower()]
    if not achados:
        return None
    valor = INDICE_GPU[max(achados, key=len)]
    return valor * 0.8 if "laptop" in n.lower() else valor  # versões de notebook rendem uns 20% menos


def modelo_cpu(nome: str) -> tuple[str, str] | None:
    """("amd" | "intel", modelo) a partir do nome comercial."""
    n = nome.replace("(R)", "").replace("(TM)", "")
    if re.search(r"ryzen", n, re.I):
        m = re.search(r"Ryzen\s+(?:\d+|Threadripper)\s+(?:PRO\s+)?(\d{4}[A-Z0-9]*)", n, re.I)
        return ("amd", m.group(1).upper()) if m else None
    if re.search(r"intel|core", n, re.I):
        m = re.search(r"\bi\d-(\d{4,5}[A-Z]*)", n) or re.search(r"Ultra\s+\d\s+(\d{3}[A-Z]*)", n)
        if not m:
            return None
        return "intel", re.sub(r"(?<=[0-9K])F$", "", m.group(1).upper())  # F = sem vídeo integrado, mesmo desempenho
    return None


def indice_cpu(nome: str) -> float | None:
    mc = modelo_cpu(nome)
    if not mc:
        return None
    tabela = INDICE_CPU_AMD if mc[0] == "amd" else INDICE_CPU_INTEL
    return tabela.get(mc[1], tabela.get(re.sub(r"[A-Z]+$", "", mc[1])))  # ex.: 12400T usa o 12400


def nome_curto_gpu(nome: str) -> str:
    s = nome.replace("(TM)", "").replace("(R)", "")
    for w in ("NVIDIA", "GeForce", "AMD", "Intel"):
        s = s.replace(w, "")
    if "Radeon RX" in s:
        s = s.replace("Radeon", "")
    s = " ".join(s.split())
    if s.startswith("Arc") and s.endswith(" Graphics"):
        s = s[:-9]
    return s or nome


def nome_curto_cpu(nome: str) -> str:
    s = nome.replace("AMD", "").replace("(R)", "").replace("(TM)", "")
    s = re.sub(r"\s+(\d+-Core|with Radeon).*$", "", s, flags=re.I)
    s = re.sub(r"\d+th Gen\s*", "", s).replace("Intel", "").replace("Processor", "").replace("CPU", "")
    return " ".join(s.split("@")[0].split()) or nome


def fator_upscaling(modo: str, res: str) -> float:
    if modo == "Qualidade":
        return 1.25 if res == "1920x1080" else 1.4
    if modo == "Desempenho":
        return 1.45 if res == "1920x1080" else 1.75
    return 1.0


def estimar(jogo: tuple, idx_gpu: float, idx_cpu: float, res: str, qualidade: str, upscaling: str) -> dict:
    """FPS = o menor entre o teto da placa de vídeo, o teto do processador e a trava do jogo."""
    nome, genero, ref_gpu, teto_cpu, trava = jogo
    fps_gpu = ref_gpu * idx_gpu / 100 * RESOLUCOES.get(res, 1) * QUALIDADES.get(qualidade, 1) * fator_upscaling(upscaling, res)
    fps_cpu = teto_cpu * idx_cpu / 100 * (1.1 if qualidade == "Baixo" else 0.95 if qualidade == "Ultra" else 1.0)
    fps, limite = (fps_gpu, "gpu") if fps_gpu <= fps_cpu else (fps_cpu, "cpu")
    if trava and trava < fps:
        fps, limite = trava, "jogo"
    return {"nome": nome, "genero": genero, "fps": round(fps), "low1": round(fps * 0.75), "limite": limite,
            "trava": trava}


def equilibrio(idx_gpu: float, idx_cpu: float) -> dict:
    """Equilíbrio teórico entre placa de vídeo e processador pelos índices."""
    r = idx_gpu / idx_cpu
    if r > 1.8:
        texto = "Placa de vídeo muito mais forte que o processador: em boa parte dos jogos o processador vai limitar o FPS, principalmente em 1080p."
    elif r > 1.3:
        texto = "Placa de vídeo um pouco à frente: jogos competitivos em 1080p tendem a ficar limitados pelo processador; em 1440p e 4K a placa volta a ser o limite."
    elif r >= 0.7:
        texto = "Hardware equilibrado: na maioria dos jogos a placa de vídeo será o limite, que é o cenário ideal."
    elif r > 0.4:
        texto = "Processador mais forte que a placa de vídeo: ela é o limite. Trocar a placa traria o maior ganho."
    else:
        texto = "Placa de vídeo muito fraca para este processador: ela limita quase tudo."
    # posição do marcador numa escala logarítmica de 0,4x a 2,5x (meio = equilibrado)
    posicao = (math.log(r) - math.log(0.4)) / (math.log(2.5) - math.log(0.4))
    return {"razao": round(r, 2), "texto": texto, "posicao": min(1.0, max(0.0, posicao))}


def hardware(sens) -> dict:
    """Nomes e índices do processador e da placa de vídeo principal."""
    from .sensores import nome_cpu
    cpu = nome_cpu()
    placas = sens.placas()
    principal = next((p for p in placas if not p.get("integrada")), placas[0] if placas else None)
    nome_gpu = principal["nome"] if principal else ""
    ig, ic = indice_gpu(nome_gpu) if nome_gpu else None, indice_cpu(cpu)
    return {"cpu": nome_curto_cpu(cpu) if cpu else "Processador", "gpu": nome_curto_gpu(nome_gpu) if nome_gpu else "Placa de vídeo",
            "idx_cpu": ic, "idx_gpu": ig, "cpu_conhecida": ic is not None, "gpu_conhecida": ig is not None}


def tabela_fps(sens, res: str, qualidade: str, upscaling: str) -> dict:
    hw = hardware(sens)
    ig = hw["idx_gpu"] if hw["idx_gpu"] is not None else 50
    ic = hw["idx_cpu"] if hw["idx_cpu"] is not None else 80
    return dict(hw, jogos=[estimar(j, ig, ic, res, qualidade, upscaling) for j in JOGOS],
                equilibrio=equilibrio(ig, ic))


# ------------------------------------------------------------------ gargalo ao vivo
TITULOS = {"gpu": "Placa de vídeo no limite", "cpu": "Gargalo no processador", "vram": "Memória de vídeo cheia",
           "ram": "Memória RAM cheia", "termico": "Limitado pela temperatura", "folga": "Nada no limite"}
CONSELHOS = {
    "gpu": "Normal em jogos pesados: a placa de vídeo está sendo bem aproveitada. Para mais FPS, baixe a resolução ou a "
           "qualidade gráfica, ou ligue o DLSS/FSR.",
    "cpu": "O processador (ou um núcleo dele) não acompanha a placa de vídeo. Subir resolução e gráficos quase não custa FPS. "
           "Para ganhar FPS: reduza opções que pesam no processador (distância de visão, multidões, física), feche programas "
           "em segundo plano e confira se o XMP/EXPO da RAM está ligado na BIOS.",
    "vram": "A memória da placa de vídeo encheu, o que causa travadas. Reduza a qualidade das texturas ou a resolução.",
    "ram": "Pouca RAM livre: o Windows passa a usar o disco e o jogo engasga. Feche navegadores e outros programas.",
    "termico": "A temperatura alta está derrubando o clock. Verifique a ventilação do gabinete, a poeira e a pasta térmica.",
    "folga": "Nem o processador nem a placa de vídeo estão no limite: provavelmente há trava de FPS, V-Sync/G-Sync limitando, "
             "ou o jogo está no menu/pausa. Normal se o FPS já é o desejado.",
}


def classificar(a: dict) -> str:
    """Ordem: VRAM cheia, temperatura, GPU >= 90% (normal em jogo pesado), CPU/núcleo >= 85%, RAM cheia, folga."""
    def ge(chave, limite):
        return a.get(chave) is not None and a[chave] >= limite
    if ge("vram_pct", 97):
        return "vram"
    if a.get("gpu_termico") or ge("cpu_temp", 95):
        return "termico"
    if ge("gpu_carga", 90):
        return "gpu"
    if ge("cpu_nucleo", 85) or ge("cpu_carga", 85):
        return "cpu"
    if ge("ram_pct", 92):
        return "ram"
    return "folga"


class _CargaNucleos:
    """Carga de cada processador lógico entre duas leituras (sem mexer no cpu_percent usado pelo monitor)."""

    def __init__(self):
        self._antes = None

    def ler(self) -> list[float]:
        agora = psutil.cpu_times(percpu=True)
        antes, self._antes = self._antes, agora
        if antes is None or len(antes) != len(agora):
            return []
        cargas = []
        for a, b in zip(antes, agora):
            total = sum(b) - sum(a)
            ocioso = (b.idle - a.idle) + (getattr(b, "iowait", 0) - getattr(a, "iowait", 0))
            cargas.append(max(0.0, min(100.0, 100 * (1 - ocioso / total))) if total > 0 else 0.0)
        return cargas


_nucleos = _CargaNucleos()


def amostra(sens) -> dict:
    from .sensores import Sensores
    linhas, _ = sens.linhas()
    r = Sensores.resumo(linhas)
    nucleos = [(l[4], l[5]) for l in linhas if l[2] == "Cpu" and l[3] == "Load" and l[4].startswith("CPU Core #")
               and "Thread" not in l[4] and l[5] is not None]
    if nucleos:
        nome, maior = max(nucleos, key=lambda x: x[1])
        nome = nome.replace("CPU Core #", "núcleo ")
    else:
        cargas = _nucleos.ler()
        maior = max(cargas) if cargas else None
        nome = f"processador lógico {cargas.index(maior)}" if cargas else None
    vram_pct = (100 * r["vram_usada"] / r["vram_total"]) if r.get("vram_usada") and r.get("vram_total") else None
    termico = False
    h = sens.placa_nvidia()
    if h:
        motivos = gpu.nvml().limitadores(h)
        termico = bool(motivos and motivos & 0x60)  # redução de clock por temperatura (software ou hardware)
    a = {"gpu_carga": r.get("gpu_carga"), "cpu_carga": r.get("cpu_carga") if r.get("cpu_carga") is not None
         else psutil.cpu_percent(), "cpu_nucleo": maior, "nucleo_nome": nome, "vram_pct": vram_pct,
         "ram_pct": psutil.virtual_memory().percent, "gpu_temp": r.get("gpu_temp"), "cpu_temp": r.get("cpu_temp"),
         "gpu_termico": termico, "fps": None, "low1": None, "jogo": None}
    f = quadros.instancia().primeiro_plano()  # só há números com a contagem de FPS ligada (ETW)
    if f:
        a.update(fps=round(f["fps"], 1), low1=round(f["low1"], 1), jogo=f["jogo"])
    a["limite"] = classificar(a)
    return a


class Sessao:
    """Coleta uma amostra por segundo durante o jogo e monta o resultado no fim."""

    def __init__(self, sens, minutos: float, ao_terminar=None):
        self._sens = sens
        self.duracao = max(0.5, float(minutos)) * 60
        self.inicio = time.time()
        self.amostras: list[dict] = []
        self.cancelada = False
        self.resultado: dict | None = None
        self._ao_terminar = ao_terminar
        threading.Thread(target=self._rodar, daemon=True, name="gargalo").start()

    def _rodar(self) -> None:
        self._sens.reter("gargalo", True)
        quadros.instancia().reter("gargalo", True)
        try:
            while not self.cancelada and time.time() - self.inicio < self.duracao:
                try:
                    self.amostras.append(amostra(self._sens))
                except Exception:
                    log.exception("falha ao amostrar o gargalo")
                time.sleep(1)
            if not self.cancelada:
                self.resultado = self._montar()
                if self._ao_terminar:
                    self._ao_terminar(self.resultado)
        finally:
            self._sens.reter("gargalo", False)
            quadros.instancia().reter("gargalo", False)

    def _montar(self) -> dict:
        n = max(1, len(self.amostras))
        contagem: dict[str, int] = {}
        for a in self.amostras:
            contagem[a["limite"]] = contagem.get(a["limite"], 0) + 1
        distribuicao = sorted(({"limite": k, "titulo": TITULOS[k], "pct": round(100 * v / n)} for k, v in contagem.items()),
                              key=lambda d: -d["pct"])
        principal = distribuicao[0]["limite"] if distribuicao else "folga"

        def media(chave):
            v = [a[chave] for a in self.amostras if a.get(chave) is not None]
            return sum(v) / len(v) if v else None

        def maximo(chave):
            v = [a[chave] for a in self.amostras if a.get(chave) is not None]
            return max(v) if v else None
        return {"principal": principal, "titulo": TITULOS[principal], "conselho": CONSELHOS[principal],
                "distribuicao": distribuicao, "amostras": len(self.amostras),
                "gpu_media": media("gpu_carga"), "cpu_media": media("cpu_carga"), "nucleo_media": media("cpu_nucleo"),
                "vram_max": maximo("vram_pct"), "gpu_temp_max": maximo("gpu_temp"), "cpu_temp_max": maximo("cpu_temp"),
                "fps_medio": media("fps"), "low1_medio": media("low1"), "jogo": self._jogo(),
                "quando": time.strftime("%H:%M")}

    def _jogo(self) -> str | None:
        """O jogo que mais apareceu em primeiro plano durante a análise."""
        nomes = [a["jogo"] for a in self.amostras if a.get("jogo")]
        return max(set(nomes), key=nomes.count) if nomes else None

    def estado(self) -> dict:
        return {"rodando": self.resultado is None and not self.cancelada,
                "decorrido": round(time.time() - self.inicio), "duracao": round(self.duracao),
                "amostras": len(self.amostras), "resultado": self.resultado, "cancelada": self.cancelada}
