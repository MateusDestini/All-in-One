"""Aba do processador: detecção, teste de estabilidade por núcleo e ajuste automático.

- Ryzen 7000/9000: `oc_ryzen` (PBO, temperatura, boost override, Curve Optimizer por núcleo).
- Intel Core: `oc_intel` (PL1/PL2/Tau, IccMax, undervolt).
O acesso ao hardware é pelo driver PawnIO (ver `pawnio.py`). Tudo volta ao padrão da BIOS ao reiniciar.

Teste de estabilidade: no estilo do CoreCycler, prende uma carga de um núcleo só em cada núcleo físico por vez
(é com um núcleo sozinho que o boost vai mais alto e o Curve Optimizer falha). A carga confere as próprias contas
(`nativo/estavel.c`) e o monitor lê os erros de hardware (WHEA) do Windows depois de cada núcleo.

Ajuste automático: desce o Curve Optimizer (ou o undervolt no Intel) em passos, testando cada passo. Antes de cada
passo grava um diário em disco; se o PC travar e reiniciar, na próxima vez o monitor sabe qual valor derrubou o PC,
marca como instável e oferece continuar de onde parou. Como os valores do SMU/MSR somem ao reiniciar, uma queda
nunca deixa o PC preso numa configuração ruim.
"""

import ctypes
import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import psutil

from . import config, oc_intel, oc_ryzen, pawnio, sensores, windows

log = logging.getLogger(__name__)

PASTA_NATIVO = Path(__file__).resolve().parent / "nativo"
TEMP_LIMITE = 95.0

_proc = None
_proc_lido = 0.0
_motivo = None
NOVA_TENTATIVA = 10  # segundos: sem processador (driver ausente, sem admin), tenta detectar de novo depois disso


# ------------------------------------------------------------------ detecção
def processador():
    """O processador ajustável deste PC (ou None, com o motivo em `motivo()`)."""
    global _proc, _proc_lido, _motivo
    if _proc_lido and (_proc is not None or time.time() - _proc_lido < NOVA_TENTATIVA):
        return _proc
    _proc_lido = time.time()
    _proc, _motivo = None, None
    fab, familia, modelo = pawnio.cpu_fms()
    nome = sensores.nome_cpu() or "Processador"
    if fab not in ("amd", "intel"):
        _motivo = "Processador não reconhecido."
        return None
    if not windows.EH_WINDOWS:
        _motivo = "Só funciona no Windows."
        return None
    if fab == "amd" and (familia, modelo) not in oc_ryzen.SUPORTADOS:
        _motivo = (f"Por enquanto o ajuste direto funciona nos Ryzen 7000 e 9000 de desktop. Este é família "
                   f"{familia:#x} modelo {modelo:#x}.")
        return None
    if not pawnio.disponivel():
        _motivo = "sem_pawnio"
        return None
    if not windows.eh_admin():
        _motivo = "sem_admin"
        return None
    try:
        _proc = oc_ryzen.detectar(nome, familia, modelo) if fab == "amd" else oc_intel.detectar(nome)
        if _proc is None:
            _motivo = "O driver PawnIO não aceitou este processador."
    except pawnio.ErroPawnIO as e:
        _motivo = f"O PawnIO não abriu: {e.texto}."
        log.warning("PawnIO: %s", e)
    except Exception as e:
        _motivo = f"Não consegui falar com o processador: {e}"
        log.exception("falha ao abrir o processador")
    return _proc


def esquecer() -> None:
    global _proc, _proc_lido, _motivo
    _proc, _proc_lido, _motivo = None, 0.0, None


def motivo() -> str | None:
    return _motivo


def presenca() -> dict:
    fab, _, _ = pawnio.cpu_fms()
    return {"cpu": fab in ("amd", "intel"), "marca": fab}


def placas() -> list:
    p = processador()
    return [p] if p else []


# ------------------------------------------------------------------ topologia
def nucleos_fisicos() -> list[dict]:
    """[{indice, lp (primeiro processador lógico), eficiencia}] na ordem do Windows."""
    if windows.EH_WINDOWS:
        try:
            return _nucleos_windows()
        except Exception:
            log.exception("falha ao ler a topologia")
    fisicos = psutil.cpu_count(logical=False) or 1
    logicos = psutil.cpu_count() or fisicos
    passo = max(1, logicos // fisicos)
    return [{"indice": i, "lp": i * passo, "eficiencia": 0} for i in range(fisicos)]


def _nucleos_windows() -> list[dict]:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    tam = ctypes.c_uint32(0)
    k.GetLogicalProcessorInformationEx(0, None, ctypes.byref(tam))
    buf = ctypes.create_string_buffer(tam.value)
    if not k.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(tam)):
        raise OSError(ctypes.get_last_error())
    dados, pos, saida = buf.raw, 0, []
    while pos < tam.value:
        rel, tamanho = int.from_bytes(dados[pos:pos + 4], "little"), int.from_bytes(dados[pos + 4:pos + 8], "little")
        if rel == 0 and tamanho >= 48:
            eficiencia = dados[pos + 9]
            mascara = int.from_bytes(dados[pos + 32:pos + 40], "little")
            grupo = int.from_bytes(dados[pos + 40:pos + 42], "little")
            if mascara:
                lp = (mascara & -mascara).bit_length() - 1
                saida.append({"lp": lp + 64 * grupo, "grupo": grupo, "eficiencia": eficiencia})
        pos += tamanho or 1
    saida.sort(key=lambda n: n["lp"])
    for i, n in enumerate(saida):
        n["indice"] = i
    return saida


def _prender(lp: int) -> None:
    if windows.EH_WINDOWS:
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentThread.restype = ctypes.c_void_p
        k.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        k.SetThreadAffinityMask.restype = ctypes.c_size_t
        if lp < 64:
            k.SetThreadAffinityMask(k.GetCurrentThread(), 1 << lp)
        k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        k.SetThreadPriority(k.GetCurrentThread(), 1)  # acima do normal: a carga não pode ser atrapalhada
    elif hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, {lp})
        except OSError:
            pass


_dll = None


def _estavel():
    global _dll
    if _dll is None:
        nome = "estavel.dll" if windows.EH_WINDOWS else "libestavel.so"
        d = ctypes.CDLL(str(PASTA_NATIVO / nome))
        d.estavel_rodar.restype = ctypes.c_int
        d.estavel_rodar.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.POINTER(ctypes.c_uint64),
                                    ctypes.POINTER(ctypes.c_int)]
        _dll = d
    return _dll


# ------------------------------------------------------------------ erros de hardware desde um instante
_SCRIPT_WHEA = r"""
$t=[datetime]::Parse('%(desde)s')
$e=@(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-WHEA-Logger';StartTime=$t} -ErrorAction SilentlyContinue | Where-Object { $_.Id -in 1,18,19 })
[pscustomobject]@{Total=$e.Count;Graves=@($e | Where-Object { $_.Id -ne 19 }).Count} | ConvertTo-Json -Compress
"""
_SCRIPT_QUEDAS = r"""
$t=[datetime]::Parse('%(desde)s')
@(Get-WinEvent -FilterHashtable @{LogName='System';Id=41,6008,1001;StartTime=$t} -ErrorAction SilentlyContinue | Where-Object { $_.ProviderName -match 'Kernel-Power|EventLog|WER-SystemErrorReporting' }).Count | ConvertTo-Json -Compress
"""


def _desde(instante: float) -> str:
    return datetime.fromtimestamp(instante).strftime("%Y-%m-%dT%H:%M:%S")


def whea_desde(instante: float) -> int:
    """Erros de processador (machine check) registrados pelo Windows desde o instante."""
    r = windows.powershell_json(_SCRIPT_WHEA % {"desde": _desde(instante)}, timeout=40)
    return int(r[0].get("Total") or 0) if r and isinstance(r[0], dict) else 0


def quedas_desde(instante: float) -> int:
    if not windows.EH_WINDOWS:
        return 0
    saida = windows.executar(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              _SCRIPT_QUEDAS % {"desde": _desde(instante)}], 60).strip()
    try:
        return int(json.loads(saida or "0"))
    except (ValueError, TypeError):
        return 0


# ------------------------------------------------------------------ teste por núcleo
class TesteCpu:
    """Roda a carga que confere as próprias contas em cada núcleo, um por vez."""

    def __init__(self, indices: list[int] | None = None, segundos: float = 30, modo: int = 3, ao_fim=None,
                 ler_whea: bool = True, sens=None):
        todos = nucleos_fisicos()
        self.nucleos = [n for n in todos if indices is None or n["indice"] in indices]
        self.segundos = max(3.0, float(segundos))
        self.modo = modo
        self.ler_whea = ler_whea and windows.EH_WINDOWS
        self.resultados: list[dict] = []
        self.atual: int | None = None
        self.cancelado = False
        self.motivo_fim = None
        self.temp_max = None
        self.inicio = time.time()
        self.fim = None
        self._parar = ctypes.c_int(0)
        self._ao_fim = ao_fim
        self._sens = sens if sens is not None else sensores.instancia()
        self._fio = threading.Thread(target=self._rodar, daemon=True, name="teste-cpu")
        self._fio.start()

    @property
    def rodando(self) -> bool:
        return self.fim is None

    def cancelar(self, motivo: str = "Cancelado") -> None:
        self.cancelado = True
        self.motivo_fim = self.motivo_fim or motivo
        self._parar.value = 1

    def aguardar(self, timeout: float | None = None) -> None:
        self._fio.join(timeout)

    def _temp(self) -> float | None:
        try:
            return self._sens.leitura(com_linhas=False)["resumo"].get("cpu_temp")
        except Exception:
            return None

    def _rodar(self) -> None:
        if self._sens:
            self._sens.reter("teste-cpu", True)
        try:
            dll = _estavel()
            for n in self.nucleos:
                if self.cancelado:
                    break
                self.atual = n["indice"]
                r = {"nucleo": n["indice"], "erros": 0, "blocos": 0, "whea": 0, "inicio": time.time()}
                self.resultados.append(r)
                fio = threading.Thread(target=self._carga, args=(dll, n["lp"], r), daemon=True)
                fio.start()
                while fio.is_alive():
                    fio.join(0.5)
                    t = self._temp()
                    if t is not None:
                        self.temp_max = max(self.temp_max or 0, t)
                        if t >= TEMP_LIMITE:
                            self.cancelar(f"Processador chegou a {t:.0f} °C")
                if self.ler_whea:
                    r["whea"] = whea_desde(r["inicio"] - 1)
                r["fim"] = time.time()
            self.motivo_fim = self.motivo_fim or "Concluído"
        except Exception as e:
            log.exception("teste do processador falhou")
            self.motivo_fim = f"Falhou: {e}"
        finally:
            self.atual = None
            self.fim = time.time()
            if self._sens:
                self._sens.reter("teste-cpu", False)
            if self._ao_fim:
                try:
                    self._ao_fim(self)
                except Exception:
                    log.exception("ao_fim do teste")

    def _carga(self, dll, lp: int, r: dict) -> None:
        _prender(lp)
        fim = time.time() + self.segundos
        semente = int(time.time() * 1000) ^ (lp << 20)
        while time.time() < fim and not self._parar.value:
            ms = int(min(1000, max(50, (fim - time.time()) * 1000)))
            blocos = ctypes.c_uint64()
            r["erros"] += dll.estavel_rodar(ms, self.modo, semente, ctypes.byref(blocos), ctypes.byref(self._parar))
            r["blocos"] += blocos.value
            semente += 1

    @property
    def falhou(self) -> bool:
        return any(r["erros"] or r["whea"] for r in self.resultados)

    def estado(self) -> dict:
        total = len(self.nucleos) * self.segundos
        feito = sum(min(self.segundos, (r.get("fim") or time.time()) - r["inicio"]) for r in self.resultados)
        return {"rodando": self.rodando, "progresso": 1.0 if not self.rodando else min(0.99, feito / total if total else 0),
                "atual": self.atual, "resultados": [{k: v for k, v in r.items() if k in ("nucleo", "erros", "blocos", "whea")}
                                                     for r in self.resultados],
                "temp_max": self.temp_max, "motivo": self.motivo_fim, "falhou": self.falhou,
                "segundos": self.segundos, "nucleos": len(self.nucleos)}


# ------------------------------------------------------------------ ajuste automático
DIARIO = "cpu_ajuste.json"
OBJETIVOS = ("desempenho", "equilibrio", "eficiencia")
TEMPOS = {"rapido": {"todos": 12, "nucleo": 30}, "completo": {"todos": 40, "nucleo": 120}}


def _diario_ler() -> dict | None:
    arq = config.pasta_dados() / DIARIO
    try:
        return json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else None
    except (OSError, ValueError):
        return None


def _diario_gravar(d: dict | None) -> None:
    arq = config.pasta_dados() / DIARIO
    if d is None:
        arq.unlink(missing_ok=True)
        return
    tmp = arq.with_suffix(".tmp")
    # gravado no disco de verdade antes de mexer no processador: o PC pode cair no passo seguinte
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False, indent=1))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, arq)


def plano_ajuste(proc, objetivo: str, duracao: str) -> dict:
    """Monta o diário inicial: limites conforme o objetivo, depois a busca de tensão."""
    info = proc.info()
    marca = info["marca"]
    limites = {}
    if marca == "amd":
        if objetivo == "desempenho":
            limites = {"ppt": min(300, round(proc.padroes["ppt"] * 1.4)), "tdc": min(250, round(proc.padroes["tdc"] * 1.3)),
                       "edc": min(300, round(proc.padroes["edc"] * 1.3)), "temp": 95, "boost": 200}
        elif objetivo == "eficiencia":
            limites = {"ppt": oc_ryzen.PADRAO_TDP[65][0], "tdc": oc_ryzen.PADRAO_TDP[65][1],
                       "edc": oc_ryzen.PADRAO_TDP[65][2], "temp": 85, "boost": 0}
        else:
            limites = {k: proc.padroes[k] for k in ("ppt", "tdc", "edc")}
            limites.update(temp=95, boost=0)
        passos = list(range(-5, -31, -5)) if duracao == "rapido" else list(range(-5, -41, -5))
        unidade, margem, por_nucleo = "", 3, bool(info.get("por_nucleo"))
    else:
        ctrl = {c["id"]: c for c in proc.controles()}
        tdp = proc._tdp() or 65
        if objetivo == "eficiencia" and "pl1" in ctrl:
            limites = {"pl1": tdp, "pl2": round(tdp * 1.5)}
        elif objetivo == "desempenho" and "pl1" in ctrl:
            limites = {"pl1": ctrl["pl2"]["atual"], "pl2": ctrl["pl2"]["atual"]}
        passos = list(range(-10, -101, -10)) if duracao == "rapido" else list(range(-10, -151, -10))
        unidade, margem, por_nucleo = "mV", 10, False
    nucleos = [n["indice"] for n in nucleos_fisicos()]
    return {"versao": 1, "marca": marca, "objetivo": objetivo, "duracao": duracao, "inicio": time.time(),
            "limites": limites, "passos": passos, "unidade": unidade, "margem": margem, "por_nucleo": por_nucleo,
            "fase": "limites", "nucleos": nucleos, "estavel_todos": 0, "melhor": {}, "instavel": {},
            "fila": [], "testando": None, "historico": [], "pausa": None, "final": None}


class AjusteAuto:
    """Executa (ou continua) o ajuste descrito no diário, numa thread."""

    def __init__(self, proc, diario: dict, ao_mudar=None, sens=None):
        self.proc = proc
        self.d = diario
        self.teste: TesteCpu | None = None
        self.cancelado = False
        self.erro = None
        self._ao_mudar = ao_mudar
        self._sens = sens
        self._fio = threading.Thread(target=self._rodar, daemon=True, name="ajuste-cpu")
        self._fio.start()

    @property
    def rodando(self) -> bool:
        return self._fio.is_alive()

    def cancelar(self) -> None:
        self.cancelado = True
        if self.teste:
            self.teste.cancelar("Ajuste cancelado")

    def aguardar(self, timeout=None) -> None:
        self._fio.join(timeout)

    # ------------------------------------------------------------ passos
    def _log(self, texto: str) -> None:
        self.d["historico"].append([time.strftime("%H:%M:%S"), texto])
        self.d["historico"] = self.d["historico"][-200:]

    def _valores_co(self, padrao: int) -> dict:
        """Valores de tensão para todos os núcleos (melhor conhecido de cada um, ou o valor geral)."""
        if self.d["marca"] == "intel":
            return {"uv": padrao}
        if not self.d["por_nucleo"]:
            return {"co": padrao}
        return {f"co_{i}": self.d["melhor"].get(str(i), padrao) for i in self.d["nucleos"]}

    def _aplicar(self, valores: dict) -> bool:
        itens = self.proc.aplicar(valores, so_mudancas=False)
        falhas = [i for i in itens if not i["ok"]]
        if falhas:
            self._log("Não aceito: " + "; ".join(f"{i['id']} ({i['mensagem']})" for i in falhas))
        return not falhas

    def _testar(self, indices: list[int] | None, segundos: float, rotulo: dict) -> bool | None:
        """True = estável, False = instável, None = cancelado."""
        self.d["testando"] = dict(rotulo, desde=time.time())
        _diario_gravar(self.d)
        self._avisar()
        self.teste = TesteCpu(indices, segundos, modo=3, sens=self._sens)
        self.teste.aguardar()
        t, self.teste = self.teste, None
        self.d["testando"] = None
        if self.cancelado or (t.cancelado and not t.motivo_fim.startswith("Processador chegou")):
            return None
        if t.cancelado:  # temperatura
            self._log(t.motivo_fim)
            return False
        if t.falhou:
            ruins = [f"núcleo {r['nucleo']}" + (f" ({r['erros']} erros de conta)" if r["erros"] else "") +
                     (f" ({r['whea']} erros WHEA)" if r["whea"] else "") for r in t.resultados if r["erros"] or r["whea"]]
            self._log("Instável: " + ", ".join(ruins))
            return False
        return True

    def _avisar(self) -> None:
        if self._ao_mudar:
            try:
                self._ao_mudar(self)
            except Exception:
                pass

    def _rodar(self) -> None:
        try:
            self._executar()
        except Exception as e:
            log.exception("ajuste automático falhou")
            self.erro = str(e)
            self.d["pausa"] = {"motivo": "erro", "texto": f"O ajuste parou por um erro: {e}"}
            _diario_gravar(self.d)
        finally:
            self._avisar()

    def _executar(self) -> None:
        d = self.d
        d["pausa"] = None
        tempos = TEMPOS.get(d["duracao"], TEMPOS["rapido"])
        un = d["unidade"]
        if d["fase"] == "limites":
            if d["limites"]:
                self._log("Aplicando limites: " + ", ".join(f"{k} {v}" for k, v in d["limites"].items()))
                if not self._aplicar(d["limites"]) and "boost" in d["limites"]:
                    d["limites"].pop("boost", None)  # sem PBO na BIOS: segue sem o boost override
                    self._aplicar(d["limites"])
            d["fase"] = "todos"
            _diario_gravar(d)
        if d["fase"] == "todos":
            for v in d["passos"]:
                if v >= d["estavel_todos"] and d["estavel_todos"] != 0:
                    continue
                if d["instavel"].get("todos") is not None and v <= d["instavel"]["todos"]:
                    break
                self._log(f"Testando todos os núcleos em {v}{un}")
                if not self._aplicar(self._valores_co(v)):
                    d["instavel"]["todos"] = v
                    break
                ok = self._testar(None, tempos["todos"], {"fase": "todos", "valor": v})
                if ok is None:
                    return self._pausar("cancelado")
                if not ok:
                    d["instavel"]["todos"] = v
                    break
                d["estavel_todos"] = v
                self._log(f"Estável em {v}{un}")
                _diario_gravar(d)
            self._aplicar(self._valores_co(d["estavel_todos"]))
            d["melhor"] = {str(i): d["estavel_todos"] for i in d["nucleos"]}
            d["fase"] = "nucleos" if d["por_nucleo"] else "final"
            d["fila"] = list(d["nucleos"])
            _diario_gravar(d)
        if d["fase"] == "nucleos":
            passo = 5
            while d["fila"]:
                i = d["fila"][0]
                chave = str(i)
                v = d["melhor"].get(chave, d["estavel_todos"]) - passo
                limite = min(d["passos"]) if d["passos"] else -30
                ruim = d["instavel"].get(chave)
                if v < limite or (ruim is not None and v <= ruim):
                    d["fila"].pop(0)
                    _diario_gravar(d)
                    continue
                self._log(f"Núcleo {i} em {v}")
                valores = self._valores_co(d["estavel_todos"])
                valores[f"co_{i}"] = v
                if not self._aplicar(valores):
                    d["instavel"][chave] = v
                    continue
                ok = self._testar([i], tempos["nucleo"], {"fase": "nucleos", "nucleo": i, "valor": v})
                if ok is None:
                    return self._pausar("cancelado")
                if ok:
                    d["melhor"][chave] = v
                else:
                    d["instavel"][chave] = v
                    d["fila"].pop(0)
                    self._aplicar({f"co_{i}": d["melhor"].get(chave, d["estavel_todos"])})
                _diario_gravar(d)
            d["fase"] = "final"
        if d["fase"] == "final":
            self._finalizar()

    def _finalizar(self) -> None:
        d = self.d
        m = d["margem"]
        if d["marca"] == "intel":
            final = {"uv": min(0, d["estavel_todos"] + m)}
        elif d["por_nucleo"]:
            final = {f"co_{i}": min(0, d["melhor"].get(str(i), d["estavel_todos"]) + m) for i in d["nucleos"]}
        else:
            final = {"co": min(0, d["estavel_todos"] + m)}
        final.update(d["limites"])
        self._aplicar(final)
        d["final"] = final
        d["fase"] = "fim"
        d["fim"] = time.time()
        self._log("Ajuste concluído. Valores finais com margem de segurança aplicados.")
        _diario_gravar(d)

    def _pausar(self, motivo: str) -> None:
        self.d["pausa"] = {"motivo": motivo, "texto": "Ajuste pausado. Dá para continuar de onde parou."}
        self._log("Pausado")
        try:
            self.proc.padrao()
        except Exception:
            pass
        _diario_gravar(self.d)


_ajuste: AjusteAuto | None = None
_teste_avulso: TesteCpu | None = None


def iniciar_ajuste(objetivo: str, duracao: str) -> dict:
    global _ajuste
    p = processador()
    if not p:
        return {"ok": False, "mensagem": "Processador indisponível."}
    if _ajuste and _ajuste.rodando:
        return {"ok": False, "mensagem": "O ajuste já está rodando."}
    if objetivo not in OBJETIVOS:
        objetivo = "equilibrio"
    d = plano_ajuste(p, objetivo, "completo" if duracao == "completo" else "rapido")
    _diario_gravar(d)
    _ajuste = AjusteAuto(p, d)
    return {"ok": True, "mensagem": "Ajuste automático começou. Não use o PC até terminar."}


def continuar_ajuste() -> dict:
    global _ajuste
    p = processador()
    d = _diario_ler()
    if not p or not d or d.get("fase") == "fim":
        return {"ok": False, "mensagem": "Nada para continuar."}
    if _ajuste and _ajuste.rodando:
        return {"ok": False, "mensagem": "O ajuste já está rodando."}
    _ajuste = AjusteAuto(p, d)
    return {"ok": True, "mensagem": "Continuando o ajuste."}


def cancelar_ajuste() -> dict:
    if _ajuste and _ajuste.rodando:
        _ajuste.cancelar()
        return {"ok": True, "mensagem": "Cancelando: o processador volta ao padrão."}
    return {"ok": True, "mensagem": "Nada rodando."}


def descartar_ajuste() -> dict:
    if _ajuste and _ajuste.rodando:
        return {"ok": False, "mensagem": "Cancele o ajuste antes."}
    _diario_gravar(None)
    return {"ok": True, "mensagem": "Ajuste descartado."}


def verificar_queda() -> str | None:
    """Chamado quando o monitor abre: se o PC caiu no meio de um passo, marca o valor como instável."""
    d = _diario_ler()
    if not d or not d.get("testando"):
        return None
    t = d["testando"]
    caiu = quedas_desde(float(t.get("desde", 0)) - 5) > 0  # reinício normal pelo usuário não conta como queda
    d["testando"] = None
    if caiu:
        chave = "todos" if t.get("fase") == "todos" else str(t.get("nucleo"))
        d["instavel"][chave] = t.get("valor")
        if t.get("fase") == "nucleos" and d.get("fila") and d["fila"][0] == t.get("nucleo"):
            d["fila"].pop(0)
        if t.get("fase") == "todos":
            d["melhor"] = {str(i): d.get("estavel_todos", 0) for i in d["nucleos"]}
            d["fase"] = "nucleos" if d.get("por_nucleo") else "final"
            d["fila"] = list(d["nucleos"])
        alvo = "todos os núcleos" if t.get("fase") == "todos" else f"o núcleo {t.get('nucleo')}"
        texto = (f"O PC reiniciou testando {alvo} em {t.get('valor')}{d.get('unidade', '')}. Esse valor ficou marcado "
                 "como instável; clique em Continuar para terminar o ajuste.")
        d["pausa"] = {"motivo": "queda", "texto": texto}
        d["historico"].append([time.strftime("%H:%M:%S"), "O PC reiniciou neste passo: valor instável"])
    else:
        d["pausa"] = {"motivo": "interrompido", "texto": "O ajuste foi interrompido (o monitor fechou). Dá para continuar."}
    _diario_gravar(d)
    return d["pausa"]["texto"] if caiu else None


def estado_ajuste() -> dict | None:
    d = _ajuste.d if _ajuste else _diario_ler()
    if not d:
        return None
    teste = _ajuste.teste.estado() if _ajuste and _ajuste.teste else None
    return {"rodando": bool(_ajuste and _ajuste.rodando), "fase": d.get("fase"), "objetivo": d.get("objetivo"),
            "duracao": d.get("duracao"), "marca": d.get("marca"), "unidade": d.get("unidade"),
            "estavel_todos": d.get("estavel_todos"), "melhor": d.get("melhor"), "instavel": d.get("instavel"),
            "fila": d.get("fila"), "passos": d.get("passos"), "nucleos": d.get("nucleos"),
            "por_nucleo": d.get("por_nucleo"), "testando": d.get("testando"), "teste": teste,
            "historico": d.get("historico", [])[-30:], "pausa": d.get("pausa"), "final": d.get("final"),
            "inicio": d.get("inicio"), "fim": d.get("fim")}


def iniciar_teste(segundos: float, indices: list[int] | None = None) -> dict:
    global _teste_avulso
    if (_teste_avulso and _teste_avulso.rodando) or (_ajuste and _ajuste.rodando):
        return {"ok": False, "mensagem": "Já há um teste rodando."}
    _teste_avulso = TesteCpu(indices, segundos, modo=3)
    return {"ok": True, "mensagem": "Teste de estabilidade começou."}


def cancelar_teste() -> dict:
    if _teste_avulso and _teste_avulso.rodando:
        _teste_avulso.cancelar()
    return {"ok": True}


def estado_teste() -> dict | None:
    return _teste_avulso.estado() if _teste_avulso else None


def encerrar() -> None:
    if _ajuste and _ajuste.rodando:
        _ajuste.cancelar()
    if _teste_avulso and _teste_avulso.rodando:
        _teste_avulso.cancelar()


def em_uso() -> bool:
    return bool((_ajuste and _ajuste.rodando) or (_teste_avulso and _teste_avulso.rodando))

