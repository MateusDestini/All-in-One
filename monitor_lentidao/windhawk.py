"""Integração com o Windhawk (https://windhawk.net), que personaliza o Windows com mods.

O monitor não instala nem altera mods: mostra se o Windhawk está instalado e rodando, lista os mods
com versão e estado, e avisa quando um mod é carregado em todos os programas (o que pode pesar).

Onde o Windhawk 1.x guarda as coisas (modo instalado):
- `windhawk.ini` na pasta do programa, seção [Storage]: Portable, AppDataPath, RegistryKey;
- configuração de cada mod em `HKLM\\SOFTWARE\\Windhawk\\Engine\\Mods\\<id>` (Disabled, Version, Include...);
- código de cada mod em `<AppDataPath>\\ModsSource\\<id>.wh.cpp`, com o cabeçalho // ==WindhawkMod==.
No modo portátil a configuração fica em arquivos .ini dentro de AppDataPath.
O Windhawk 2 traz o `windhawk-cli`, que é usado quando existe.
"""

import configparser
import json
import logging
import os
import re
import subprocess
from pathlib import Path

import psutil

from . import windows

log = logging.getLogger(__name__)

WINGET_ID = "RamenSoftware.Windhawk"
SITE = "https://windhawk.net"
CHAVE_PADRAO = r"HKEY_LOCAL_MACHINE\SOFTWARE\Windhawk"


# ------------------------------------------------------------------ onde está instalado

def _desinstalacao() -> dict:
    """Lê a entrada "Windhawk" de Programas e Recursos (pasta e versão)."""
    if not windows.EH_WINDOWS:
        return {}
    import winreg
    caminho = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Windhawk"
    for raiz in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for vista in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(raiz, caminho, 0, winreg.KEY_READ | vista) as k:
                    def valor(nome):
                        try:
                            return str(winreg.QueryValueEx(k, nome)[0])
                        except OSError:
                            return ""
                    return {"pasta": valor("InstallLocation").strip('"'), "versao": valor("DisplayVersion"),
                            "icone": valor("DisplayIcon").strip('"')}
            except OSError:
                continue
    return {}


def _pasta_programa(info: dict) -> Path | None:
    candidatos = []
    if info.get("pasta"):
        candidatos.append(Path(info["pasta"]))
    if info.get("icone"):
        candidatos.append(Path(info["icone"].split(",")[0]).parent)
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if os.environ.get(var):
            candidatos.append(Path(os.environ[var]) / "Windhawk")
    return next((p for p in candidatos if (p / "windhawk.exe").exists()), None)


def _ini(pasta: Path | None) -> dict:
    """[Storage] do windhawk.ini: modo portátil, pasta de dados e chave do registro."""
    r = {"portatil": False, "dados": Path(os.path.expandvars(r"%ProgramData%\Windhawk")), "chave": CHAVE_PADRAO}
    if not pasta or not (pasta / "windhawk.ini").exists():
        return r
    cp = configparser.ConfigParser(interpolation=None)
    try:
        cp.read(pasta / "windhawk.ini", encoding="utf-8-sig")
    except (configparser.Error, OSError):
        log.warning("windhawk.ini ilegível", exc_info=True)
        return r
    s = cp["Storage"] if cp.has_section("Storage") else {}
    try:
        r["portatil"] = int(s.get("Portable", "0") or 0) != 0
    except ValueError:
        pass
    if s.get("AppDataPath"):
        dados = Path(os.path.expandvars(s["AppDataPath"]))
        r["dados"] = dados if dados.is_absolute() else pasta / dados
    if s.get("RegistryKey"):
        r["chave"] = s["RegistryKey"]
    return r


def _cli(pasta: Path | None) -> Path | None:
    if not pasta:
        return None
    return next((p for p in (pasta / "windhawk-cli.exe", pasta / "cli" / "windhawk-cli.exe") if p.exists()), None)


def rodando() -> bool:
    for p in psutil.process_iter(["name"]):
        if (p.info.get("name") or "").lower() == "windhawk.exe":
            return True
    return False


# ------------------------------------------------------------------ mods

_CABECALHO = re.compile(r"//\s*==WindhawkMod==(.*?)//\s*==/WindhawkMod==", re.S)


def ler_cabecalho(texto: str) -> dict:
    """Metadados do mod (// @id, @name, @description, @version, @author, @include, @exclude)."""
    m = _CABECALHO.search(texto)
    meta: dict = {"include": [], "exclude": []}
    if not m:
        return meta
    for linha in m.group(1).splitlines():
        lm = re.match(r"\s*//\s*@([\w-]+)(?::([\w-]+))?\s+(.*?)\s*$", linha)
        if not lm:
            continue
        chave, idioma, valor = lm.group(1), lm.group(2), lm.group(3)
        if chave in ("include", "exclude"):
            meta[chave].append(valor)
        elif idioma is None:
            meta.setdefault(chave, valor)
        elif idioma.lower() in ("pt-br", "pt") and chave in ("name", "description"):
            meta[chave + "_pt"] = valor
    return meta


def _fonte(dados: Path, mod_id: str) -> dict:
    for pasta in (dados / "ModsSource", dados / "Data" / "ModsSource"):
        arq = pasta / f"{mod_id}.wh.cpp"
        if arq.exists():
            try:
                return ler_cabecalho(arq.read_text(encoding="utf-8", errors="replace")[:20000])
            except OSError:
                pass
    return {"include": [], "exclude": []}


def _lista_campo(v) -> list[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split("|") if x.strip()]


def _mods_registro(chave: str) -> list[dict]:
    if not windows.EH_WINDOWS:
        return []
    import winreg
    raizes = {"HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE, "HKLM": winreg.HKEY_LOCAL_MACHINE,
              "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER, "HKCU": winreg.HKEY_CURRENT_USER}
    raiz_nome, _, resto = chave.partition("\\")
    raiz = raizes.get(raiz_nome.upper())
    if raiz is None:
        return []
    mods = []
    try:
        with winreg.OpenKey(raiz, resto + r"\Engine\Mods", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            i = 0
            while True:
                try:
                    mod_id = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                with winreg.OpenKey(k, mod_id) as km:
                    def valor(nome, padrao=None):
                        try:
                            return winreg.QueryValueEx(km, nome)[0]
                        except OSError:
                            return padrao
                    mods.append({"id": mod_id, "desligado": bool(valor("Disabled", 0)), "versao": str(valor("Version", "") or ""),
                                 "include": _lista_campo(valor("Include", "")), "exclude": _lista_campo(valor("Exclude", "")),
                                 "include_extra": _lista_campo(valor("IncludeCustom", "")),
                                 "so_extra": bool(valor("IncludeExcludeCustomOnly", 0))})
    except OSError:
        pass
    return mods


def _mods_portatil(dados: Path) -> list[dict]:
    mods = []
    pasta = dados / "Engine" / "Mods"
    for arq in sorted(pasta.glob("*.ini")) if pasta.exists() else []:
        cp = configparser.ConfigParser(interpolation=None)
        try:
            cp.read(arq, encoding="utf-8-sig")
        except (configparser.Error, OSError):
            continue
        valores = {k.lower(): v for s in cp.sections() for k, v in cp[s].items()}
        mods.append({"id": arq.stem, "desligado": valores.get("disabled", "0") not in ("0", ""),
                     "versao": valores.get("version", ""), "include": _lista_campo(valores.get("include", "")),
                     "exclude": _lista_campo(valores.get("exclude", "")),
                     "include_extra": _lista_campo(valores.get("includecustom", "")),
                     "so_extra": valores.get("includeexcludecustomonly", "0") not in ("0", "")})
    return mods


def _mods_cli(cli: Path) -> list[dict] | None:
    try:
        r = subprocess.run([str(cli), "mod", "list", "--json"], capture_output=True, timeout=30,
                           creationflags=windows.CREATE_NO_WINDOW if windows.EH_WINDOWS else 0)
        dados = json.loads(r.stdout.decode("utf-8", errors="replace") or "null")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        log.warning("windhawk-cli mod list falhou", exc_info=True)
        return None
    if isinstance(dados, dict):
        dados = dados.get("mods") or dados.get("items") or list(dados.values())
    if not isinstance(dados, list):
        return None
    mods = []
    for d in dados:
        if not isinstance(d, dict):
            continue
        cfg = d.get("config") if isinstance(d.get("config"), dict) else {}
        meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
        junto = {**meta, **cfg, **d}
        mod_id = junto.get("id") or junto.get("modId") or junto.get("mod_id")
        if not mod_id:
            continue
        desligado = junto.get("disabled")
        if desligado is None and "enabled" in junto:
            desligado = not junto["enabled"]
        mods.append({"id": str(mod_id), "desligado": bool(desligado), "versao": str(junto.get("version") or ""),
                     "nome": junto.get("name") or junto.get("displayName"), "descricao": junto.get("description"),
                     "include": _lista_campo(junto.get("include")), "exclude": _lista_campo(junto.get("exclude")),
                     "include_extra": _lista_campo(junto.get("includeCustom") or junto.get("include_custom")),
                     "so_extra": bool(junto.get("includeExcludeCustomOnly") or junto.get("include_exclude_custom_only"))})
    return mods


def _alvo(mod: dict) -> tuple[str, bool]:
    """Texto curto de onde o mod carrega e se pega todos os programas."""
    inc = mod["include_extra"] if mod.get("so_extra") else mod["include"] + mod["include_extra"]
    todos = any(x.strip() in ("*", "*.exe") for x in inc)
    if todos:
        txt = "Todos os programas"
        if mod["exclude"]:
            txt += f" (menos {len(mod['exclude'])})"
    elif not inc:
        txt = "—"
    else:
        nomes = [Path(x.replace("\\", "/")).name for x in inc]
        txt = ", ".join(nomes[:3]) + (f" e mais {len(nomes) - 3}" if len(nomes) > 3 else "")
    return txt, todos


def estado() -> dict:
    info = _desinstalacao()
    pasta = _pasta_programa(info)
    if not pasta:
        return {"instalado": False, "rodando": rodando(), "mods": [], "site": SITE, "winget": WINGET_ID}
    ini = _ini(pasta)
    cli = _cli(pasta)
    mods = _mods_cli(cli) if cli else None
    origem = "cli" if mods is not None else ("portatil" if ini["portatil"] else "registro")
    if mods is None:
        mods = _mods_portatil(ini["dados"]) if ini["portatil"] else _mods_registro(ini["chave"])
    lista = []
    for m in mods:
        meta = _fonte(ini["dados"], m["id"])
        if not m["include"] and meta["include"]:
            m["include"], m["exclude"] = meta["include"], m["exclude"] or meta["exclude"]
        alvo, todos = _alvo(m)
        lista.append({"id": m["id"], "nome": meta.get("name_pt") or m.get("nome") or meta.get("name") or m["id"],
                      "descricao": meta.get("description_pt") or m.get("descricao") or meta.get("description") or "",
                      "autor": meta.get("author", ""), "versao": m["versao"] or meta.get("version", ""),
                      "ligado": not m["desligado"], "alvo": alvo, "todos": todos,
                      "pagina": f"{SITE}/mods/{m['id']}" if not m["id"].startswith("local@") else ""})
    lista.sort(key=lambda x: (not x["ligado"], not x["todos"], x["nome"].lower()))
    return {"instalado": True, "rodando": rodando(), "versao": info.get("versao", ""), "pasta": str(pasta),
            "portatil": ini["portatil"], "origem": origem, "mods": lista, "site": SITE, "winget": WINGET_ID}


def abrir() -> bool:
    pasta = _pasta_programa(_desinstalacao())
    if not pasta or not windows.EH_WINDOWS:
        return False
    os.startfile(str(pasta / "windhawk.exe"))
    return True


def instalar(progresso) -> dict:
    from . import apps
    progresso(0.1, "Instalando o Windhawk pelo winget... baixando do site oficial, pode levar alguns minutos.")
    ok, msg = apps.instalar_winget(WINGET_ID)
    progresso(1.0, "Concluído")
    return {"ok": ok, "mensagem": msg}


# ------------------------------------------------------------------ ligar e desligar (pedido pelo Mateus)

_ID_VALIDO = re.compile(r"^[A-Za-z0-9@._-]{1,120}$")


def alternar(mod_id: str, ligar: bool) -> dict:
    """Liga ou desliga um mod do mesmo jeito que o Windhawk: windhawk-cli, se existir; senão o valor Disabled."""
    if not _ID_VALIDO.match(mod_id or ""):
        return {"ok": False, "mensagem": "Mod inválido."}
    pasta = _pasta_programa(_desinstalacao())
    if not pasta:
        return {"ok": False, "mensagem": "O Windhawk não está instalado."}
    ini = _ini(pasta)
    cli = _cli(pasta)
    acao = "ligado" if ligar else "desligado"
    if cli:
        try:
            r = subprocess.run([str(cli), "mod", "enable" if ligar else "disable", mod_id], capture_output=True, timeout=60,
                               creationflags=windows.CREATE_NO_WINDOW if windows.EH_WINDOWS else 0)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {"ok": False, "mensagem": f"O windhawk-cli não respondeu ({e})."}
        if r.returncode == 0:
            log.info("windhawk: mod %s %s pelo windhawk-cli", mod_id, acao)
            return {"ok": True, "mensagem": f"Mod {acao}."}
        saida = (r.stdout + r.stderr).decode("utf-8", errors="replace").strip().splitlines()
        return {"ok": False, "mensagem": f"O Windhawk recusou ({saida[-1] if saida else r.returncode})."}
    if ini["portatil"]:
        arq = ini["dados"] / "Engine" / "Mods" / f"{mod_id}.ini"
        if not arq.exists():
            return {"ok": False, "mensagem": "Não achei a configuração desse mod."}
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # mantém Disabled com D maiúsculo
        cp.read(arq, encoding="utf-8-sig")
        secao = cp.sections()[0] if cp.sections() else "Mod"
        if not cp.has_section(secao):
            cp.add_section(secao)
        chave = next((k for k in cp[secao] if k.lower() == "disabled"), "Disabled")
        cp[secao][chave] = "0" if ligar else "1"
        with open(arq, "w", encoding="utf-8") as f:
            cp.write(f, space_around_delimiters=False)
        log.info("windhawk: mod %s %s (modo portátil)", mod_id, acao)
        return {"ok": True, "mensagem": f"Mod {acao}."}
    if not windows.EH_WINDOWS:
        return {"ok": False, "mensagem": "Só funciona no Windows."}
    import winreg
    raiz_nome, _, resto = ini["chave"].partition("\\")
    raiz = winreg.HKEY_CURRENT_USER if raiz_nome.upper() in ("HKEY_CURRENT_USER", "HKCU") else winreg.HKEY_LOCAL_MACHINE
    try:
        with winreg.OpenKey(raiz, resto + r"\Engine\Mods" + "\\" + mod_id, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, "Disabled", 0, winreg.REG_DWORD, 0 if ligar else 1)
    except PermissionError:
        return {"ok": False, "mensagem": "O Windows só deixa mudar os mods com o monitor aberto como administrador "
                                         "(botão em Ajustes)."}
    except OSError as e:
        return {"ok": False, "mensagem": f"Não consegui mudar ({e})."}
    log.info("windhawk: mod %s %s (registro)", mod_id, acao)
    return {"ok": True, "mensagem": f"Mod {acao}. O Windhawk aplica na hora; se algum programa não mudar, "
                                    "use Reiniciar o Windhawk."}


def reiniciar() -> dict:
    """Fecha e abre de novo o windhawk.exe (não mexe em serviços do Windows)."""
    pasta = _pasta_programa(_desinstalacao())
    if not pasta or not windows.EH_WINDOWS:
        return {"ok": False, "mensagem": "O Windhawk não está instalado."}
    procs = [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == "windhawk.exe"]
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, vivos = psutil.wait_procs(procs, timeout=5)
    if vivos:
        return {"ok": False, "mensagem": "O Windhawk não fechou. Abra o monitor como administrador e tente de novo."}
    os.startfile(str(pasta / "windhawk.exe"))
    log.info("windhawk reiniciado pelo monitor")
    return {"ok": True, "mensagem": "Windhawk reiniciado."}
