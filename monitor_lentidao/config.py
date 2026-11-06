"""Configuração: limites de alerta e pasta de dados.

Na primeira execução é criado um config.json na pasta de dados; edite-o para
ajustar os limites e reinicie o programa.
"""

import json
import logging
import os
import sys
from pathlib import Path

PADRAO = {
    # Intervalos
    "intervalo_amostra_s": 5,
    "janela_media_s": 120,  # média usada para decidir se algo está "alto de forma sustentada"
    "intervalo_verificacao_pesada_s": 600,  # eventos, disco, inicialização...
    "silenciar_repeticao_min": 30,  # não repete o mesmo alerta antes disso
    # Sistema
    "cpu_total_alerta": 85,
    "ram_alerta": 90,
    "disco_ocupado_alerta": 90,
    "disco_latencia_alerta_ms": 50,
    "cpu_desempenho_minimo": 60,  # % da frequência nominal com a CPU ocupada
    # Programas (somando todos os processos de mesmo nome, ex.: chrome.exe)
    "programa_cpu_alerta": 40,  # % da CPU total
    "programa_ram_alerta_mb": 3072,
    "programa_disco_alerta_mb_s": 30,
    "processo_handles_alerta": 10000,
    "vazamento_memoria_mb": 500,
    "janela_vazamento_min": 30,
    # Verificações periódicas
    "espaco_livre_min_pct": 10,
    "espaco_livre_min_gb": 15,
    "programas_inicializacao_alerta": 12,
    "uptime_alerta_dias": 7,
    "inicializacao_lenta_s": 120,
    # Processos que nunca devem gerar alerta (nome do executável, minúsculo)
    "ignorar_processos": ["system idle process", "idle", "memory compression", "registry"],
    # Auto-tune de RAM (loop automático via AOD)
    "ram_auto_niveis": ["perfil", "afinado", "agressivo"],
    "ram_auto_minutos": {"perfil": 5, "afinado": 15, "agressivo": 30},
    "ram_auto_temp_max": {"DDR5": 55, "DDR4": 60},
    "ram_auto_max_falhas": 3,
}


def pasta_dados() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        pasta = base / "MonitorLentidao"
    else:
        pasta = Path.home() / ".monitor_lentidao"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def carregar() -> dict:
    arquivo = pasta_dados() / "config.json"
    cfg = dict(PADRAO)
    if arquivo.exists():
        try:
            cfg.update(json.loads(arquivo.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            logging.exception("config.json inválido; usando valores padrão")
    else:
        arquivo.write_text(json.dumps(PADRAO, indent=2, ensure_ascii=False), encoding="utf-8")
    cfg["ignorar_processos"] = [n.lower() for n in cfg["ignorar_processos"]]
    return cfg


# Preferências alteradas pelo painel/menu (diferente do config.json, que guarda limites).
PREFERENCIAS_PADRAO = {"notificacoes": "todas",  # todas | graves | nenhuma
                       "tema": "escuro",  # escuro | claro | sistema
                       "avisos_usb": True,
                       "atualizar_sozinho": True}  # confere o GitHub ao abrir e instala a versão nova
_preferencias: dict | None = None


def preferencias() -> dict:
    global _preferencias
    if _preferencias is None:
        _preferencias = dict(PREFERENCIAS_PADRAO)
        arquivo = pasta_dados() / "preferencias.json"
        if arquivo.exists():
            try:
                _preferencias.update(json.loads(arquivo.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                logging.exception("preferencias.json inválido; usando padrão")
    return _preferencias


def definir_preferencia(chave: str, valor) -> None:
    preferencias()[chave] = valor
    (pasta_dados() / "preferencias.json").write_text(
        json.dumps(_preferencias, indent=2, ensure_ascii=False), encoding="utf-8")
