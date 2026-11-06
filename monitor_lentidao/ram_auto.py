"""Auto-tune automático de RAM para AMD AM4/AM5 via AOD.

Orquestra um loop completo: aplica perfil via AOD → reinicia → valida timings → testa estabilidade →
avança se estável / reverte se falhar. Persiste estado em JSON entre reboots (padrão oc_cpu).

Reutiliza: aod.aplicar/conferir/restaurar/reiniciar_pc, ram_timings.sugestoes/iniciar_teste/melhor,
oc_cpu.quedas_desde (watchdog de crash).
"""

import json
import logging
import os
import time
from datetime import datetime

from . import aod, config, oc_cpu, ram_timings, windows

log = logging.getLogger(__name__)

DIARIO = "ram_auto_estado.json"
RELATORIO = "ram_auto_relatorio.json"

# Duração dos testes por nível (minutos)
MINUTOS_PADRAO = {"perfil": 5, "afinado": 15, "agressivo": 30}
TEMP_MAX_PADRAO = {"DDR5": 55, "DDR4": 60}
MAX_FALHAS_PADRAO = 3


def _arquivo(nome: str):
    return config.pasta_dados() / nome


def _ler_diario() -> dict | None:
    try:
        return json.loads(_arquivo(DIARIO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gravar_diario(d: dict) -> None:
    """Escrita atômica com fsync para garantir que o estado sobrevive ao reboot."""
    caminho = _arquivo(DIARIO)
    tmp = caminho.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, caminho)
    except OSError:
        log.exception("falha ao gravar diário do auto-tune")


def _apagar_diario() -> None:
    try:
        _arquivo(DIARIO).unlink(missing_ok=True)
    except OSError:
        pass


def _config_padrao():
    p = config.PADRAO if hasattr(config, "PADRAO") else {}
    return {
        "niveis": p.get("ram_auto_niveis", ["perfil", "afinado", "agressivo"]),
        "minutos": p.get("ram_auto_minutos", MINUTOS_PADRAO),
        "temp_max": p.get("ram_auto_temp_max", TEMP_MAX_PADRAO),
        "max_falhas": p.get("ram_auto_max_falhas", MAX_FALHAS_PADRAO),
    }


def _assinatura_atual() -> dict | None:
    atual = ram_timings.leitura_atual(True)
    if not atual.get("ok"):
        return None
    t = atual.get("timings") or {}
    c = atual.get("clocks") or {}
    return {
        "mts": atual.get("mts"),
        "cl": t.get("cl"),
        "trcd": t.get("trcd"),
        "trp": t.get("trp"),
        "tras": t.get("tras"),
        "trfc_ns": t.get("trfc_ns"),
        "trefi": t.get("trefi"),
        "fclk": c.get("fclk"),
        "modo_uclk": c.get("modo_uclk"),
    }


# ------------------------------------------------------------------ máquina de estados

def iniciar(niveis: list[str] | None = None) -> dict:
    """Inicia o loop de auto-tune. Retorna {ok, mensagem}."""
    if not windows.EH_WINDOWS:
        return {"ok": False, "mensagem": "Auto-tune só funciona no Windows."}
    if not windows.eh_admin():
        return {"ok": False, "mensagem": "É preciso rodar como administrador."}
    lista_aod = aod.listar(True)
    if not lista_aod.get("ok"):
        return {"ok": False, "mensagem": "A interface AOD da BIOS não está disponível. Verifique se a placa suporta."}
    if ram_timings.em_uso():
        return {"ok": False, "mensagem": "Um teste de RAM já está rodando. Espere terminar."}
    d = _ler_diario()
    if d and d.get("fase") not in (None, "ocioso", "final"):
        return {"ok": False, "mensagem": f"Auto-tune já está em andamento (fase: {d['fase']}). Cancele antes de reiniciar."}

    cfg = _config_padrao()
    fila = [n for n in (niveis or cfg["niveis"]) if n in ("perfil", "afinado", "agressivo")]
    if not fila:
        return {"ok": False, "mensagem": "Nenhum nível válido selecionado."}

    # Snapshot do estado atual para rollback final
    original = aod._ler_json("aod_original.json") or {"quando": time.time(), "valores": {}}
    melhor_sig = _assinatura_atual()

    novo = {
        "versao": 1,
        "inicio": time.time(),
        "fase": "planejamento",
        "nivel_atual": None,
        "fila": fila,
        "melhor_atual": melhor_sig,
        "config_original": original,
        "resultados": {},
        "descartados": [],
        "testando": None,
        "pausa": None,
        "reboots_realizados": 0,
        "falhas_consecutivas": 0,
    }
    _gravar_diario(novo)
    # Avança imediatamente para o primeiro nível
    _avancar_proximo_nivel(novo, cfg)
    return {"ok": True, "mensagem": f"Auto-tune iniciado. Primeiro perfil: {novo['nivel_atual']}."}


def _avancar_proximo_nivel(d: dict, cfg: dict) -> None:
    """Move para o próximo nível na fila e inicia a fase 'aplicar'."""
    fila = d.get("fila", [])
    descartados = set(d.get("descartados", []))
    proximo = None
    for n in fila:
        if n not in descartados and n not in d.get("resultados", {}):
            proximo = n
            break
    if not proximo:
        d["fase"] = "final"
        d["nivel_atual"] = None
        _gravar_diario(d)
        _finalizar(d)
        return
    d["nivel_atual"] = proximo
    d["fase"] = "aplicar"
    d["testando"] = {"nivel": proximo, "quando": time.time()}
    d["pausa"] = None
    _gravar_diario(d)
    _executar_aplicar(d, cfg)


def _executar_aplicar(d: dict, cfg: dict) -> None:
    """Aplica o nível atual via AOD e reinicia."""
    nivel_id = d["nivel_atual"]
    sugestoes = ram_timings.sugestoes(ram_timings.kit(), ram_timings.leitura_atual(), ram_timings.plataforma())
    niveis_map = {n["id"]: n for n in sugestoes.get("niveis", [])}
    nivel = niveis_map.get(nivel_id)
    if not nivel or not nivel.get("valores"):
        d["descartados"].append(nivel_id)
        d["resultados"][nivel_id] = {"motivo": "sem_valores"}
        _avancar_proximo_nivel(d, cfg)
        return

    r = aod.aplicar(nivel["valores"], origem=f"auto_{nivel_id}")
    if not r.get("ok"):
        d["pausa"] = {"motivo": f"A BIOS não aceitou os valores do nível {nivel_id}: {r.get('mensagem', '')}", "quando": time.time()}
        d["fase"] = "pausado"
        _gravar_diario(d)
        return

    d["reboots_realizados"] = d.get("reboots_realizados", 0) + 1
    d["fase"] = "aguardando_boot"
    _gravar_diario(d)
    aod.reiniciar_pc()


def verificar_estado() -> str | None:
    """Chamado na inicialização (bandeja.py). Detecta crash e retoma o loop após reboot."""
    d = _ler_diario()
    if not d or d.get("fase") in (None, "ocioso", "final"):
        return None

    cfg = _config_padrao()

    # Watchdog: se estava aguardando boot e houve queda, reverte
    if d.get("fase") == "aguardando_boot" and d.get("testando"):
        quedas = oc_cpu.quedas_desde(d["testando"]["quando"])
        if quedas:
            aod.restaurar("antes")
            nivel = d.get("nivel_atual")
            d["descartados"].append(nivel)
            d["resultados"][nivel] = {"motivo": "crash_ou_no_post", "quedas": quedas}
            d["falhas_consecutivas"] = d.get("falhas_consecutivas", 0) + 1
            d["testando"] = None
            if d["falhas_consecutivas"] >= cfg["max_falhas"]:
                d["pausa"] = {"motivo": f"{cfg['max_falhas']} falhas consecutivas. Auto-tune pausado.", "quando": time.time()}
                d["fase"] = "pausado"
                _gravar_diario(d)
                return f"Auto-tune: {cfg['max_falhas']} falhas seguidas. Pausado. Abra o painel para continuar."
            _avancar_proximo_nivel(d, cfg)
            return f"Auto-tune: PC instável com perfil {nivel}, revertido. Avançando para o próximo."

    # Após reboot bem-sucedido: validar se os timings entraram
    if d.get("fase") == "aguardando_boot":
        atual_umc = ram_timings.leitura_atual(True)
        conf = aod.conferir_depois_do_boot(atual_umc if atual_umc.get("ok") else None)
        resultado = conf.get("resultado") if isinstance(conf, dict) else None
        if resultado in ("parcial", "nao_entrou"):
            aod.restaurar("antes")
            nivel = d.get("nivel_atual")
            d["descartados"].append(nivel)
            d["resultados"][nivel] = {"motivo": f"timings_{resultado}"}
            d["testando"] = None
            _avancar_proximo_nivel(d, cfg)
            return f"Auto-tune: timings do nível {nivel} não entraram. Revertido e avançando."
        # Timings entraram: iniciar teste
        d["fase"] = "teste"
        _gravar_diario(d)
        _iniciar_teste(d, cfg)
        return None

    # Se estava testando e o teste terminou (usuário voltou ao painel)
    if d.get("fase") == "teste":
        _verificar_teste(d, cfg)
        return None

    return None


def _iniciar_teste(d: dict, cfg: dict) -> None:
    nivel = d.get("nivel_atual", "perfil")
    minutos = cfg["minutos"].get(nivel, 15)
    ram_timings.iniciar_teste(minutos, rotulo=f"auto_{nivel}", medir=True)
    d["testando"] = {"nivel": nivel, "quando": time.time(), "minutos": minutos}
    _gravar_diario(d)


def _verificar_teste(d: dict, cfg: dict) -> None:
    """Verifica se o teste terminou e toma decisão."""
    teste = ram_timings.estado_teste()
    if not teste or teste.get("rodando"):
        return  # ainda rodando

    resultado = teste.get("resultado")
    nivel = d.get("nivel_atual")
    if not resultado:
        return

    estavel = resultado.get("estavel", False)
    temp_max = resultado.get("temp_max")
    tipo_ddr = (ram_timings.kit().get("resumo") or {}).get("tipo", "DDR5")
    limite_temp = cfg["temp_max"].get(tipo_ddr, 55)

    if estavel and (temp_max is None or temp_max <= limite_temp):
        # Estável: atualiza melhor e avança
        d["melhor_atual"] = _assinatura_atual()
        d["resultados"][nivel] = {
            "estavel": True,
            "latencia": resultado.get("latencia"),
            "leitura": resultado.get("leitura"),
            "minutos": resultado.get("minutos"),
            "temp_max": temp_max,
        }
        d["falhas_consecutivas"] = 0
        d["testando"] = None
        _avancar_proximo_nivel(d, cfg)
    else:
        # Instável ou quente: reverte
        motivo = "temperatura_alta" if temp_max and temp_max > limite_temp else "instavel"
        aod.restaurar("antes")
        d["descartados"].append(nivel)
        d["resultados"][nivel] = {
            "estavel": False,
            "motivo": motivo,
            "erros": resultado.get("erros"),
            "whea": resultado.get("whea"),
            "temp_max": temp_max,
        }
        d["falhas_consecutivas"] = d.get("falhas_consecutivas", 0) + 1
        d["testando"] = None
        if d["falhas_consecutivas"] >= cfg["max_falhas"]:
            d["pausa"] = {"motivo": f"{cfg['max_falhas']} falhas consecutivas. Auto-tune pausado.", "quando": time.time()}
            d["fase"] = "pausado"
            _gravar_diario(d)
        else:
            # Reboot para confirmar rollback antes de tentar próximo
            d["fase"] = "aguardando_boot_rollback"
            d["reboots_realizados"] = d.get("reboots_realizados", 0) + 1
            _gravar_diario(d)
            aod.reiniciar_pc()


def _finalizar(d: dict) -> None:
    """Gera relatório e aplica o melhor perfil permanentemente."""
    relatorio = {
        "quando": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "melhor": d.get("melhor_atual"),
        "resultados": d.get("resultados", {}),
        "descartados": d.get("descartados", []),
        "reboots": d.get("reboots_realizados", 0),
        "duracao_min": round((time.time() - d.get("inicio", time.time())) / 60, 1),
    }
    try:
        _arquivo(RELATORIO).write_text(json.dumps(relatorio, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        log.exception("falha ao gravar relatório do auto-tune")

    d["fase"] = "final"
    d["testando"] = None
    _gravar_diario(d)


# ------------------------------------------------------------------ controle externo

def estado() -> dict:
    d = _ler_diario()
    if not d:
        return {"fase": "ocioso", "nivel_atual": None, "fila": [], "resultados": {}, "melhor_atual": None}
    teste = ram_timings.estado_teste()
    d["teste"] = teste
    return d


def pausar() -> dict:
    d = _ler_diario()
    if not d or d.get("fase") in (None, "ocioso", "final"):
        return {"ok": False, "mensagem": "Auto-tune não está rodando."}
    d["pausa"] = {"motivo": "Pausado pelo usuário.", "quando": time.time()}
    d["fase"] = "pausado"
    _gravar_diario(d)
    return {"ok": True, "mensagem": "Auto-tune pausado. Os valores atuais não foram revertidos."}


def continuar() -> dict:
    d = _ler_diario()
    if not d or d.get("fase") != "pausado":
        return {"ok": False, "mensagem": "Auto-tune não está pausado."}
    if ram_timings.em_uso():
        return {"ok": False, "mensagem": "Um teste de RAM já está rodando."}
    d["pausa"] = None
    cfg = _config_padrao()
    # Retoma da fase em que parou
    if d.get("nivel_atual") and d["nivel_atual"] not in d.get("resultados", {}):
        d["fase"] = "aplicar"
        _gravar_diario(d)
        _executar_aplicar(d, cfg)
    else:
        _avancar_proximo_nivel(d, cfg)
    return {"ok": True, "mensagem": "Auto-tune retomado."}


def cancelar() -> dict:
    d = _ler_diario()
    if not d or d.get("fase") in (None, "ocioso"):
        return {"ok": False, "mensagem": "Auto-tune não está rodando."}
    # Reverte ao estado original
    aod.restaurar("antes")
    _apagar_diario()
    return {"ok": True, "mensagem": "Auto-tune cancelado. Valores revertidos para o último estável."}


def pular() -> dict:
    d = _ler_diario()
    if not d or d.get("fase") in (None, "ocioso", "final"):
        return {"ok": False, "mensagem": "Auto-tune não está rodando."}
    nivel = d.get("nivel_atual")
    if nivel:
        d["descartados"].append(nivel)
        d["resultados"][nivel] = {"motivo": "pulado_pelo_usuario"}
        d["testando"] = None
        cfg = _config_padrao()
        _avancar_proximo_nivel(d, cfg)
        return {"ok": True, "mensagem": f"Nível {nivel} pulado."}
    return {"ok": False, "mensagem": "Nenhum nível ativo para pular."}


def relatorio() -> dict | None:
    try:
        return json.loads(_arquivo(RELATORIO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def encerrar() -> None:
    """Chamado ao fechar o programa. Não cancela o auto-tune (ele persiste entre sessões)."""
    pass