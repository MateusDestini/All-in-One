"""Energia: bateria de notebook (desgaste, ciclos, carga) e plano de energia. Só leitura.

Critérios adaptados do ResetExplode (BatteryInfo.cs, licença MIT, © 2026 Gustavo).
"""

import psutil

from . import windows

_SCRIPT_BATERIA = r"""
$w=@(Get-CimInstance Win32_Battery -ErrorAction SilentlyContinue)
$s=@(Get-CimInstance -Namespace root/wmi BatteryStaticData -ErrorAction SilentlyContinue)
$f=@(Get-CimInstance -Namespace root/wmi BatteryFullChargedCapacity -ErrorAction SilentlyContinue)
$c=@(Get-CimInstance -Namespace root/wmi BatteryCycleCount -ErrorAction SilentlyContinue)
$v=@(Get-CimInstance -Namespace root/wmi BatteryStatus -ErrorAction SilentlyContinue)
$n=[Math]::Max($w.Count,$s.Count)
for($i=0;$i -lt $n;$i++){ [pscustomobject]@{
  Carga=$w[$i].EstimatedChargeRemaining; Estado=$w[$i].BatteryStatus; Autonomia=$w[$i].EstimatedRunTime; Nome=$w[$i].Name
  Projeto=$s[$i].DesignedCapacity; Fabricante=$s[$i].ManufactureName; Modelo=$s[$i].DeviceName
  Maxima=$f[$i].FullChargedCapacity; Ciclos=$c[$i].CycleCount
  Tomada=$v[$i].PowerOnline; Carregando=$v[$i].Charging; Descarregando=$v[$i].Discharging
  Tensao=$v[$i].Voltage; TaxaCarga=$v[$i].ChargeRate; TaxaDescarga=$v[$i].DischargeRate } } | ConvertTo-Json -Compress
"""

ALTO_DESEMPENHO = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
DESEMPENHO_MAXIMO = "e9a42b02-d5df-448d-aa00-03f14749eb61"


def avaliar_bateria(b: dict, indice: int, total: int) -> dict:
    projeto, maxima = b.get("Projeto") or 0, b.get("Maxima") or 0
    ciclos = b.get("Ciclos") or None  # muitos fabricantes informam 0
    desgaste = max(0.0, 100 * (1 - maxima / projeto)) if projeto > 0 and maxima > 0 else None
    motivos, nivel, rotulo = [], "ok", "Saudável"
    if desgaste is None:
        nivel, rotulo = "sem_dados", "Sem dados"
        motivos.append("O Windows não informou a capacidade original ou a atual, então o desgaste não pôde ser calculado.")
    elif desgaste >= 50:
        nivel, rotulo = "critico", "Desgastada"
        motivos.append(f"Perdeu {desgaste:.0f}% da capacidade original: a autonomia é metade do que era. Vale trocar a bateria.")
    elif desgaste >= 30:
        nivel, rotulo = "aviso", "Atenção"
        motivos.append(f"Perdeu {desgaste:.0f}% da capacidade original. Ainda funciona, mas a autonomia já caiu bastante.")
    elif desgaste >= 15:
        motivos.append(f"Desgaste de {desgaste:.0f}%, normal para uma bateria com uso.")
    else:
        motivos.append(f"Desgaste de apenas {desgaste:.0f}%: bateria em ótimo estado.")
    if ciclos and ciclos >= 1000 and nivel == "ok":
        nivel, rotulo = "aviso", "Atenção"
    if ciclos and ciclos >= 800:
        motivos.append(f"{ciclos} ciclos de carga: perto do fim da vida útil típica (500 a 1000 ciclos).")

    metricas = []

    def m(rot, val):
        if val not in (None, ""):
            metricas.append([rot, val])
    m("Carga atual", f"{b['Carga']}%" if b.get("Carga") is not None else None)
    estado = {1: "Descarregando", 2: "Na tomada", 3: "Totalmente carregada", 4: "Carga baixa", 5: "Carga crítica",
              6: "Carregando", 7: "Carregando", 8: "Carregando", 9: "Carregando", 11: "Parcialmente carregada"}
    m("Estado", estado.get(b.get("Estado")))
    aut = b.get("Autonomia")
    if aut and 0 < aut < 71582788 and not b.get("Tomada"):
        m("Autonomia estimada", f"{aut // 60}h {aut % 60:02d}min")
    m("Capacidade original", f"{projeto / 1000:.1f} Wh".replace(".", ",") if projeto else None)
    m("Capacidade máxima hoje", f"{maxima / 1000:.1f} Wh".replace(".", ",") if maxima else None)
    m("Desgaste", f"{desgaste:.1f}%".replace(".", ",") if desgaste is not None else None)
    m("Ciclos de carga", str(ciclos) if ciclos else None)
    if b.get("Carregando") and (b.get("TaxaCarga") or 0) > 0:
        m("Potência de carga", f"{b['TaxaCarga'] / 1000:.1f} W".replace(".", ","))
    if b.get("Descarregando") and (b.get("TaxaDescarga") or 0) > 0:
        m("Consumo agora", f"{b['TaxaDescarga'] / 1000:.1f} W".replace(".", ","))
    m("Fabricante", (b.get("Fabricante") or "").strip() or None)
    m("Modelo", (b.get("Modelo") or b.get("Nome") or "").strip() or None)
    return {"titulo": f"Bateria {indice + 1}" if total > 1 else "Bateria", "nivel": nivel, "rotulo": rotulo,
            "motivos": " ".join(motivos), "metricas": metricas, "desgaste": desgaste}


def ler() -> dict:
    brutos = windows.powershell_json(_SCRIPT_BATERIA, timeout=30)
    baterias = [avaliar_bateria(b, i, len(brutos)) for i, b in enumerate(brutos)]
    tomada = None
    try:
        s = psutil.sensors_battery()
        tomada = s.power_plugged if s else None
    except (AttributeError, NotImplementedError, OSError):
        pass
    plano = windows.plano_energia()
    guid, nome = plano if plano else (None, None)
    rapido = guid in (ALTO_DESEMPENHO, DESEMPENHO_MAXIMO) or any(
        p in (nome or "").lower() for p in ("alto", "high", "máximo", "ultimate"))
    economia = guid == windows.ECONOMIA_ENERGIA
    if economia:
        conselho = "O plano Economia de energia limita o processador e deixa tudo mais lento. Troque em Otimização."
    elif not rapido and not baterias:
        conselho = "Em PC de mesa, o plano Alto desempenho (em Otimização) evita que o processador demore a acelerar."
    else:
        conselho = ""
    return {"baterias": baterias, "desktop": not baterias,
            "fonte": "Tomada (PC de mesa)" if not baterias else "Tomada" if tomada else "Bateria" if tomada is False else "—",
            "plano": nome or "Não identificado", "plano_economia": economia, "conselho": conselho}
