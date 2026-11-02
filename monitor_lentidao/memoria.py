"""Teste de memória (latência e banda, estilo AIDA64) e análise dos pentes de RAM."""

import ctypes
import json
import logging
import sys
import threading
from datetime import datetime
from pathlib import Path

import psutil

from . import config, windows
from .verificacoes import AVISO, Problema

log = logging.getLogger(__name__)

PASTA_NATIVO = Path(__file__).resolve().parent / "nativo"
TIPOS_DDR = {20: "DDR", 21: "DDR2", 24: "DDR3", 26: "DDR4", 29: "LPDDR3", 30: "LPDDR4", 34: "DDR5", 35: "LPDDR5"}
FORMATOS_ENCAIXE = {8: "DIMM", 12: "SO-DIMM"}  # pentes removíveis (desktop e notebook)

_SCRIPT_MODULOS = r"""
Get-CimInstance Win32_PhysicalMemory | Select-Object Capacity,Speed,ConfiguredClockSpeed,Manufacturer,
  PartNumber,DeviceLocator,SMBIOSMemoryType,FormFactor | ConvertTo-Json -Compress
"""


# ------------------------------------------------------------------ biblioteca nativa

_lib = None


def _biblioteca():
    global _lib
    if _lib is None:
        nome = "memteste.dll" if sys.platform == "win32" else "libmemteste.so"
        lib = ctypes.CDLL(str(PASTA_NATIVO / nome))
        lib.mt_caches.argtypes = [ctypes.POINTER(ctypes.c_int64)]
        lib.mt_latencia.restype = ctypes.c_double
        lib.mt_latencia.argtypes = [ctypes.c_int64, ctypes.c_double, ctypes.POINTER(ctypes.c_int)]
        lib.mt_banda.restype = ctypes.c_double
        lib.mt_banda.argtypes = [ctypes.c_int, ctypes.c_int64, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.mt_nucleos.argtypes = [ctypes.c_int]
        _lib = lib
    return _lib


def disponivel() -> bool:
    try:
        _biblioteca()
        return True
    except OSError:
        log.warning("biblioteca do teste de memória indisponível", exc_info=True)
        return False


# ------------------------------------------------------------------ pentes de memória

def modulos() -> list[dict]:
    saida = []
    for m in windows.powershell_json(_SCRIPT_MODULOS):
        saida.append({
            "local": m.get("DeviceLocator") or "",
            "fabricante": (m.get("Manufacturer") or "").strip(),
            "modelo": (m.get("PartNumber") or "").strip(),
            "gb": round((m.get("Capacity") or 0) / 2**30),
            "nominal": m.get("Speed") or 0,
            "atual": m.get("ConfiguredClockSpeed") or 0,
            "tipo": TIPOS_DDR.get(m.get("SMBIOSMemoryType"), ""),
            "removivel": m.get("FormFactor") in FORMATOS_ENCAIXE,
        })
    return saida


def analisar_modulos(mods: list[dict]) -> list[dict]:
    """Achados sobre a configuração da RAM: {nivel, titulo, texto}."""
    achados = []
    if not mods:
        return achados
    if len(mods) == 1 and mods[0]["removivel"]:
        achados.append({"nivel": "aviso", "titulo": "Só um pente de memória (canal único)",
                        "texto": "Com um pente só, a memória trabalha em canal único e entrega cerca de metade da "
                                 "velocidade possível. Adicionar um segundo pente igual (dual channel) é uma das "
                                 "melhorias mais baratas e perceptíveis."})
    lentos = [m for m in mods if m["nominal"] and m["atual"] and m["atual"] < m["nominal"] * 0.9]
    if lentos:
        m = lentos[0]
        achados.append({"nivel": "aviso", "titulo": "Memória abaixo da velocidade anunciada",
                        "texto": f"Os pentes suportam {m['nominal']} MT/s, mas estão rodando a {m['atual']} MT/s. "
                                 "Ative o perfil XMP (Intel) ou EXPO/DOCP (AMD) na BIOS para usar a velocidade "
                                 "completa."})
    tamanhos = {m["gb"] for m in mods}
    if len(mods) > 1 and len(tamanhos) > 1:
        achados.append({"nivel": "info", "titulo": "Pentes de tamanhos diferentes",
                        "texto": "Pentes diferentes funcionam, mas parte da memória pode ficar em canal único. "
                                 "Para o melhor desempenho, use pares iguais."})
    return achados


def problemas_modulos() -> list[Problema]:
    """Usado na verificação periódica: transforma os achados em alertas do monitor."""
    return [Problema(f"ram_{a['titulo']}", AVISO, a["titulo"], a["texto"],
                     "Veja os detalhes em Teste de memória no painel.", renotificar_min=7 * 24 * 60)
            for a in analisar_modulos(modulos()) if a["nivel"] == "aviso"]


# ------------------------------------------------------------------ teste

def _avaliar_latencia(ns: float) -> str:
    return "Excelente" if ns <= 75 else "Boa" if ns <= 95 else "Normal" if ns <= 120 else "Alta"


def executar(progresso=lambda fracao, etapa: None) -> dict:
    lib = _biblioteca()
    caches = (ctypes.c_int64 * 3)()
    lib.mt_caches(caches)
    l1, l2, l3 = (int(c) for c in caches)

    livre = psutil.virtual_memory().available
    bloco = int(min(512 * 2**20, max(64 * 2**20, livre // 4)))
    tamanhos = []
    t = 4 * 1024
    while t <= bloco:
        tamanhos.append(t)
        t *= 2

    passos = len(tamanhos) + 6
    feitos = 0

    def avancar(etapa):
        nonlocal feitos
        feitos += 1
        progresso(feitos / passos, etapa)

    curva = []
    paginas_grandes = ctypes.c_int(0)
    for tam in tamanhos:
        progresso(feitos / passos, f"Medindo latência com {_tamanho(tam)}...")
        curva.append({"bytes": tam, "ns": round(lib.mt_latencia(tam, 0.15, ctypes.byref(paginas_grandes)), 2)})
        avancar("")

    def latencia_em(alvo):
        candidatos = [c for c in curva if c["bytes"] <= alvo]
        return candidatos[-1]["ns"] if candidatos else None

    # Testa com uma thread por núcleo físico e com todos os threads (SMT); fica com o melhor.
    opcoes_threads = sorted({lib.mt_nucleos(0), lib.mt_nucleos(1)})
    threads_usados = {}
    isa = lib.mt_isa()
    banda_total = int(min(512 * 2**20, max(64 * 2**20, livre // 4)))
    banda = {}
    for tipo, nome, rotulo in ((0, "leitura", "leitura"), (1, "escrita", "escrita"), (2, "copia", "cópia")):
        progresso(feitos / passos, f"Medindo velocidade de {rotulo}...")
        # Melhor de 4 rodadas, em bytes/s -> MB/s decimais (como o AIDA64) e GB/s.
        bps, threads_usados[nome] = max((lib.mt_banda(tipo, banda_total, t, 3, 0), t)
                                        for t in opcoes_threads for _ in range(3))
        banda[nome] = round(bps / 1e9, 1)
        banda[nome + "_mb"] = round(bps / 1e6)
        avancar("")

    ram_ns = curva[-1]["ns"]
    progresso(feitos / passos, "Lendo informações dos pentes de memória...")
    mods = modulos()
    avancar("")

    achados = analisar_modulos(mods)
    teorico = None
    velocidade = max((m["atual"] or m["nominal"] for m in mods), default=0)
    if velocidade and mods:
        canais = 2 if len(mods) >= 2 or not mods[0]["removivel"] else 1
        teorico = round(velocidade * 8 * canais / 1000, 1)
        if banda["leitura"] < teorico * 0.45:
            achados.append({"nivel": "aviso", "titulo": "Velocidade de leitura abaixo do esperado",
                            "texto": f"A leitura ficou em {banda['leitura']} GB/s, menos da metade dos "
                                     f"~{teorico} GB/s que essa memória deveria entregar. Programas abertos durante "
                                     "o teste também puxam o resultado para baixo; feche-os e repita."})
    if ram_ns > 120:
        achados.append({"nivel": "aviso", "titulo": "Latência da memória alta",
                        "texto": f"Cada acesso à RAM levou {ram_ns:.0f} ns. O comum fica entre 60 e 110 ns. "
                                 "Verifique o XMP/EXPO na BIOS e se não há programas pesados rodando."})
    if sys.platform == "win32" and not paginas_grandes.value:
        achados.append({"nivel": "info", "titulo": "Latência medida com páginas normais",
                        "texto": "Para a latência ficar ainda mais próxima da do AIDA64, o Windows precisa permitir "
                                 "páginas grandes: em secpol.msc > Políticas locais > Atribuição de direitos de "
                                 "usuário > 'Bloquear páginas na memória', adicione seu usuário, reinicie o PC e "
                                 "abra o monitor como administrador."})
    if not [a for a in achados if a["nivel"] != "info"]:
        achados.append({"nivel": "ok", "titulo": "Memória funcionando bem",
                        "texto": "Os resultados estão dentro do esperado para esse tipo de memória."})

    resultado = {
        "quando": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "leitura": banda["leitura"], "escrita": banda["escrita"], "copia": banda["copia"],
        "latencia": round(ram_ns, 1), "avaliacao_latencia": _avaliar_latencia(ram_ns),
        "cache": [{"nivel": f"L{i + 1}", "bytes": tam, "tamanho": _tamanho(tam),
                   "ns": latencia_em(max(4096, tam // 2)) if tam else None}
                  for i, tam in enumerate((l1, l2, l3))],
        "leitura_mb": banda["leitura_mb"], "escrita_mb": banda["escrita_mb"], "copia_mb": banda["copia_mb"],
        "curva": curva, "threads": max(threads_usados.values()), "teorico": teorico,
        "instrucoes": {3: "AVX-512", 2: "AVX2", 1: "SSE2"}.get(isa, "?"),
        "paginas_grandes": bool(paginas_grandes.value),
        "modulos": mods, "achados": achados,
    }
    _salvar(resultado)
    progresso(1.0, "Concluído")
    return resultado


def _tamanho(b: int) -> str:
    if not b:
        return "?"
    return f"{b // 2**20} MB" if b >= 2**20 else f"{b // 1024} KB"


# ------------------------------------------------------------------ histórico

def _arquivo() -> Path:
    return config.pasta_dados() / "testes_memoria.json"


def historico() -> list[dict]:
    try:
        return json.loads(_arquivo().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _salvar(resultado: dict) -> None:
    resumo = {k: resultado[k] for k in ("quando", "leitura", "escrita", "copia", "latencia",
                                        "leitura_mb", "escrita_mb", "copia_mb")}
    _arquivo().write_text(json.dumps((historico() + [resumo])[-20:], ensure_ascii=False), encoding="utf-8")


class TesteEmSegundoPlano:
    """Roda o teste numa thread para o painel continuar respondendo."""

    def __init__(self):
        self.rodando = False
        self.progresso = 0.0
        self.etapa = ""
        self.resultado: dict | None = None
        self.erro = ""

    def iniciar(self) -> bool:
        if self.rodando:
            return False
        self.rodando, self.progresso, self.etapa, self.erro = True, 0.0, "Preparando...", ""
        threading.Thread(target=self._rodar, name="teste-memoria", daemon=True).start()
        return True

    def _rodar(self):
        try:
            self.resultado = executar(self._avancar)
        except Exception as e:
            log.exception("falha no teste de memória")
            self.erro = f"Não foi possível concluir o teste: {e}"
        finally:
            self.rodando = False

    def _avancar(self, fracao, etapa):
        self.progresso = fracao
        if etapa:
            self.etapa = etapa

    def estado(self) -> dict:
        return {"rodando": self.rodando, "progresso": round(self.progresso, 3), "etapa": self.etapa,
                "erro": self.erro, "resultado": self.resultado, "historico": list(reversed(historico()))}
