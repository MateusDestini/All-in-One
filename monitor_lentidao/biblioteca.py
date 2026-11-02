"""Biblioteca de jogos: acha os jogos instalados e abre cada um.

Só lê arquivos e registro; nada no PC é alterado. Lojas:
- Steam: `libraryfolders.vdf` + `appmanifest_*.acf` de cada biblioteca; tempo jogado e última vez vêm do
  `localconfig.vdf` da conta que usou a Steam por último; capas do cache da própria Steam.
- Epic Games: manifestos `*.item` (JSON) em %ProgramData%\\Epic\\EpicGamesLauncher\\Data\\Manifests.
- GOG: registro HKLM\\SOFTWARE\\WOW6432Node\\GOG.com\\Games.
- Ubisoft Connect: registro HKLM\\SOFTWARE\\WOW6432Node\\Ubisoft\\Launcher\\Installs.
- Riot (VALORANT, League of Legends...): %ProgramData%\\Riot Games\\Metadata.
- Adicionados à mão pelo painel (ficam nas preferências do monitor).

Os jogos abrem pelo próprio launcher (steam://, com.epicgames.launcher://, uplay://). Com o monitor aberto
como administrador, o jogo é aberto pelo Explorer para não herdar o modo administrador.
"""

import base64
import io
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata

import psutil

from . import config, windows

log = logging.getLogger(__name__)

LOJAS = {"steam": "Steam", "epic": "Epic Games", "gog": "GOG", "ubisoft": "Ubisoft Connect", "riot": "Riot Games",
         "manual": "Adicionado por você"}

# ------------------------------------------------------------------ VDF (formato de texto da Steam)

_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])|//[^\n]*|([^\s{}"]+)')
_ESCAPES = {"\\\\": "\\", '\\"': '"', "\\n": "\n", "\\t": "\t"}


def _desescapar(s: str) -> str:
    return re.sub(r'\\[\\"nt]', lambda m: _ESCAPES[m.group(0)], s) if "\\" in s else s


def ler_vdf(texto: str) -> dict:
    """KeyValues da Steam para dicionários aninhados. Chaves repetidas: vale a última."""
    raiz: dict = {}
    pilha = [raiz]
    chave = None
    for m in _TOKEN.finditer(texto):
        aspas, chave_ou_fecha, solto = m.groups()
        if chave_ou_fecha == "{":
            novo: dict = {}
            if chave is not None:
                pilha[-1][chave] = novo
            pilha.append(novo)
            chave = None
        elif chave_ou_fecha == "}":
            if len(pilha) > 1:
                pilha.pop()
            chave = None
        elif aspas is not None or solto is not None:
            valor = _desescapar(aspas) if aspas is not None else solto
            if chave is None:
                chave = valor
            else:
                pilha[-1][chave] = valor
                chave = None
    return raiz


def _g(d, *chaves):
    """Busca aninhada sem diferenciar maiúsculas (a Steam mistura "apps", "Apps", "Software", "software")."""
    for c in chaves:
        if not isinstance(d, dict):
            return None
        if c in d:
            d = d[c]
            continue
        cl = c.lower()
        d = next((v for k, v in d.items() if k.lower() == cl), None)
    return d


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ registro

def _reg_valor(raiz: str, caminho: str, nome: str):
    if not windows.EH_WINDOWS:
        return None
    import winreg
    try:
        with winreg.OpenKey(getattr(winreg, raiz), caminho) as k:
            return winreg.QueryValueEx(k, nome)[0]
    except OSError:
        return None


def _reg_subchaves(raiz: str, caminho: str) -> list[dict]:
    """Subchaves com todos os valores de cada uma: [{"_nome": sub, valor: dado...}]."""
    if not windows.EH_WINDOWS:
        return []
    import winreg
    saida = []
    try:
        with winreg.OpenKey(getattr(winreg, raiz), caminho) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                d = {"_nome": sub}
                try:
                    with winreg.OpenKey(k, sub) as ks:
                        j = 0
                        while True:
                            try:
                                n, v, _ = winreg.EnumValue(ks, j)
                            except OSError:
                                break
                            d[n] = v
                            j += 1
                except OSError:
                    pass
                saida.append(d)
    except OSError:
        pass
    return saida


def _program_data() -> str:
    return os.environ.get("ProgramData") or r"C:\ProgramData"


def _existe_pasta(p) -> bool:
    try:
        return bool(p) and os.path.isdir(p)
    except (OSError, ValueError):
        return False


def _jogo(loja, id_, nome, pasta, **extra) -> dict:
    pasta = os.path.normpath(pasta) if pasta else ""
    disco = os.path.splitdrive(pasta)[0].upper() if pasta else ""
    j = {"chave": f"{loja}:{id_}", "loja": loja, "loja_nome": LOJAS[loja], "id": str(id_), "nome": nome.strip(),
         "pasta": pasta, "disco": disco, "exe": None, "uri": None, "args": "", "tamanho": None, "ultimo": None,
         "tempo_min": None, "capa": False, "situacao": None}
    j.update(extra)
    return j


# ------------------------------------------------------------------ Steam

STEAM_IGNORAR = {"228980", "1070560", "1391110", "1628350", "1493710", "2180100", "1826330"}
STEAM_IGNORAR_PREFIXOS = ("proton", "steam linux runtime", "steamworks common", "steamvr beta")


def pasta_steam() -> str | None:
    for raiz, caminho, nome in (("HKEY_CURRENT_USER", r"Software\Valve\Steam", "SteamPath"),
                                ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                                ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Valve\Steam", "InstallPath")):
        p = _reg_valor(raiz, caminho, nome)
        if p and _existe_pasta(p):
            return os.path.normpath(p)
    return None


def bibliotecas_steam(steam: str) -> list[str]:
    pastas = [steam]
    for arq in (os.path.join(steam, "steamapps", "libraryfolders.vdf"), os.path.join(steam, "config", "libraryfolders.vdf")):
        try:
            with open(arq, encoding="utf-8", errors="replace") as f:
                dados = _g(ler_vdf(f.read()), "libraryfolders") or {}
        except OSError:
            continue
        for k, v in dados.items():
            if not k.isdigit():
                continue
            p = v if isinstance(v, str) else _g(v, "path")
            if p:
                pastas.append(os.path.normpath(p))
        break
    vistos, saida = set(), []
    for p in pastas:
        n = os.path.normcase(p)
        if n not in vistos:
            vistos.add(n)
            saida.append(p)
    return saida


_cache_localconfig: dict = {}


def _localconfig_steam(steam: str) -> dict:
    """{appid: {"ultimo": unix, "tempo_min": minutos}} da conta que usou a Steam por último."""
    pasta = os.path.join(steam, "userdata")
    candidatos = []
    try:
        for d in os.scandir(pasta):
            arq = os.path.join(d.path, "config", "localconfig.vdf")
            try:
                candidatos.append((os.path.getmtime(arq), arq))
            except OSError:
                pass
    except OSError:
        return {}
    if not candidatos:
        return {}
    mtime, arq = max(candidatos)
    if _cache_localconfig.get("arq") == arq and _cache_localconfig.get("mtime") == mtime:
        return _cache_localconfig["apps"]
    try:
        with open(arq, encoding="utf-8", errors="replace") as f:
            raiz = ler_vdf(f.read())
    except OSError:
        return {}
    apps = _g(raiz, "UserLocalConfigStore", "Software", "Valve", "Steam", "apps") or {}
    saida = {}
    for appid, d in apps.items():
        if isinstance(d, dict):
            saida[appid] = {"ultimo": _int(_g(d, "LastPlayed")) or None, "tempo_min": _int(_g(d, "Playtime"))}
    _cache_localconfig.update(arq=arq, mtime=mtime, apps=saida)
    return saida


def _capa_steam(steam: str, appid: str) -> str | None:
    """Capa vertical (600x900) ou, se não houver, o cabeçalho horizontal, do cache da Steam."""
    cache = os.path.join(steam, "appcache", "librarycache")
    for nome in ("library_600x900.jpg", "library_capsule.jpg", "header.jpg"):
        for p in (os.path.join(cache, f"{appid}_{nome}"), os.path.join(cache, appid, nome)):
            if os.path.isfile(p):
                return p
        sub = os.path.join(cache, appid)
        try:  # Steam nova: librarycache/<appid>/<hash>/library_600x900.jpg
            for d in os.scandir(sub):
                if d.is_dir():
                    p = os.path.join(d.path, nome)
                    if os.path.isfile(p):
                        return p
        except OSError:
            pass
    return None


def _steam() -> list[dict]:
    steam = pasta_steam()
    if not steam:
        return []
    uso = _localconfig_steam(steam)
    jogos = []
    for lib in bibliotecas_steam(steam):
        apps = os.path.join(lib, "steamapps")
        try:
            arquivos = [e.path for e in os.scandir(apps) if e.name.startswith("appmanifest_") and e.name.endswith(".acf")]
        except OSError:
            continue
        for acf in arquivos:
            try:
                with open(acf, encoding="utf-8", errors="replace") as f:
                    st = _g(ler_vdf(f.read()), "AppState")
            except OSError:
                continue
            if not isinstance(st, dict):
                continue
            appid, nome, pasta = _g(st, "appid"), _g(st, "name"), _g(st, "installdir")
            if not appid or not nome or not pasta or appid in STEAM_IGNORAR:
                continue
            if nome.lower().startswith(STEAM_IGNORAR_PREFIXOS):
                continue
            flags = _int(_g(st, "StateFlags")) or 0
            if flags & 1 or flags & 2048:  # desinstalado / desinstalando
                continue
            completo = os.path.join(apps, "common", pasta)
            if not _existe_pasta(completo):
                continue
            situacao = None
            if flags & (32 | 128):
                situacao = "arquivos"
            elif flags and not flags & 4:
                situacao = "atualizando"
            u = uso.get(appid, {})
            ultimo = u.get("ultimo") or _int(_g(st, "LastPlayed")) or None
            jogos.append(_jogo("steam", appid, nome, completo, uri=f"steam://rungameid/{appid}",
                               tamanho=_int(_g(st, "SizeOnDisk")), ultimo=ultimo, tempo_min=u.get("tempo_min"),
                               capa=bool(_capa_steam(steam, appid)), situacao=situacao))
    return jogos


# ------------------------------------------------------------------ Epic Games

def _epic() -> list[dict]:
    pasta = os.path.join(_program_data(), "Epic", "EpicGamesLauncher", "Data", "Manifests")
    jogos = []
    try:
        arquivos = [e.path for e in os.scandir(pasta) if e.name.endswith(".item")]
    except OSError:
        return []
    for arq in arquivos:
        try:
            with open(arq, encoding="utf-8", errors="replace") as f:
                r = json.load(f)
        except (OSError, ValueError):
            continue
        nome, local, app = r.get("DisplayName"), r.get("InstallLocation"), r.get("AppName")
        if not nome or not local or not app or not _existe_pasta(local):
            continue
        cats = r.get("AppCategories")
        if isinstance(cats, list) and cats and "games" not in cats:
            continue  # Unreal Engine, plugins...
        principal = r.get("MainGameAppName")
        if principal and principal != app:
            continue  # DLC
        ns, item = r.get("CatalogNamespace"), r.get("CatalogItemId")
        exe = os.path.join(local, r["LaunchExecutable"]) if r.get("LaunchExecutable") else None
        uri = (f"com.epicgames.launcher://apps/{ns}%3A{item}%3A{app}?action=launch&silent=true" if ns and item else None)
        jogos.append(_jogo("epic", app, nome, local, uri=uri, exe=exe if exe and os.path.isfile(exe) else None,
                           tamanho=_int(r.get("InstallSize")),
                           situacao="atualizando" if r.get("bIsIncompleteInstall") else None))
    return jogos


# ------------------------------------------------------------------ GOG

def _gog() -> list[dict]:
    jogos, vistos = [], set()
    for caminho in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        for d in _reg_subchaves("HKEY_LOCAL_MACHINE", caminho):
            gid = d["_nome"]
            nome, pasta, exe = d.get("gameName") or d.get("GAMENAME"), d.get("path") or d.get("PATH"), d.get("exe") or d.get("EXE")
            if gid in vistos or not nome or not _existe_pasta(pasta) or (d.get("dependsOn") or "").strip():
                continue
            vistos.add(gid)
            jogos.append(_jogo("gog", gid, nome, pasta, exe=exe if exe and os.path.isfile(exe) else None,
                               args=str(d.get("launchParam") or "").strip(), cwd=d.get("workingDir") or None))
    return jogos


# ------------------------------------------------------------------ Ubisoft Connect

def _ubisoft() -> list[dict]:
    jogos = []
    for d in _reg_subchaves("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"):
        uid = d["_nome"]
        pasta = str(d.get("InstallDir") or "").replace("/", "\\").rstrip("\\")
        if not _existe_pasta(pasta):
            continue
        nome = _reg_valor("HKEY_LOCAL_MACHINE", rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Uplay Install {uid}",
                          "DisplayName") or os.path.basename(pasta)
        jogos.append(_jogo("ubisoft", uid, str(nome), pasta, uri=f"uplay://launch/{uid}/0"))
    return jogos


# ------------------------------------------------------------------ Riot Games

RIOT_NOMES = {"valorant": "VALORANT", "league_of_legends": "League of Legends", "bacon": "Legends of Runeterra",
              "lion": "2XKO"}


def _riot() -> list[dict]:
    base = os.path.join(_program_data(), "Riot Games")
    cliente = None
    try:
        with open(os.path.join(base, "RiotClientInstalls.json"), encoding="utf-8") as f:
            inst = json.load(f)
        for k in ("rc_default", "rc_live", "rc_beta"):
            if inst.get(k) and os.path.isfile(inst[k]):
                cliente = os.path.normpath(inst[k])
                break
    except (OSError, ValueError):
        pass
    jogos = []
    meta = os.path.join(base, "Metadata")
    try:
        produtos = [e for e in os.scandir(meta) if e.is_dir()]
    except OSError:
        return []
    for e in produtos:
        produto, _, linha = e.name.partition(".")
        if produto not in RIOT_NOMES:
            continue
        arq = os.path.join(e.path, f"{e.name}.product_settings.yaml")
        try:
            with open(arq, encoding="utf-8", errors="replace") as f:
                texto = f.read()
        except OSError:
            continue
        m = re.search(r'^product_install_full_path:\s*"?([^"\r\n]+)"?', texto, re.M)
        if not m or not _existe_pasta(m.group(1).strip()):
            continue
        jogos.append(_jogo("riot", e.name, RIOT_NOMES[produto], m.group(1).strip(), exe=cliente,
                           args=f"--launch-product={produto} --launch-patchline={linha or 'live'}"))
    return jogos


# ------------------------------------------------------------------ adicionados à mão e ocultos

def manuais() -> list[dict]:
    return [m for m in config.preferencias().get("jogos_manuais", []) if isinstance(m, dict) and m.get("exe")]


def _manuais() -> list[dict]:
    jogos = []
    for m in manuais():
        exe = m["exe"]
        if not os.path.isfile(exe):
            continue
        nome = m.get("nome") or os.path.splitext(os.path.basename(exe))[0]
        jogos.append(_jogo("manual", os.path.normcase(exe), nome, os.path.dirname(exe), exe=exe,
                           args=str(m.get("args") or "")))
    return jogos


def adicionar(exe: str, nome: str = "") -> dict:
    exe = os.path.normpath(str(exe))
    if not exe.lower().endswith(".exe") or not os.path.isfile(exe):
        return {"ok": False, "erro": "Escolha um arquivo .exe que exista."}
    lista = manuais()
    if any(os.path.normcase(m["exe"]) == os.path.normcase(exe) for m in lista):
        return {"ok": False, "erro": "Esse jogo já está na biblioteca."}
    nome = (nome or "").strip() or _nome_bonito(exe)
    config.definir_preferencia("jogos_manuais", lista + [{"exe": exe, "nome": nome}])
    _esquecer()
    return {"ok": True, "chave": f"manual:{os.path.normcase(exe)}"}


def _nome_bonito(exe: str) -> str:
    """Nome do jogo pela descrição do arquivo ou pela pasta (o .exe costuma ter nome feio)."""
    desc = ""
    try:
        desc = windows.descricao_arquivo(exe) or ""
    except Exception:
        pass
    if desc and desc.lower() not in ("launcher", "game", "application"):
        return desc
    base = os.path.splitext(os.path.basename(exe))[0]
    pasta = os.path.basename(os.path.dirname(exe))
    if re.search(r"(win64|win32|shipping|binaries|bin|x64)", base + pasta, re.I):
        partes = [p for p in os.path.normpath(exe).split(os.sep)[:-1]
                  if p and not re.fullmatch(r"(?i)binaries|win64|win32|bin|x64|x86|game|retail|_retail_|live", p)]
        if partes:
            return partes[-1]
    return base


def remover_manual(chave: str) -> dict:
    alvo = chave.split(":", 1)[1] if ":" in chave else chave
    lista = [m for m in manuais() if os.path.normcase(m["exe"]) != alvo]
    config.definir_preferencia("jogos_manuais", lista)
    _esquecer()
    return {"ok": True}


def ocultos() -> list[str]:
    return list(config.preferencias().get("jogos_ocultos", []))


def ocultar(chave: str, sim: bool) -> dict:
    lista = [c for c in ocultos() if c != chave]
    if sim:
        lista.append(chave)
    config.definir_preferencia("jogos_ocultos", lista)
    return {"ok": True, "ocultos": lista}


def renomear(chave: str, nome: str) -> dict:
    """Só para os adicionados à mão (nas lojas o nome vem da loja)."""
    if not chave.startswith("manual:"):
        return {"ok": False}
    alvo, nome = chave.split(":", 1)[1], (nome or "").strip()
    if not nome:
        return {"ok": False}
    lista = manuais()
    for m in lista:
        if os.path.normcase(m["exe"]) == alvo:
            m["nome"] = nome
    config.definir_preferencia("jogos_manuais", lista)
    _esquecer()
    return {"ok": True}


# ------------------------------------------------------------------ FPS estimado (tabela do Gargalo)

_APELIDOS = {"grandtheftautov": "gtav", "grandtheftautovlegacy": "gtav", "grandtheftautovenhanced": "gtav",
             "tomclancysrainbowsixsiege": "rainbowsixsiege", "tomclancysrainbowsixsiegex": "rainbowsixsiege",
             "rainbowsixsiegex": "rainbowsixsiege", "overwatch": "overwatch2", "minecraftlauncher": "minecraft",
             "thewitcher3wildhuntcompleteedition": "thewitcher3wildhunt", "callofdutywarzone20": "callofdutywarzone"}


def normalizar(nome: str) -> str:
    s = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9]", "", s)
    return _APELIDOS.get(s, s)


def mapa_fps(tabela: dict) -> dict:
    return {normalizar(j["nome"]): j for j in tabela.get("jogos", [])}


# ------------------------------------------------------------------ lista

_cache: dict = {}
_trava = threading.Lock()
VALIDADE_S = 120


def _esquecer() -> None:
    _cache.clear()


def listar(forcar: bool = False, tabela_fps: dict | None = None) -> dict:
    with _trava:
        if forcar or not _cache or time.time() - _cache.get("t", 0) > VALIDADE_S:
            jogos, erros = [], []
            for nome, fn in (("Steam", _steam), ("Epic Games", _epic), ("GOG", _gog), ("Ubisoft Connect", _ubisoft),
                             ("Riot Games", _riot), ("manual", _manuais)):
                try:
                    jogos.extend(fn())
                except Exception:
                    log.exception("falha ao ler a biblioteca %s", nome)
                    erros.append(nome)
            vistos, unicos = set(), []
            for j in jogos:
                if j["chave"] not in vistos:
                    vistos.add(j["chave"])
                    unicos.append(j)
            unicos.sort(key=lambda j: normalizar(j["nome"]) or j["nome"].lower())
            _cache.update(t=time.time(), jogos=unicos, erros=erros)
    jogos = [dict(j) for j in _cache["jogos"]]
    if tabela_fps:
        mapa = mapa_fps(tabela_fps)
        for j in jogos:
            e = mapa.get(normalizar(j["nome"]))
            if e:
                j["fps_estimado"] = {"fps": e["fps"], "low1": e["low1"], "limite": e["limite"]}
    escondidos = set(ocultos())
    for j in jogos:
        j["oculto"] = j["chave"] in escondidos
        j.pop("cwd", None)
    lojas = {}
    for j in jogos:
        if not j["oculto"]:
            lojas[j["loja"]] = lojas.get(j["loja"], 0) + 1
    return {"jogos": jogos, "lojas": lojas, "erros": _cache.get("erros", []), "lido_em": _cache.get("t"),
            "steam": bool(pasta_steam())}


def _achar(chave: str) -> dict | None:
    if not _cache:
        listar()
    return next((j for j in _cache.get("jogos", []) if j["chave"] == chave), None)


# ------------------------------------------------------------------ rodando agora

def rodando() -> dict:
    """{chave: pid} dos jogos com algum processo aberto dentro da pasta do jogo."""
    jogos = _cache.get("jogos") or []
    pastas, exes = [], {}
    for j in jogos:
        if j["loja"] == "manual":
            exes[os.path.normcase(j["exe"])] = j["chave"]
        elif j["pasta"] and len(j["pasta"]) > 3:
            pastas.append((os.path.normcase(j["pasta"]).rstrip("\\/") + os.sep, j["chave"]))
    if not pastas and not exes:
        return {}
    achados = {}
    for p in psutil.process_iter(["pid", "exe"]):
        exe = p.info.get("exe")
        if not exe:
            continue
        e = os.path.normcase(exe)
        chave = exes.get(e) or next((c for pasta, c in pastas if e.startswith(pasta)), None)
        if chave and chave not in achados:
            achados[chave] = p.info["pid"]
    return achados


# ------------------------------------------------------------------ abrir

def _abrir_shell(alvo: str) -> None:
    """Abre um endereço ou programa como o usuário comum, mesmo com o monitor como administrador."""
    if windows.eh_admin():
        subprocess.Popen(["explorer.exe", alvo], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.startfile(alvo)  # type: ignore[attr-defined]


def _abrir_exe(exe: str, args: str, cwd: str | None) -> None:
    if not args:
        _abrir_shell(exe)
    else:
        os.startfile(exe, "open", args, cwd or os.path.dirname(exe))  # type: ignore[attr-defined]


def jogar(chave: str) -> dict:
    j = _achar(chave)
    if not j:
        return {"ok": False, "erro": "Jogo não encontrado. Atualize a biblioteca."}
    if not windows.EH_WINDOWS:
        return {"ok": False, "erro": "Só funciona no Windows."}
    try:
        if j["uri"]:
            _abrir_shell(j["uri"])
        elif j["exe"]:
            _abrir_exe(j["exe"], j["args"], j.get("cwd"))
        else:
            return {"ok": False, "erro": "Não achei o executável deste jogo."}
    except OSError as e:
        log.exception("falha ao abrir %s", chave)
        return {"ok": False, "erro": f"O Windows não abriu o jogo ({e.strerror or e})."}
    return {"ok": True, "loja": j["loja_nome"]}


def abrir_pasta(chave: str) -> dict:
    j = _achar(chave)
    if not j or not _existe_pasta(j["pasta"]):
        return {"ok": False, "erro": "A pasta do jogo não existe mais."}
    if windows.EH_WINDOWS:
        subprocess.Popen(["explorer.exe", j["pasta"]])
    return {"ok": True}


def verificar_arquivos(chave: str) -> dict:
    """Pede à Steam para conferir os arquivos do jogo (ela mesma baixa o que estiver faltando ou corrompido)."""
    j = _achar(chave)
    if not j or j["loja"] != "steam":
        return {"ok": False, "erro": "Só para jogos da Steam."}
    if windows.EH_WINDOWS:
        _abrir_shell(f"steam://validate/{j['id']}")
    return {"ok": True}


# ------------------------------------------------------------------ capas e tamanho

_capas: dict = {}


def capa(chave: str) -> str | None:
    """Capa como data URI (reduzida para ~300 px de largura). None se não houver."""
    if chave in _capas:
        return _capas[chave]
    j = _achar(chave)
    url = None
    if j and j["loja"] == "steam":
        steam = pasta_steam()
        caminho = _capa_steam(steam, j["id"]) if steam else None
        if caminho:
            url = _data_uri(caminho)
    _capas[chave] = url
    return url


def _data_uri(caminho: str) -> str | None:
    try:
        with open(caminho, "rb") as f:
            dados = f.read()
    except OSError:
        return None
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(dados))
        if img.width > 320:
            img = img.convert("RGB")
            img.thumbnail((320, 480))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            dados = buf.getvalue()
    except Exception:
        pass
    return "data:image/jpeg;base64," + base64.b64encode(dados).decode()


_tamanhos: dict = {}


def tamanho(chave: str, limite_s: float = 20.0) -> int | None:
    """Tamanho da pasta (para lojas que não informam). Para de contar após `limite_s`."""
    j = _achar(chave)
    if not j:
        return None
    if j["tamanho"]:
        return j["tamanho"]
    if chave in _tamanhos:
        return _tamanhos[chave]
    if j["loja"] == "manual":
        return None  # a pasta de um .exe solto pode ser qualquer coisa
    total, fim = 0, time.time() + limite_s
    pilha = [j["pasta"]]
    while pilha:
        if time.time() > fim:
            return None
        try:
            with os.scandir(pilha.pop()) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            pilha.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    _tamanhos[chave] = total
    return total
