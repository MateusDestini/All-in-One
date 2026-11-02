"""Conhecimento sobre programas comuns que roubam desempenho."""

import os
from pathlib import PureWindowsPath as Path

# Antivírus de terceiros (o Windows Defender se desliga sozinho quando há outro).
ANTIVIRUS_TERCEIROS = {
    "avp.exe": "Kaspersky", "avpui.exe": "Kaspersky",
    "mcshield.exe": "McAfee", "mfemms.exe": "McAfee", "mcafee.exe": "McAfee",
    "nortonsecurity.exe": "Norton", "ns.exe": "Norton", "norton.exe": "Norton",
    "avastsvc.exe": "Avast", "avastui.exe": "Avast",
    "avgsvc.exe": "AVG", "avgui.exe": "AVG",
    "vsserv.exe": "Bitdefender", "bdagent.exe": "Bitdefender", "bdservicehost.exe": "Bitdefender",
    "ekrn.exe": "ESET", "egui.exe": "ESET",
    "psanhost.exe": "Panda", "psuaservice.exe": "Panda",
    "avguard.exe": "Avira", "avira.servicehost.exe": "Avira",
    "sbamsvc.exe": "Malwarebytes (proteção em tempo real)", "mbamservice.exe": "Malwarebytes (proteção em tempo real)",
    "wrsa.exe": "Webroot", "coreserviceshell.exe": "Trend Micro", "sophoshealth.exe": "Sophos",
}

# Dicas para programas que frequentemente aparecem consumindo recursos.
DICAS_PROGRAMAS = {
    "msmpeng.exe": "É o antivírus do Windows (Defender) fazendo verificação. Se for constante, agende a verificação "
                   "para um horário em que não usa o PC ou adicione pastas de trabalho pesado às exclusões.",
    "mpdefendercoreservice.exe": "Serviço do Windows Defender. Normalmente passa após a verificação terminar.",
    "searchindexer.exe": "Indexação da Pesquisa do Windows. Em Configurações > Privacidade > Pesquisando no Windows, "
                         "use o modo 'Clássico' ou exclua pastas grandes.",
    "searchprotocolhost.exe": "Parte da indexação da Pesquisa do Windows (veja SearchIndexer).",
    "tiworker.exe": "Windows Update instalando atualizações. Deixe terminar e reinicie o PC.",
    "trustedinstaller.exe": "Windows Update instalando atualizações. Deixe terminar e reinicie o PC.",
    "wuauclt.exe": "Windows Update. Deixe terminar e reinicie o PC.",
    "musnotification.exe": "Windows Update. Deixe terminar e reinicie o PC.",
    "compattelrunner.exe": "Telemetria de compatibilidade da Microsoft; roda de vez em quando e passa sozinha.",
    "onedrive.exe": "OneDrive sincronizando. Pause a sincronização pelo ícone da nuvem se precisar de desempenho.",
    "dropbox.exe": "Dropbox sincronizando. Pause a sincronização se precisar de desempenho.",
    "googledrivefs.exe": "Google Drive sincronizando. Pause a sincronização se precisar de desempenho.",
    "chrome.exe": "Cada aba e extensão é um processo. Feche abas, remova extensões e veja o Gerenciador de "
                  "Tarefas do Chrome (Shift+Esc).",
    "msedge.exe": "Cada aba e extensão é um processo. Feche abas, ative 'Guias em suspensão' e veja o "
                  "Gerenciador de Tarefas do Edge (Shift+Esc).",
    "firefox.exe": "Feche abas e veja about:processes para descobrir qual aba pesa mais.",
    "opera.exe": "Feche abas e extensões que não usa.",
    "brave.exe": "Feche abas e veja o Gerenciador de Tarefas do navegador (Shift+Esc).",
    "teams.exe": "O Teams é pesado; feche-o quando não estiver em reunião e desative 'abrir com o Windows'.",
    "ms-teams.exe": "O Teams é pesado; feche-o quando não estiver em reunião e desative 'abrir com o Windows'.",
    "discord.exe": "Desative a aceleração de hardware e a sobreposição de jogos nas configurações do Discord.",
    "steamwebhelper.exe": "Interface do Steam; minimize o Steam ou ative o 'modo pequeno'.",
    "dwm.exe": "Gerenciador de janelas do Windows. Uso alto costuma ser driver de vídeo desatualizado.",
    "explorer.exe": "Explorador de Arquivos. Uso alto pode ser extensão de terceiros no menu de contexto ou "
                    "pasta de rede travada. Reinicie-o pelo Gerenciador de Tarefas.",
    "svchost.exe": "Hospeda serviços do Windows. No Gerenciador de Tarefas > Detalhes, clique com o botão direito "
                   "> 'Ir para serviço(s)' para ver qual serviço está pesado.",
    "wmiprvse.exe": "WMI: algum programa está consultando o sistema sem parar. Reinicie o serviço "
                    "'Instrumentação de Gerenciamento do Windows' ou procure o programa responsável.",
    "antimalware service executable": "É o antivírus do Windows (Defender) fazendo verificação.",
    "audiodg.exe": "Áudio do Windows. Desative 'melhorias de áudio' nas propriedades do dispositivo de som.",
}

# Programas de sistema que só deveriam existir em System32/SysWOW64.
NOMES_DE_SISTEMA = {"svchost.exe", "csrss.exe", "lsass.exe", "services.exe", "winlogon.exe",
                    "smss.exe", "wininit.exe", "conhost.exe", "taskhostw.exe", "dllhost.exe",
                    "rundll32.exe", "spoolsv.exe"}

PASTAS_SUSPEITAS = {
    "temp": "Executando a partir da pasta temporária",
    "downloads": "Executando direto da pasta Downloads",
    "$recycle.bin": "Executando de dentro da Lixeira",
}


def local_suspeito(caminho: str) -> str:
    if not caminho:
        return ""
    partes = [p.lower() for p in Path(caminho).parts[:-1]]
    for pasta, motivo in PASTAS_SUSPEITAS.items():
        if pasta in partes:
            return motivo
    return ""


def nome_de_sistema_fora_do_lugar(nome: str, caminho: str) -> str:
    if not caminho or (nome or "").lower() not in NOMES_DE_SISTEMA:
        return ""
    pasta = str(Path(caminho).parent).lower()
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    permitidas = (str(Path(windir) / "system32"), str(Path(windir) / "syswow64"))
    if pasta not in permitidas:
        return f"Usa o nome de um programa do Windows ({nome}) fora da pasta System32"
    return ""
