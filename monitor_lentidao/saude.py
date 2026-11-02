"""Lista rápida de verificação da saúde do PC (Visão geral).

Itens adaptados do ResetExplode (MainWindow.Dash.cs e OcCheck.cs; licença MIT, © 2026 Gustavo).
"""

import time

from . import otimizacao, windows

_SCRIPT_ERROS = r"""
$x="*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger'] and TimeCreated[timediff(@SystemTime) <= 604800000]]]"
$w=@(Get-WinEvent -LogName System -FilterXPath $x -ErrorAction SilentlyContinue).Count
$x="*[System[((Provider[@Name='nvlddmkm'] and (EventID=13 or EventID=153)) or (Provider[@Name='Display'] and EventID=4101) or Provider[@Name='amdkmdag']) and TimeCreated[timediff(@SystemTime) <= 604800000]]]"
$g=@(Get-WinEvent -LogName System -FilterXPath $x -ErrorAction SilentlyContinue).Count
[pscustomobject]@{Whea=$w;Gpu=$g} | ConvertTo-Json -Compress
"""

_cache: tuple[float, list] | None = None


def erros_hardware() -> dict | None:
    """Erros WHEA (processador, memória, PCIe) e reinícios do driver de vídeo nos últimos 7 dias."""
    r = windows.powershell_json(_SCRIPT_ERROS, timeout=60)
    return {"whea": int(r[0].get("Whea") or 0), "gpu": int(r[0].get("Gpu") or 0)} if r else None


def checklist(diagnostico_sensores: dict | None) -> list[dict]:
    global _cache
    if _cache and time.time() - _cache[0] < 60:
        itens = list(_cache[1])
    else:
        itens = []
        if windows.EH_WINDOWS:
            e = erros_hardware()
            if e is None:
                itens.append({"titulo": "Sem erros de hardware", "ok": None,
                              "detalhe": "Não foi possível ler o registro de eventos do Windows."})
            else:
                itens.append({"titulo": "Sem erros de hardware", "ok": e["whea"] + e["gpu"] == 0,
                              "detalhe": "Nenhum erro WHEA ou reinício do driver de vídeo em 7 dias."
                              if e["whea"] + e["gpu"] == 0 else
                              f"{e['whea']} erros WHEA e {e['gpu']} reinícios do driver de vídeo em 7 dias. "
                              "Pode ser temperatura, overclock instável, RAM ou fonte."})
            for id_, titulo, bom, ruim in (
                    ("modo_jogo", "Modo de Jogo ligado", "O Windows prioriza o jogo aberto.",
                     "Desligado: ligue em Otimização."),
                    ("game_dvr", "Gravação em segundo plano desligada", "O Xbox Game Bar não fica gravando.",
                     "O Game DVR está gravando o tempo todo e custa desempenho. Desligue em Otimização.")):
                try:
                    ok = otimizacao.ler_ajuste(id_)
                except Exception:
                    continue
                itens.append({"titulo": titulo, "ok": ok, "detalhe": bom if ok else ruim})
        _cache = (time.time(), itens)
        itens = list(itens)
    if windows.EH_WINDOWS:
        sem = diagnostico_sensores and diagnostico_sensores.get("codigo") not in (None, "carregando")
        itens.insert(0, {"titulo": "Sensores do processador", "ok": not sem,
                         "detalhe": "Temperatura e consumo disponíveis." if not sem else
                         diagnostico_sensores["texto"].split(". ")[0] + "."})
    return itens
