"""Relatório em texto do estado atual e dos problemas encontrados."""

from datetime import datetime
from pathlib import Path

from .verificacoes import CRITICO, Monitor, _gb, _mb


def gerar(monitor: Monitor, historico: Path | None = None) -> str:
    r = monitor.resumo_atual()
    linhas = [
        "MONITOR DE LENTIDÃO - DIAGNÓSTICO",
        f"Gerado em {datetime.now():%d/%m/%Y %H:%M}",
        f"Sistema: {r['sistema']} | {r['cpus']} núcleos lógicos | RAM {_gb(r['ram'].total)}",
        f"Ligado há {r['ligado_ha_h']:.0f} h",
        "",
    ]

    u = r["ultima"]
    if u:
        linhas += ["== AGORA ==",
                   f"CPU: {u.cpu:.0f}%   RAM: {u.ram:.0f}% ({_gb(r['ram'].used)} de {_gb(r['ram'].total)})"]
        if u.desempenho_cpu is not None:
            linhas.append(f"Velocidade do processador: {u.desempenho_cpu:.0f}% da nominal")
        for disco, (ocupado, lat) in sorted(u.discos.items()):
            linhas.append(f"Disco {disco}: {ocupado:.0f}% ocupado, {lat * 1000:.0f} ms por operação")
        linhas.append("")

    problemas = monitor.problemas()
    linhas.append(f"== PROBLEMAS ENCONTRADOS ({len(problemas)}) ==")
    if not problemas:
        linhas += ["Nenhum problema detectado no momento.", ""]
    for p in problemas:
        linhas.append(f"[{'CRÍTICO' if p.severidade == CRITICO else 'AVISO'}] {p.titulo}")
        linhas.append(f"    {p.detalhe}")
        if p.sugestao:
            linhas.append(f"    O que fazer: {p.sugestao}")
        linhas.append("")

    programas = list(r["programas"].values())
    if programas:
        def tabela(titulo, chave, formatar):
            linhas.append(f"== {titulo} ==")
            for p in sorted(programas, key=chave, reverse=True)[:8]:
                qtd = f" ({p.processos} proc.)" if p.processos > 1 else ""
                linhas.append(f"  {formatar(p):>12}  {p.nome}{qtd}")
            linhas.append("")
        tabela("PROGRAMAS QUE MAIS USAM CPU (último minuto)", lambda p: p.cpu, lambda p: f"{p.cpu:.1f}%")
        tabela("PROGRAMAS QUE MAIS USAM MEMÓRIA", lambda p: p.ram, lambda p: _mb(p.ram))
        tabela("PROGRAMAS QUE MAIS USAM O DISCO", lambda p: p.disco, lambda p: f"{_mb(p.disco)}/s")

    if r["particoes"]:
        linhas.append("== ESPAÇO EM DISCO ==")
        for ponto, uso in r["particoes"]:
            linhas.append(f"  {ponto}: {_gb(uso.free)} livres de {_gb(uso.total)} ({100 - uso.percent:.0f}%)")
        linhas.append("")

    if r["inicializacao"]:
        ativos = [i for i in r["inicializacao"] if i["ativo"]]
        linhas.append(f"== PROGRAMAS QUE ABREM COM O WINDOWS ({len(ativos)} ativos) ==")
        for i in r["inicializacao"]:
            linhas.append(f"  [{'ativo' if i['ativo'] else 'desativado'}] {i['nome']} - {i['comando'][:100]}")
        linhas.append("")

    if historico and historico.exists():
        recentes = historico.read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
        if recentes:
            linhas.append("== ÚLTIMOS ALERTAS ==")
            linhas += [f"  {x}" for x in recentes]

    return "\n".join(linhas) + "\n"
