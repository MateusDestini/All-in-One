"""Atualização automática: ao abrir, confere a release "latest" do GitHub e instala a versão nova sozinho.

O GitHub Actions compila a cada envio para a main e publica na release "latest" o DestiniSetup.exe e um versao.json
({versao, build, commit, sha256, tamanho}). O monitor instalado compara o número do build dele com o da release; se
houver um mais novo, baixa o instalador, confere o SHA-256, fecha, instala em silêncio e abre de novo.

Só atualiza sozinho a cópia instalada pelo DestiniSetup (tem o desinstalador do lado do .exe). A versão portátil e a
rodada pelo Python só avisam. Se a mesma atualização falhar duas vezes em 24 h, ele para de tentar até a próxima.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__, config, windows

log = logging.getLogger(__name__)

REPOSITORIO = "MateusDestini/All-in-One"
API = f"https://api.github.com/repos/{REPOSITORIO}/releases/tags/latest"
PAGINA = f"https://github.com/{REPOSITORIO}/releases/latest"

try:  # gerado pelo GitHub Actions na hora de compilar
    from ._build import BUILD, COMMIT  # type: ignore
except ImportError:
    BUILD, COMMIT = 0, ""

_trava = threading.Lock()
_estado: dict = {"verificando": False, "baixando": False, "progresso": 0.0, "ultima": None, "erro": None, "nova": None}


def _pedir(url: str, timeout: float = 15) -> urllib.request.addinfourl:
    req = urllib.request.Request(url, headers={"User-Agent": f"Destini-Monitor/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def instalado() -> bool:
    """Cópia instalada pelo DestiniSetup (o Inno Setup deixa o unins000.exe do lado)."""
    if not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).parent / "unins000.exe").exists()


def _ler_release(dados: dict) -> dict | None:
    """Da resposta da API: {versao, build, commit, sha256, tamanho, url} do instalador, ou None."""
    ativos = {a.get("name"): a for a in dados.get("assets") or []}
    setup, info = ativos.get("DestiniSetup.exe"), ativos.get("versao.json")
    if not setup or not info:
        return None
    with _pedir(info["browser_download_url"]) as r:
        v = json.loads(r.read(64_000).decode("utf-8-sig"))
    try:
        build = int(v.get("build") or 0)
    except (TypeError, ValueError):
        return None
    return {"versao": str(v.get("versao") or "?"), "build": build, "commit": str(v.get("commit") or ""),
            "sha256": str(v.get("sha256") or "").lower(), "tamanho": int(v.get("tamanho") or setup.get("size") or 0),
            "url": setup["browser_download_url"], "publicada": dados.get("published_at")}


def verificar() -> dict:
    """Confere se há build mais novo. {ok, nova: bool, release: {...}}"""
    _estado.update(verificando=True, erro=None)
    try:
        with _pedir(API) as r:
            dados = json.loads(r.read(2_000_000).decode("utf-8"))
        rel = _ler_release(dados)
        if not rel:
            raise ValueError("a release não tem o instalador")
        nova = rel["build"] > BUILD
        _estado.update(ultima=time.time(), nova=rel if nova else None)
        return {"ok": True, "nova": nova, "release": rel}
    except Exception as e:
        log.info("atualização: não consegui conferir (%s)", e)
        if isinstance(e, urllib.error.HTTPError) and e.code == 404:
            erro = "Ainda não há versão publicada no GitHub."
        elif isinstance(e, urllib.error.HTTPError) and e.code == 403:
            erro = "O GitHub pediu para esperar um pouco (limite de consultas)."
        elif isinstance(e, OSError):
            erro = "Sem conexão com o GitHub agora."
        else:
            erro = str(e)
        _estado.update(ultima=time.time(), erro=erro)
        return {"ok": False, "erro": erro}
    finally:
        _estado["verificando"] = False


# ------------------------------------------------------------------ tentativas (para não ficar em laço)
def _tentativas() -> dict:
    try:
        return json.loads((config.pasta_dados() / "atualizacao.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _registrar_tentativa(build: int) -> None:
    t = _tentativas()
    if t.get("build") != build or time.time() - t.get("quando", 0) > 86400:
        t = {"build": build, "vezes": 0}
    t.update(vezes=t.get("vezes", 0) + 1, quando=time.time())
    try:
        (config.pasta_dados() / "atualizacao.json").write_text(json.dumps(t), encoding="utf-8")
    except OSError:
        pass


def desistiu(build: int) -> bool:
    t = _tentativas()
    return t.get("build") == build and t.get("vezes", 0) >= 2 and time.time() - t.get("quando", 0) < 86400


# ------------------------------------------------------------------ baixar e instalar
def baixar(rel: dict) -> Path:
    pasta = config.pasta_dados() / "atualizacao"
    pasta.mkdir(exist_ok=True)
    for velho in pasta.glob("DestiniSetup-*.exe"):
        try:
            velho.unlink()
        except OSError:
            pass
    destino = pasta / f"DestiniSetup-{rel['build']}.exe"
    h, total = hashlib.sha256(), 0
    _estado.update(baixando=True, progresso=0.0)
    try:
        with _pedir(rel["url"], timeout=30) as r, open(destino, "wb") as f:
            while True:
                bloco = r.read(256 * 1024)
                if not bloco:
                    break
                f.write(bloco)
                h.update(bloco)
                total += len(bloco)
                if rel.get("tamanho"):
                    _estado["progresso"] = min(1.0, total / rel["tamanho"])
    finally:
        _estado["baixando"] = False
    if rel.get("tamanho") and total != rel["tamanho"]:
        destino.unlink(missing_ok=True)
        raise ValueError(f"download incompleto ({total} de {rel['tamanho']} bytes)")
    if rel.get("sha256") and h.hexdigest() != rel["sha256"]:
        destino.unlink(missing_ok=True)
        raise ValueError("o instalador baixado não confere com o SHA-256 publicado")
    return destino


def script_instalacao(setup: Path, exe: Path, bandeja: bool, iniciar_com_windows: bool) -> str:
    """Script que espera o monitor fechar, instala em silêncio e abre de novo."""
    log_setup = config.pasta_dados() / "atualizacao" / "instalador.log"
    tarefas = "iniciar" if iniciar_com_windows else ""
    return "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",  # caminhos com acento (o arquivo é UTF-8)
        "ping -n 4 127.0.0.1 >nul",  # ~3 s para o monitor fechar
        f'"{setup}" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS /SP- /TASKS="{tarefas}" /LOG="{log_setup}"',
        f'start "" "{exe}"' + (" --bandeja" if bandeja else ""),
        f'del "{setup}"',
        '(goto) 2>nul & del "%~f0"',
    ]) + "\r\n"


def instalar(rel: dict, bandeja: bool) -> dict:
    """Baixa, confere e deixa o instalador rodando por fora. Quem chama fecha o monitor logo depois."""
    if not instalado():
        return {"ok": False, "mensagem": f"Esta cópia não foi instalada pelo DestiniSetup. Baixe a versão nova em {PAGINA}"}
    with _trava:
        _registrar_tentativa(rel["build"])
        setup = baixar(rel)
        cmd = config.pasta_dados() / "atualizacao" / "atualizar.cmd"
        cmd.write_text(script_instalacao(setup, Path(sys.executable), bandeja, windows.autostart_ativo()), encoding="utf-8")
        flags = windows.CREATE_NO_WINDOW | 0x00000200  # sem janela, novo grupo de processos
        try:
            subprocess.Popen(["cmd.exe", "/c", str(cmd)], creationflags=flags | 0x01000000, close_fds=True, cwd=str(cmd.parent))
        except OSError:  # sem permissão para sair do job: vai sem
            subprocess.Popen(["cmd.exe", "/c", str(cmd)], creationflags=flags, close_fds=True, cwd=str(cmd.parent))
    log.info("atualização: instalando o build %s (%s)", rel["build"], rel["versao"])
    return {"ok": True, "mensagem": f"Atualizando para a versão {rel['versao']}. O monitor reabre sozinho em instantes."}


# ------------------------------------------------------------------ ao abrir
def ao_abrir(notificar, sair, bandeja: bool, espera: float = 8.0) -> None:
    """Thread de fundo: confere e, se tiver build novo, instala e fecha (o script abre o monitor de novo)."""
    def tarefa():
        time.sleep(espera)
        if not config.preferencias().get("atualizar_sozinho", True) or os.environ.get("DESTINI_SEM_ATUALIZAR"):
            return
        r = verificar()
        if not r.get("ok") or not r.get("nova"):
            return
        rel = r["release"]
        if not instalado():
            notificar("Versão nova do Destini", f"Saiu a versão {rel['versao']}. Esta cópia é portátil: baixe pelo GitHub (Ajustes).")
            return
        if desistiu(rel["build"]):
            log.warning("atualização: o build %s já falhou duas vezes hoje; tento amanhã", rel["build"])
            return
        try:
            res = instalar(rel, bandeja)
        except Exception as e:
            log.exception("atualização falhou")
            _estado["erro"] = f"A atualização falhou: {e}"
            return
        if res.get("ok"):
            notificar("Atualizando o Destini", res["mensagem"])
            time.sleep(1.5)
            sair()

    threading.Thread(target=tarefa, name="atualizador", daemon=True).start()


def estado() -> dict:
    return {"versao": __version__, "build": BUILD, "commit": COMMIT, "instalado": instalado(),
            "ligado": config.preferencias().get("atualizar_sozinho", True), "pagina": PAGINA,
            **{k: v for k, v in _estado.items()}}
