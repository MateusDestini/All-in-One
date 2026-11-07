# Destini · Monitor de Lentidão (Windows 11)

*Seu PC está manco?*

Fica na bandeja do sistema (perto do relógio) e avisa quando encontra algo que
deixa o PC lento, explicando a causa e o que fazer.

O ícone é o logo Destini; com um aviso ganha um **ponto amarelo** e com um problema crítico, um **ponto vermelho**.
Clique duas vezes nele para abrir o **painel** (preto e branco, em blocos, com tema escuro ou claro e o menu em
categorias que abrem e fecham: Hardware, Jogos, Desempenho e Ferramentas):

- **Visão geral**: nota de saúde do PC (0 a 100), medidores de Processador, Memória, Disco e Espaço
  livre com gráfico dos últimos 2 minutos, e um cartão por problema com "O que fazer"; logo abaixo,
  **Desempenho ao vivo**: gráfico de temperatura, carga ou consumo do processador × placa de vídeo
  (1, 5 ou 15 min), anéis de uso, tabela de componentes, lista "Saúde do PC" e eventos recentes
- **Programas**: tudo que está aberto, ordenado por impacto (Alto/Médio/Baixo), com busca. Clique num
  programa para pesquisar "o que é este programa?", abrir o local do arquivo ou fechá-lo
- **Teste de memória**: mede leitura, escrita e cópia da RAM (GB/s) e a latência dos caches L1, L2, L3
  e da RAM (ns), no estilo do AIDA64, com gráfico, comparação com o teste anterior e explicação do
  resultado (canal único, XMP/EXPO desligado etc.)
- **Inicialização**: o que abre junto com o Windows
- **Histórico**: alertas anteriores
- **Ajustes**: notificações (Todas, Só graves ou Desligadas), tema (Escuro, Claro ou o do Windows), aviso de
  pendrive/USB conectado, iniciar com o Windows, atualizar sozinho ao abrir, relatório completo (HTML ou JSON) ou em texto, abrir a pasta de configuração

No rodapé do painel ficam sempre as temperaturas do processador e da placa de vídeo, e o ícone de Sensores
fica vermelho a partir de 90 °C.

Para silenciar os avisos, use o botão **Notificações** no topo do painel, a tela Ajustes ou
botão direito no ícone > *Notificações*. Desligadas, o monitor continua verificando e registrando tudo.

Fechar a janela não encerra o monitor; ele continua na bandeja. Para sair, botão direito no ícone > *Sair*.

O painel usa o WebView2 da Microsoft, que já vem no Windows 11. Se ele não estiver disponível,
o monitor abre um painel mais simples automaticamente.

## O que ele verifica

**Sistema (a cada 5 s, com média de 2 min para não alarmar à toa)**
- CPU, memória RAM e disco sobrecarregados, mostrando quais programas são os culpados
- Disco respondendo devagar (sinal de disco sobrecarregado ou falhando)
- Processador abaixo da velocidade normal mesmo ocupado (superaquecimento / modo de energia)

**Programas que roubam desempenho**
- Uso de CPU, RAM e disco **por programa**, somando todos os processos (ex.: as 30 abas do Chrome)
- Vazamento de memória: programa que cresce sem parar por 30 min
- Programas que acumulam *handles* (recursos presos do Windows)
- Programas suspeitos: rodando da pasta Temp/Downloads/Lixeira ou usando nome de programa
  do Windows (ex.: `svchost.exe`) fora da System32, padrão comum de malware e mineradores
- Dois ou mais antivírus ao mesmo tempo
- Dicas específicas para os vilões comuns: Defender, indexação da Pesquisa, Windows Update,
  OneDrive, navegadores, Teams, Discord, WMI etc.

**A cada 10 minutos**
- Pouco espaço livre em disco
- Quantidade de programas que abrem com o Windows
- Visualizador de Eventos (últimas 24 h): setores defeituosos, aviso SMART de falha do disco,
  corrupção do NTFS, resets do controlador de disco, erros de hardware (WHEA), memória virtual
  esgotada, CPU limitada pelo firmware, driver de vídeo travando, serviços falhando,
  programas travando/fechando
- **Reinícios sozinhos** (últimos 7 dias): para cada um, o motivo provável. Tela azul com o código
  e o que ele significa, erro de hardware antes de cair, botão de energia segurado, Windows Update
  ou outro programa que mandou reiniciar, ou queda sem registro (energia, fonte, superaquecimento).
  O aviso aparece logo depois que o PC volta a ligar; a lista completa fica em **Histórico**
- Tempo do último boot e quem atrasou a inicialização (precisa rodar como administrador para ler)
- Saúde dos discos e se o Windows está em HD mecânico
- Plano de energia em "Economia" e PC sem reiniciar há muitos dias

## Hardware (vindo do ResetExplode)

- **Sistema**: Windows, placa-mãe, BIOS, processador, memória RAM (pentes, slots, canais, ECC, tensões e se o
  perfil EXPO/XMP parece ligado), vídeo e discos com partições, além de um cartão por unidade (C:, D:...)
  com o espaço livre.
- **Sensores**: todas as temperaturas, cargas, clocks, ventoinhas, consumo e tensões com mínimo e máximo, com
  filtro por categoria. Usa a [LibreHardwareMonitorLib](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
  num processo separado, que só roda enquanto o painel está aberto. **Temperatura e consumo do processador** exigem
  o monitor aberto como administrador e o driver **PawnIO**; a própria página diz o que falta e tem o botão para
  instalar o PawnIO (pelo winget). Sem a biblioteca, ainda mostra a carga do processador e das placas de vídeo e os
  sensores das placas NVIDIA.
- **Placa de vídeo**: modelo, fabricante da placa, GPU, arquitetura, VRAM, barramento, PCIe (atual e máximo),
  driver, DirectX, limites de consumo e temperatura e, nas NVIDIA, leitura ao vivo com o que está limitando o clock.
- **NVIDIA** e **AMD Radeon**: abas que só aparecem se o PC tiver a placa. Overclock pelo driver oficial (NVML na
  NVIDIA; ADL Overdrive 8 na AMD, RX 5000 em diante), sempre dentro da faixa que o driver aceita: limite de energia,
  clock do núcleo e da memória, alvo de temperatura, ventoinha e teto de clock (NVIDIA) ou clock máximo/mínimo,
  tensão (undervolt), memória, energia, TDC e ventoinha (AMD). Cada placa mostra só o que o driver dela deixa mudar.
  Tem **5 perfis** e um deles pode ser **aplicado sozinho quando você entra no Windows** (o Windows pede confirmação
  uma vez para criar a tarefa agendada que aplica como administrador). Proteção: se o PC desligar de repente ou o
  driver de vídeo cair até 3 minutos depois de aplicar, o monitor volta a placa ao padrão, desliga a aplicação
  automática e avisa. Mudar a placa exige o monitor aberto como administrador; ver e salvar perfis, não.
- **Processador**: overclock e undervolt do processador direto do Windows, no estilo do Ryzen Master (AMD) e do XTU
  (Intel). Ryzen 7000/9000: limites PPT/TDC/EDC, limite de temperatura, boost a mais e **Curve Optimizer por núcleo**.
  Intel: PL1/PL2/Tau, IccMax e undervolt. O **Ajuste automático** acha a melhor configuração sozinho (objetivo
  desempenho, equilíbrio ou eficiência): desce o Curve Optimizer ou a tensão aos poucos e testa um núcleo por vez com
  um teste que confere as contas e os erros de hardware do Windows, volta ao último valor estável e deixa uma margem.
  Também tem o **Teste por núcleo**, 5 perfis e "aplicar ao iniciar" (a mesma tarefa da placa de vídeo). Os valores
  somem ao reiniciar o PC, então uma configuração ruim nunca prende o PC; se ele travar logo depois de aplicar, o
  monitor não aplica de novo e avisa. Usa o driver **PawnIO** e os módulos assinados dele (nada de WinRing0) e precisa
  do monitor aberto como administrador. Ryzen 5000 e anteriores não entram.
- **Timings da RAM**: diz qual é o seu kit (decifra o código do pente de G.Skill, Corsair, Kingston, Crucial,
  TeamGroup, XPG, Patriot e Lexar), **qual chip ele usa** (lido do próprio pente, pelo SPD, quando o monitor está como
  administrador; senão, estimado pelo perfil do kit) e, nos Ryzen, **os timings que estão valendo agora** (primários,
  secundários, tRFC, FCLK/UCLK/MCLK, GDM e 1T/2T), lidos do controlador de memória. Aponta o que está fora do lugar
  (EXPO desligado, controlador em 1:2, tRFC da JEDEC, um pente só...) e sugere três níveis para a BIOS (perfil do
  fabricante, afinado e agressivo, este só para chips que aguentam), com o nome do campo na BIOS, a estimativa de
  latência e banda e o botão de copiar a lista. A **análise de cada timing** compara o valor (em clocks e em ns)
  com o que o chip costuma aguentar e marca folgado, bom ou apertado; mostra também os terciários (turnaround,
  tMOD, tSTAG, tPHYRDL), canal a canal, Power Down, ECC e as incoerências (tRC menor que tRAS + tRP, tFAW abaixo de
  4 × tRRD_S...).
  **Gravar na BIOS pelo Windows**: nas placas AMD que trazem a interface AOD (a mesma que o Ryzen Master usa), o
  monitor lista o que a BIOS deixa mudar (velocidade da memória, FCLK e UCLK 1:1 pelas listas de opções da BIOS,
  timings primários, secundários, tRFC, turnaround e as tensões VDD, VDDQ e VPP), só aceita valores dentro do mínimo,
  máximo e passo que a própria BIOS informa (com teto do monitor: até 6400 MT/s e FCLK 2200 MHz no AM5, tensões até
  1,45 V), preenche com o nível Perfil do fabricante (faz o papel do EXPO), Afinado ou Agressivo, grava para valer no
  próximo boot, guarda os valores de antes para desfazer, tem a volta ao padrão da BIOS e, depois de reiniciar,
  confere pelo controlador se tudo entrou e se o PC desligou sem avisar. O que a BIOS não oferece pelo Windows (na
  placa testada, o tREFI) continua só na BIOS.
  **Auto-tune**: lê o kit (chip e densidade pelo SPD, ranks, quantos pentes, perfil de fábrica, timings e tensão
  atuais, sensor de temperatura) e monta um plano com o piso de cada timing para aquele chip. Depois acha o limite
  real do kit sozinho: tensão, tRFC (com busca fina no fim), CL, tRCD/tRP, tRAS/tRC, secundários em degraus, tREFI
  (se a BIOS deixar e houver sensor) e FCLK, cada um em busca binária, com um reinício e um teste de estabilidade por
  tentativa. Percebe quando o PC travou ou quando a placa voltou ao padrão no treino, segue com o último valor bom e
  para depois de 3 boots ruins seguidos. No fim roda o teste longo; se achar erro, afrouxa um passo em cada timing e
  testa de novo. Continua sozinho depois de cada reinício por uma tarefa de logon como administrador, apagada no fim.
  Modos Rápido e Completo e a opção de deixar um passo de folga.
  Depois de mudar, o **teste de estabilidade** (5 a 60 min, grava e confere toda a memória livre com padrões feitos
  para pegar timing apertado, mais os erros de hardware do Windows e a temperatura dos pentes) e a medição de
  latência e banda dizem se ficou estável e quanto ganhou. Cada teste fica no histórico com os timings daquele
  momento, e o monitor marca a melhor configuração estável.
- **Drivers**: lista os drivers instalados (com data, versão, fabricante e os que estão com problema), diz se há
  **driver novo da placa de vídeo** (NVIDIA pelo site oficial, com opção Game Ready ou Studio; AMD e Intel com o link
  da página oficial), mostra as **atualizações de driver do Windows Update** e o link de suporte da sua placa-mãe.
  Instalar só acontece quando você manda e pede confirmação: o instalador da NVIDIA só é baixado do site da NVIDIA e
  só roda com a assinatura digital conferida, e o Windows Update instala só o que você marcar. O **backup** copia
  todos os drivers de terceiros para `Documentos\Destini-Drivers` (pelo `pnputil`), para reinstalar depois.
- **Aviso de USB**: notifica quando um pendrive ou outro dispositivo USB é conectado ou removido (dá para desligar
  em Ajustes).

## Desempenho (vindo do ResetExplode)

- **Benchmark**: renderiza uma cena 3D por path tracing em todos os núcleos e em 1 núcleo e dá uma nota em pontos
  (estilo Cinebench, mas com nota própria: não dá para comparar com a do Cinebench). Tem o modo de 10 minutos para
  ver se o clock cai com o calor, e histórico.
- **Benchmark da placa**: mede a placa de vídeo numa janela própria (WebGL 2), em 1080p, 1440p ou 4K e qualidade
  normal, alta ou extrema. A **cena de jogo** tem vale com rio, dezenas de milhares de árvores, sombra em tempo real,
  água, nuvens, neblina e pós-processamento HDR com brilho; a de **ray tracing** calcula sombra suave, oclusão de
  ambiente e reflexos nos shaders (roda em qualquer placa, mas não usa os núcleos de RT nem o DXR, então não se
  compara com o Port Royal ou com jogos com RTX). Aquece 8 s sem medir e faz 3 passadas de 20 s; a nota é a mediana
  (FPS × 10), com 1% low, e o resultado diz se dá para confiar (passadas com menos de 3% de diferença) e compara com
  o último teste igual. Para sozinho se a placa passar de 90 °C.
- **Simulação de jogo**: 30 mil unidades em dois times, com física, colisão e IA em paralelo e a lógica do jogo na
  thread principal. A nota é o FPS médio e o 1% low que o processador aguenta, sem depender da placa de vídeo.
- **Estresse**: processador (AVX2/FMA em todos os threads), memória RAM (grava e confere padrões, conta erros) e placa
  de vídeo (desenho 3D pesado estilo FurMark, em WebGL, numa janela própria que precisa ficar visível). Para sozinho se
  passar do limite de temperatura que você escolher, se a memória errar ou se o driver de vídeo reiniciar. O corte pela
  temperatura do processador precisa do monitor como administrador e do PawnIO (veja Sensores).
- **Gargalo**: mostra ao vivo se o limite é a placa de vídeo, o processador (total ou um núcleo só), a memória de vídeo,
  a RAM ou a temperatura, com o que fazer. A "Análise durante o jogo" lê tudo por 1 a 5 minutos com o jogo aberto e
  dá o resultado com a distribuição. Também mostra o equilíbrio entre o seu processador e a sua placa.
- **Overlay de FPS**: FPS, tempo de quadro, 1% low, gráfico de engasgos e sensores (placa, VRAM, processador, RAM)
  num canto da tela por cima do jogo, como no Afterburner. Não injeta nada no jogo: o Windows avisa cada quadro
  entregue (ETW, o mesmo método do PresentMon), por isso a contagem de FPS precisa do monitor como administrador.
  Liga e desliga com **Ctrl+Shift+O** de dentro do jogo ou pelo menu da bandeja. Funciona sobre jogos em janela sem
  borda e na maioria dos DirectX 11/12 em tela cheia; não aparece em tela cheia exclusiva antiga. O Gargalo também
  usa esse FPS (FPS médio e 1% low no resultado da análise).
- **Biblioteca de jogos**: junta os jogos instalados na Steam, Epic, GOG, Ubisoft Connect e Riot (e os que você
  adicionar), com capa, tamanho e o botão de jogar. Só lê e abre: não muda nada no PC.
- **FPS nos jogos**: estimativa de FPS e 1% low em 32 jogos conhecidos para o seu processador e placa, por
  resolução, qualidade e DLSS/FSR. É uma conta local e aproximada (a partir de uma tabela de desempenho relativo),
  não uma medição; serve para ter uma ideia e saber quem vai limitar em cada jogo.

Só um teste pesado roda por vez, e o Windows não entra em suspensão durante o estresse.

## Leve em segundo plano

O monitor foi feito para não atrapalhar nem um PC lento:
- Lê todos os processos com **uma única consulta ao Windows** a cada 5 s (como o Gerenciador de Tarefas), sem abrir
  processo por processo.
- Com o painel fechado, roda com **prioridade baixa e EcoQoS** (no Windows 11, núcleos e clocks econômicos) e devolve
  a memória que não está usando a cada 10 minutos. Com o painel aberto ou um teste rodando, volta ao normal para não
  atrapalhar as medições.
- O painel (WebView2) **só é carregado quando você abre**; 1 minuto depois de fechar, a página sai da memória.
- As verificações pesadas (eventos, discos, bateria...) rodam só quando faz sentido: o que só muda reiniciando o PC
  roda uma vez, o resto a cada 10 a 60 minutos, e o PowerShell roda com prioridade baixa. "Verificar agora" roda tudo.
- Os sensores (LibreHardwareMonitor) só ficam ligados enquanto o painel está aberto.

## Ferramentas (vindas do ResetExplode)

- **Inicialização**: ligar/desligar cada programa (igual ao Gerenciador de Tarefas), remover e restaurar removidos.
- **Discos**: saúde S.M.A.R.T. (temperatura, desgaste do SSD, horas ligado, erros, falha prevista) e teste de velocidade
  sem cache (sequencial 1 MB, 4K aleatório e 4K com 8 leituras ao mesmo tempo), com histórico. O **carregamento de
  mapa** simula um jogo abrindo uma fase: lê um pacote de 512 MB e mil arquivos de 64 KB em 8 threads, sem cache, e
  dá o tempo de cada parte (os arquivos de teste são apagados no final).
- **Rede**: latência, jitter e perda até o roteador e a internet, DNS, download/upload (speed.cloudflare.com) e bufferbloat,
  dizendo se o problema é Wi-Fi/rede local ou provedor.
- **Otimização**: limpar a memória (cache da RAM e memória dos programas), fechar apps de segundo plano
  (OneDrive, Teams, Widgets...) e ajustes do Windows com "Restaurar originais" (Alto desempenho, Modo de Jogo,
  Game DVR, agendamento de GPU, jogos em janela e **desligar a aceleração do mouse**, que pede confirmação e
  volta aos valores de antes pelo mesmo botão ou por "Restaurar originais").
- **Energia**: plano de energia e saúde da bateria em notebooks.
- **Windhawk**: mostra se o [Windhawk](https://windhawk.net) está instalado e rodando (e instala pelo winget), lista
  os mods com versão e onde carregam, liga e desliga cada um e avisa quando um mod ligado carrega em **todos os
  programas**, o que pode pesar. Usa o `windhawk-cli` quando existe; senão, o mesmo valor `Disabled` que o Windhawk
  grava (em `HKLM\SOFTWARE\Windhawk\Engine\Mods\<mod>`, por isso precisa do monitor aberto como administrador).
  Mods novos se instalam pelo próprio Windhawk.
- **Luzes RGB**: uma cor ou efeito para todas as luzes do PC (cor fixa, respirar, arco-íris, ou a cor seguindo a
  temperatura do processador ou da placa, ou a carga do processador). Fala com o **OpenRGB** (pelo servidor SDK dele,
  que o monitor abre se estiver instalado), o **Corsair iCUE** (a biblioteca oficial da Corsair só é baixada do
  GitHub da Corsair quando você pede, e só é usada com a assinatura da Corsair) e o **Razer Synapse** (Chroma).
  Ao parar, devolve as luzes para o programa do fabricante.
- **Instalar apps**: Steam, Chrome, Discord, Lightshot, 7-Zip, WinRAR e VLC de uma vez, em silêncio, pelo
  winget (sempre a versão oficial mais recente). Os que já estão instalados aparecem marcados.

## Maker (para quem usa Arduino, ESP32 e afins)

- **Portas COM**: lista as portas e os aparelhos seriais, inclusive os **fantasmas** (que não estão conectados mas
  ainda seguram um número de porta), remove os fantasmas que você marcar, **libera os números de porta** presos
  (guarda os de antes para desfazer), mostra quem está segurando uma porta (Arduino IDE, Cura, PuTTY...) e fecha
  esses programas se você pedir.
- **Drivers seriais**: diz qual chip USB-serial cada placa usa (CH340, CP210x, FTDI, Prolific) e o que há de errado
  (driver faltando, erro 10/28/43, Prolific falsificado, FTDI com PID 0000), com o link da página oficial do driver,
  reinstala o aparelho, remove pacotes de driver serial (com backup antes e "Restaurar"), instala ou atualiza a
  Arduino IDE pelo winget e limpa o cache dela. Os drivers que vinham dentro do ResetExplode não vêm junto: o monitor
  manda para o site do fabricante.

Tudo que muda o PC nessas páginas pede confirmação antes, e a maior parte precisa do monitor como administrador.

Algumas ações precisam de administrador; o botão "Abrir como administrador" (em Ajustes) reabre o monitor assim.

## Como usar

### Opção 0: baixar o instalador pronto pelo GitHub

Baixe o **`DestiniSetup.exe`** em [Releases](https://github.com/MateusDestini/All-in-One/releases/latest) e instale.
O arquivo `.github/workflows/compilar.yml` gera o instalador, o `MonitorLentidao-portatil.zip` e o `versao.json`
num Windows do próprio GitHub a cada push na `main` e publica na release **latest**.

**Atualização sozinha**: a cópia instalada confere essa release toda vez que abre. Se o número do build for mais
novo, baixa o instalador, confere o SHA-256 publicado no `versao.json`, fecha, instala em silêncio (mantendo
configurações, histórico e o "iniciar com o Windows") e abre de novo. Dá para desligar ou procurar na hora em
Ajustes. Se a mesma versão falhar duas vezes no dia, ele espera o dia seguinte. A versão portátil só avisa.

### Opção 1: gerar o instalador no seu PC
1. Instale o [Python 3.11 ou mais novo](https://www.python.org/downloads/) marcando **"Add python.exe to PATH"**.
2. Para sair o instalador, instale também o **Inno Setup 6** (`winget install JRSoftware.InnoSetup`).
3. Extraia esta pasta e dê dois cliques em **`compilar.bat`**.
4. Saem o **`dist\DestiniSetup.exe`** e a pasta `dist\MonitorLentidao` (versão portátil, sem instalar).
5. O instalador não pede administrador: instala em `%LOCALAPPDATA%\Programs\Destini`, cria o atalho no menu Iniciar,
   pode ligar **"Abrir junto com o Windows"** e aparece em Aplicativos instalados para desinstalar. Instalar uma
   versão nova por cima fecha o monitor aberto e mantém as configurações e o histórico.

### Opção 2: rodar direto com Python
```
pip install -r requirements.txt
pythonw MonitorLentidao.pyw
```

### Diagnóstico rápido sem ficar na bandeja
Dê dois cliques em `diagnostico.bat`. Ele coleta por 1 minuto e mostra o relatório no console.

> O Windows SmartScreen pode avisar que o .exe é "desconhecido" porque ele não é assinado
> digitalmente. Clique em *Mais informações > Executar assim mesmo*.

## Configuração
Os limites de alerta ficam em `%LOCALAPPDATA%\MonitorLentidao\config.json`
(menu do ícone > *Abrir pasta de dados*). Edite e reinicie o monitor. Na mesma pasta ficam
`alertas.log` (histórico de alertas), `diagnostico.txt` e `monitor.log`.

Para um programa parar de gerar alertas, adicione o nome dele (minúsculo) em `ignorar_processos`.

## Sobre o teste de memória
A parte que mede nanossegundos é escrita em C (`monitor_lentidao/nativo/memteste.c`), porque Python é
lento demais para isso. Ela usa as mesmas técnicas conhecidas dos benchmarks de memória:

- leitura, escrita e cópia com AVX-512, AVX2 ou SSE2, conforme o que o processador suporta;
- escrita "non-temporal", que vai direto para a RAM sem passar pelo cache;
- uma thread presa a cada núcleo físico (e também com todos os threads de SMT), ficando com o melhor
  de várias rodadas;
- na cópia, conta os bytes lidos e escritos, como a maioria dos benchmarks;
- latência com acessos em ordem aleatória que visitam cada página uma vez por volta, para o
  prefetcher dos Ryzen não "adivinhar" os endereços, e páginas grandes quando o Windows permite.

Os valores aparecem em MB/s decimais, a mesma unidade do AIDA64. Mesmo assim não ficam idênticos,
porque o método exato do AIDA64 não é público. Para a latência ficar mais próxima, permita páginas
grandes: secpol.msc > Políticas locais > Atribuição de direitos de usuário > "Bloquear páginas na
memória", adicione seu usuário, reinicie o PC e abra o monitor como administrador.

O `memteste.dll` do pacote foi compilado a partir do `.c`; para gerar de novo, use o MinGW-w64:
`x86_64-w64-mingw32-gcc -O2 -shared -static -s -o memteste.dll memteste.c`.

## Estrutura
- `monitor_lentidao/verificacoes.py`: coleta de dados e regras
- `monitor_lentidao/programas.py`: antivírus conhecidos, dicas por programa, detecção de suspeitos
- `monitor_lentidao/eventos.py`: interpretação do Visualizador de Eventos
- `monitor_lentidao/windows.py`: contadores de desempenho, registro, PowerShell
- `monitor_lentidao/ui/painel.html`: visual do painel (HTML/CSS/JS)
- `monitor_lentidao/memoria.py` e `nativo/memteste.c`: teste de memória
- `monitor_lentidao/painel_web.py`: dados e ações que o painel chama
- `monitor_lentidao/janelas.py`: janela moderna e a reserva simples
- `monitor_lentidao/painel.py`: painel simples (reserva, em tkinter)
- `monitor_lentidao/bandeja.py`: ícone, menu e notificações
- `monitor_lentidao/relatorio.py`: relatório em texto
- `monitor_lentidao/relatorio_html.py`: relatório completo em HTML/JSON
- `monitor_lentidao/sistema.py`, `gpu.py`, `sensores.py`: páginas Sistema, Placa de vídeo e Sensores
- `monitor_lentidao/nativo/lhm/`: LibreHardwareMonitorLib e dependências (.NET Framework)
- `monitor_lentidao/dispositivos.py`: aviso de USB; `apps.py`: instalar apps; `saude.py`: lista "Saúde do PC"
- `monitor_lentidao/benchmark.py` e `nativo/bench.c`: benchmark, simulação de jogo e a carga do estresse
- `monitor_lentidao/estresse.py` e `ui/estresse_gpu.html`: teste de estresse (a janela da placa de vídeo é o HTML)
- `monitor_lentidao/gargalo.py`: detector de gargalo, equilíbrio do hardware e estimativa de FPS
- `monitor_lentidao/windhawk.py`: integração com o Windhawk
- `monitor_lentidao/quadros.py` e `overlay.py`: contagem de FPS por ETW e a janela do overlay
- `monitor_lentidao/oc.py`, `oc_nvidia.py` e `oc_amd.py`: abas NVIDIA e AMD, perfis e aplicação no logon
- `monitor_lentidao/oc_cpu.py`, `oc_ryzen.py` e `oc_intel.py`: aba Processador e o ajuste automático
- `monitor_lentidao/pawnio.py` e `nativo/pawnio/`: acesso ao driver PawnIO e os módulos assinados dele
- `monitor_lentidao/ram_timings.py`: aba Timings da RAM (kit, SPD, controlador de memória, análise, sugestões e teste)
- `monitor_lentidao/ram_auto.py`: auto-tune da RAM (análise do kit, busca timing por timing, teste final, diário entre reinícios)
- `monitor_lentidao/aod.py`: gravação dos timings na BIOS pela interface AOD da AMD (próximo boot)
- `monitor_lentidao/atualizador.py`: atualização sozinha pela release do GitHub
- `nativo/estavel.c`: testes de estabilidade do processador (por núcleo) e da RAM (timings)
- `monitor_lentidao/biblioteca.py`: biblioteca de jogos
- `monitor_lentidao/drivers.py`: drivers, driver novo da placa de vídeo, Windows Update e backup
- `monitor_lentidao/bench_gpu.py` e `ui/bench_gpu.html`: benchmark da placa de vídeo (cena de jogo e ray tracing)
- `monitor_lentidao/rgb.py`: luzes RGB (OpenRGB, Corsair iCUE e Razer Chroma)
- `monitor_lentidao/maker.py`: portas COM e drivers seriais
- `nativo/discoteste.c`: teste de velocidade do disco e de carregamento de mapa
- `monitor_lentidao/ui/marca/`: logo Destini (ícone do programa, da bandeja e do menu)
- `instalador.iss`: instalador (Inno Setup 6)

## Créditos

As páginas Sistema, Sensores, Placa de vídeo, Benchmark, Estresse, Gargalo, FPS nos jogos, Overlay de FPS,
NVIDIA/AMD, Biblioteca de jogos, Instalar apps, Drivers, Benchmark da placa, Luzes RGB, Portas COM, Drivers
seriais e as ferramentas de Inicialização, Discos, Rede, Otimização e Energia foram adaptadas (reescritas em Python) do
[ResetExplode](https://github.com/GustavoReset/ResetExplode), licença MIT, © 2026 Gustavo (GustavoReset).

Os sensores usam a LibreHardwareMonitorLib e as bibliotecas DiskInfoToolkit, RAMSPDToolkit e BlackSharp
(MPL-2.0), HidSharp (Apache 2.0) e pacotes System.* da Microsoft (MIT), distribuídos sem modificação.
Os avisos de licença estão em `LICENCAS-TERCEIROS.txt` e em `monitor_lentidao/nativo/lhm/`.

A aba Processador e a leitura dos timings usam o driver [PawnIO](https://pawnio.eu) com os módulos RyzenSMU,
IntelMSR e AMDFamily17 (LGPL 2.1 ou posterior), distribuídos sem modificação. Os endereços dos registradores do
SMU e do controlador de memória dos Ryzen são fatos técnicos documentados pela comunidade (ZenStates-Core e
ZenTimings); nenhum código desses projetos foi copiado. O SPD dos pentes é lido pela RAMSPDToolkit, que já vem
com a LibreHardwareMonitorLib.

As abas NVIDIA e AMD seguem as estruturas públicas da NVML (NVIDIA) e do ADL SDK da AMD (licença MIT); nenhum
código dessas empresas vai junto: o monitor usa as bibliotecas que já vêm instaladas com o driver de vídeo.
