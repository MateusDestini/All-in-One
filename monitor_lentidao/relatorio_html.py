"""Relatório completo em HTML (um arquivo só, abre em qualquer navegador) ou JSON.

Números de série ficam de fora, porque o relatório costuma ser enviado a terceiros.
Estrutura adaptada do ResetExplode (ReportBuilder.cs; licença MIT, © 2026 Gustavo).
"""

import json
import socket
from datetime import datetime
from html import escape

from . import __version__
from .sistema import sem_serie

CSS = """
:root{--bg:#fff;--fg:#111;--muted:#666;--line:#e3e3e3;--card:#fafafa;--ok:#15803d;--aviso:#b45309;--critico:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#f2f2f2;--muted:#9a9aa2;--line:#2c2c30;--card:#1c1c1f;--ok:#4ade80;--aviso:#fbbf24;--critico:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 'Segoe UI',system-ui,sans-serif}
.wrap{max-width:980px;margin:0 auto;padding:32px 20px 60px}
h1{margin:0;font-size:26px}h2{margin:34px 0 12px;font-size:17px}h3{margin:0 0 8px;font-size:14px}
.sub,.muted{color:var(--muted)}.sub{margin:2px 0 0}.nota{color:var(--muted);font-size:12px;margin:6px 0 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:0 0 12px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:center}
.selo{font-size:11px;font-weight:600;letter-spacing:.5px;padding:3px 10px;border-radius:20px;border:1px solid currentColor;white-space:nowrap}
.ok{color:var(--ok)}.aviso{color:var(--aviso)}.critico{color:var(--critico)}.sem_dados{color:var(--muted)}
table{width:100%;border-collapse:collapse}td,th{padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{color:var(--muted);font-weight:600;font-size:12px}.k{color:var(--muted);width:38%}.num{font-family:Consolas,monospace;text-align:right;white-space:nowrap}
.grp{font-weight:600;padding-top:12px}.inner{padding:0 0 0 14px;border-bottom:0}.nested td{font-size:13px}
footer{margin-top:40px;color:var(--muted);font-size:12px}
@media print{body{background:#fff;color:#000}.card{break-inside:avoid}}
"""


def _e(s) -> str:
    return escape("" if s is None else str(s))


def _arvore(nos: list[dict] | None, profundidade: int = 0) -> str:
    if not nos:
        return ""
    partes = ['<table class="nested">' if profundidade else "<table>"]
    for n in nos:
        if n.get("filhos"):
            valor = f" · {_e(n['valor'])}" if n.get("valor") else ""
            partes.append(f'<tr><td colspan="2" class="grp">{_e(n["nome"])}{valor}</td></tr>'
                          f'<tr><td colspan="2" class="inner">{_arvore(n["filhos"], profundidade + 1)}</td></tr>')
        else:
            partes.append(f'<tr><td class="k">{_e(n["nome"])}</td><td>{_e(n.get("valor"))}</td></tr>')
    partes.append("</table>")
    return "".join(partes)


def _cartoes(titulo: str, itens: list[dict]) -> str:
    """itens: {titulo, sub, nivel, rotulo, texto, metricas: [[k, v]]}"""
    if not itens:
        return ""
    partes = [f"<section><h2>{_e(titulo)}</h2>"]
    for i in itens:
        partes.append(f'<div class="card"><div class="row"><b>{_e(i["titulo"])}</b>'
                      f'<span class="selo {_e(i.get("nivel", "sem_dados"))}">{_e(i.get("rotulo", ""))}</span></div>')
        if i.get("sub"):
            partes.append(f'<p class="muted">{_e(i["sub"])}</p>')
        if i.get("texto"):
            partes.append(f"<p>{_e(i['texto'])}</p>")
        if i.get("metricas"):
            partes.append("<table>" + "".join(f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>'
                                              for k, v in i["metricas"]) + "</table>")
        partes.append("</div>")
    partes.append("</section>")
    return "".join(partes)


def html(d: dict) -> str:
    agora = datetime.now()
    p = [f'<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" '
         f'content="width=device-width,initial-scale=1"><title>Relatório · {_e(socket.gethostname())}</title>'
         f'<style>{CSS}</style></head><body><div class="wrap">',
         f'<h1>Monitor de Lentidão</h1><p class="sub">Relatório do PC · {_e(socket.gethostname())} · '
         f'{agora:%d/%m/%Y %H:%M} · v{_e(__version__)}</p>',
         '<p class="nota">Números de série foram omitidos deste relatório.</p>']

    if d.get("problemas") is not None:
        itens = [{"titulo": x["titulo"], "nivel": x["nivel"], "rotulo": "Grave" if x["nivel"] == "critico" else "Aviso",
                  "texto": x["detalhe"] + (f" O que fazer: {x['sugestao']}" if x.get("sugestao") else "")}
                 for x in d["problemas"]]
        p.append(_cartoes("O que pode estar deixando o PC lento", itens) if itens else
                 '<section><h2>O que pode estar deixando o PC lento</h2><div class="card">Nenhum problema encontrado.</div></section>')

    p.append(_cartoes("Saúde dos discos", [
        {"titulo": x["modelo"], "sub": x.get("meta"), "nivel": x["nivel"], "rotulo": x.get("rotulo", x["nivel"]),
         "texto": x.get("motivos"), "metricas": [m for m in x.get("metricas", []) if "série" not in m[0].lower()]}
        for x in d.get("discos") or []]))
    energia = d.get("energia")
    if energia:
        itens = [{"titulo": "Energia", "nivel": "aviso" if energia.get("plano_economia") else "ok",
                  "rotulo": "Economia" if energia.get("plano_economia") else "Em uso", "texto": energia.get("conselho"),
                  "metricas": [["Fonte de energia", energia.get("fonte")], ["Plano de energia", energia.get("plano")]]}]
        itens += [{"titulo": b["titulo"], "nivel": b["nivel"], "rotulo": b["rotulo"], "texto": b["motivos"],
                   "metricas": b["metricas"]} for b in energia.get("baterias", [])]
        p.append(_cartoes("Bateria e energia", itens))
    rede = d.get("rede")
    if rede:
        c = rede.get("conexao") or {}
        p.append(_cartoes("Teste de rede", [{
            "titulo": "Conexão", "sub": f"{'Wi-Fi' if c.get('wifi') else 'Cabo'} · {rede.get('quando', '')}",
            "nivel": rede["nivel"], "rotulo": rede.get("rotulo"), "texto": " ".join(rede.get("motivos", [])),
            "metricas": [["Download", f"{rede['download']:.0f} Mbps" if rede.get("download") else "—"],
                         ["Upload", f"{rede['upload']:.0f} Mbps" if rede.get("upload") else "—"],
                         ["DNS", f"{rede['dns_ms']} ms" if rede.get("dns_ms") is not None else "—"]]}]))

    sistema = d.get("sistema") or {}
    if sistema.get("secoes"):
        p.append("<section><h2>Sistema</h2>")
        for s in sistema["secoes"]:
            s = sem_serie(s)
            p.append(f'<div class="card"><h3>{_e(s["nome"])}</h3>{_arvore(s["filhos"])}</div>')
        p.append("</section>")
    for g in d.get("gpus") or []:
        p.append(f"<section><h2>Placa de vídeo · {_e(g['nome'])}</h2>")
        if g.get("subtitulo"):
            p.append(f'<p class="muted">{_e(g["subtitulo"])}</p>')
        for s in g["secoes"]:
            p.append(f'<div class="card"><h3>{_e(s["titulo"])}</h3><table>' + "".join(
                f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>' for k, v in s["itens"]) + "</table></div>")
        p.append("</section>")

    sensores = d.get("sensores") or []
    if sensores:
        p.append("<section><h2>Sensores (leitura no momento da exportação)</h2>")
        for hw in dict.fromkeys(s["hardware"] for s in sensores):
            p.append(f'<div class="card"><h3>{_e(hw)}</h3><table><tr><th>Categoria</th><th>Sensor</th>'
                     f'<th class="num">Valor</th><th class="num">Mín.</th><th class="num">Máx.</th></tr>')
            for s in sensores:
                if s["hardware"] == hw:
                    p.append(f'<tr><td class="muted">{_e(s["categoria"])}</td><td>{_e(s["sensor"])}</td>'
                             f'<td class="num">{_e(s["valor"])}</td><td class="num muted">{_e(s["min"])}</td>'
                             f'<td class="num muted">{_e(s["max"])}</td></tr>')
            p.append("</table></div>")
        p.append("</section>")

    testes = d.get("testes") or []
    if testes:
        p.append('<section><h2>Testes recentes</h2><div class="card"><table><tr><th>Data</th><th>Teste</th><th>Resultado</th></tr>')
        p += [f'<tr><td class="muted">{_e(t["quando"])}</td><td>{_e(t["teste"])}</td><td>{_e(t["resultado"])}</td></tr>'
              for t in testes]
        p.append("</table></div></section>")

    p.append("<footer>Gerado pelo Monitor de Lentidão. Dados lidos do Windows, do driver da placa de vídeo e dos "
             "sensores da placa-mãe; podem variar conforme o hardware.</footer></div></body></html>")
    return "".join(p)


def json_(d: dict) -> str:
    saida = dict(d, gerado_em=datetime.now().isoformat(timespec="seconds"), computador=socket.gethostname(),
                 versao=__version__)
    if saida.get("sistema"):
        saida["sistema"] = dict(saida["sistema"], secoes=[sem_serie(s) for s in saida["sistema"]["secoes"]])
    return json.dumps(saida, indent=2, ensure_ascii=False, default=str)


def testes_recentes(memoria_hist: list[dict], disco_hist: list[dict], bench_hist: list[dict] = ()) -> list[dict]:
    """Resumo de uma linha dos testes de memória, disco e benchmarks, do mais novo ao mais antigo."""
    linhas = []
    for h in bench_hist:
        if h.get("tipo") == "render":
            partes = [f"todos os núcleos {h['multi']:,} pts".replace(",", ".") if h.get("multi") else "",
                      f"1 núcleo {h['single']:,} pts".replace(",", ".") if h.get("single") else ""]
            linhas.append({"quando": h.get("quando", ""), "teste": "Benchmark do processador" +
                           (" (10 min)" if h.get("sustentado") else ""), "resultado": " · ".join(p for p in partes if p)})
        elif h.get("tipo") == "sim":
            linhas.append({"quando": h.get("quando", ""), "teste": "Simulação de jogo",
                           "resultado": f"{h.get('fps', 0)} FPS · 1% low {h.get('low1', 0)}"})
    for h in memoria_hist:
        leitura = h.get("leitura_mb") or (h.get("leitura") or 0) * 1000
        linhas.append({"quando": h.get("quando", ""), "teste": "Memória",
                       "resultado": f"leitura {leitura:,.0f} MB/s".replace(",", ".") +
                                    f" · latência {h.get('latencia', 0):.1f} ns".replace(".", ",")})
    for h in disco_hist:
        linhas.append({"quando": h.get("quando", ""), "teste": f"Disco {h.get('unidade', '')}",
                       "resultado": f"SEQ1M Q8 {h.get('seq_q8_leitura', 0):,.0f} / {h.get('seq_q8_escrita', 0):,.0f} MB/s · "
                                    f"RND4K Q1 {h.get('rnd_q1_leitura', 0):.1f} / {h.get('rnd_q1_escrita', 0):.1f} MB/s"
                       .replace(",", "X").replace(".", ",").replace("X", ".")})
    return linhas
