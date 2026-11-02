"""Interpreta o Visualizador de Eventos: erros que costumam vir junto com lentidão."""

from collections import defaultdict

from .verificacoes import AVISO, CRITICO, Problema


def _armazenamento(prov: str) -> bool:
    return prov.startswith(("stor", "iastor", "nvme", "amd_sata", "amdsata"))


# (teste do provedor, ids, chave, severidade, título, sugestão)
REGRAS = [
    (lambda p: p == "disk", {7}, "ev_setores", CRITICO, "Setores defeituosos no disco",
     "Faça backup agora e verifique o disco com 'chkdsk /r' e o CrystalDiskInfo."),
    (lambda p: p == "disk", {52}, "ev_smart", CRITICO, "O disco avisou que vai falhar (SMART)",
     "Faça backup imediatamente e troque o disco."),
    (lambda p: p == "disk", {51, 153}, "ev_disco_repeticoes", AVISO, "Disco precisou repetir leituras/gravações",
     "Cada repetição trava o sistema por instantes. Verifique cabo/porta SATA e a saúde do disco."),
    (lambda p: _armazenamento(p), {129}, "ev_disco_reset", AVISO, "Controlador de disco reiniciando (travamentos)",
     "Atualize o driver do chipset/armazenamento e o firmware do SSD; verifique a saúde do disco."),
    (lambda p: p in ("ntfs", "microsoft-windows-ntfs"), {55, 98, 137}, "ev_ntfs", CRITICO,
     "Sistema de arquivos corrompido",
     "Abra o Prompt como administrador e rode 'chkdsk C: /f' (reinicia o PC)."),
    (lambda p: p == "microsoft-windows-whea-logger", {1, 17, 18, 19, 47}, "ev_whea", CRITICO,
     "Erros de hardware (WHEA)",
     "Pode ser superaquecimento, overclock instável, memória ou placa-mãe. Desfaça overclocks e teste a RAM "
     "(Diagnóstico de Memória do Windows)."),
    (lambda p: p == "microsoft-windows-resource-exhaustion-detector", {2004}, "ev_memoria_virtual", CRITICO,
     "Memória virtual esgotada",
     "Algum programa consumiu toda a memória. Verifique os alertas de memória e aumente o arquivo de paginação."),
    (lambda p: p == "microsoft-windows-kernel-processor-power", {37}, "ev_cpu_limitada", AVISO,
     "Processador limitado pelo firmware",
     "Geralmente é temperatura ou energia: limpe a poeira, use o carregador original e o plano 'Equilibrado'."),
    (lambda p: p == "display", {4101}, "ev_video", AVISO, "Driver de vídeo travou e foi recuperado",
     "Atualize o driver de vídeo pelo site da NVIDIA/AMD/Intel."),
    (lambda p: p == "service control manager", {7000, 7009, 7011, 7022, 7023, 7031, 7034}, "ev_servicos", AVISO,
     "Serviços do Windows falhando ou demorando",
     "Serviços que demoram a responder atrasam o sistema. Atualize ou desinstale o programa dono do serviço."),
    (lambda p: p == "application hang", {1002}, "ev_travamentos", AVISO, "Programas travando ('Não respondendo')",
     "Atualize os programas listados; se forem vários, a causa pode ser disco lento ou memória cheia."),
    (lambda p: p == "application error", {1000}, "ev_falhas", AVISO, "Programas fechando sozinhos com erro",
     "Atualize ou reinstale os programas listados."),
]


def analisar(eventos: list[dict]) -> list[Problema]:
    grupos: dict[int, list[dict]] = defaultdict(list)
    outros_erros = 0
    for ev in eventos:
        prov = str(ev.get("ProviderName") or "").lower()
        eid = ev.get("Id")
        for i, (teste, ids, *_resto) in enumerate(REGRAS):
            if eid in ids and teste(prov):
                grupos[i].append(ev)
                break
        else:
            if ev.get("Level") in (1, 2):
                outros_erros += 1

    problemas = []
    for i, lista in grupos.items():
        _, _, chave, sev, titulo, sugestao = REGRAS[i]
        lista.sort(key=lambda e: e.get("Hora", ""), reverse=True)
        mensagens = list(dict.fromkeys(e.get("Msg", "") for e in lista if e.get("Msg")))[:3]
        detalhe = f"{len(lista)} ocorrência(s) nas últimas 24 h, a última em {lista[0].get('Hora', '?').replace('T', ' ')}."
        if mensagens:
            detalhe += " Ex.: " + " | ".join(m[:160] for m in mensagens)
        problemas.append(Problema(chave, sev, titulo, detalhe, sugestao,
                                  assinatura=lista[0].get("Hora", ""), renotificar_min=0))

    if outros_erros >= 50:
        problemas.append(Problema("ev_muitos_erros", AVISO, f"{outros_erros} erros no log do sistema em 24 h",
                                  "Muitos erros repetidos costumam indicar driver ou serviço com problema.",
                                  "Abra o Visualizador de Eventos > Logs do Windows > Sistema e veja os mais frequentes.",
                                  assinatura=str(outros_erros // 50), renotificar_min=0))
    return problemas
