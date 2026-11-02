"""Programas que abrem com o Windows: listar, ativar/desativar, remover e restaurar.

Ativar/desativar usa as mesmas chaves "StartupApproved" do Gerenciador de Tarefas: a entrada continua
lá e pode ser religada. Remover apaga a entrada, guardando uma cópia para "Restaurar removidos".
Itens de "todos os usuários" (HKLM e pasta comum) precisam do monitor aberto como administrador.

Lógica adaptada do ResetExplode (StartupManager.cs, licença MIT, © 2026 Gustavo).
"""

import json
import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import config, windows

log = logging.getLogger(__name__)

RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN32 = r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run"
APROVADOS = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\StartupApproved\\"
PRECISA_ADMIN = ("Esse programa vale para todos os usuários do PC. Para mudar, abra o monitor como administrador "
                 "(Ajustes > Abrir como administrador).")


def _pastas():
    return [
        (Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup", False),
        (Path(os.environ.get("PROGRAMDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup", True),
    ]


def _raiz(todos: bool):
    import winreg
    return winreg.HKEY_LOCAL_MACHINE if todos else winreg.HKEY_CURRENT_USER


def _tipo_aprovado(tipo: str) -> str:
    return "StartupFolder" if tipo == "Pasta" else tipo


def _desativado(todos: bool, tipo: str, nome: str) -> bool:
    import winreg
    try:
        with winreg.OpenKey(_raiz(todos), APROVADOS + _tipo_aprovado(tipo)) as k:
            dado, _ = winreg.QueryValueEx(k, nome)
        # 02/06 = ativado, 03/07 = desativado; sem valor = ativado
        return isinstance(dado, bytes) and len(dado) > 0 and bool(dado[0] & 1)
    except OSError:
        return False


def executavel_do_comando(cmd: str) -> str | None:
    """'"C:\\x\\a.exe" -min' -> 'C:\\x\\a.exe'. None quando não dá para saber."""
    cmd = os.path.expandvars(cmd.strip())
    if not cmd:
        return None
    if cmd[0] == '"':
        fim = cmd.find('"', 1)
        if fim <= 1:
            return None
        caminho = cmd[1:fim]
    else:
        exe = cmd.lower().find(".exe")
        espaco = cmd.find(" ")
        caminho = cmd[:exe + 4] if exe > 0 else cmd[:espaco] if espaco > 0 else cmd
    if len(caminho) > 2 and (caminho[1] == ":" or caminho.startswith("\\\\")):
        return caminho
    sistema = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / caminho
    return str(sistema) if sistema.exists() else None


def _item(nome, comando, origem_todos, tipo, arquivo=None):
    exe = arquivo if tipo == "Pasta" and not str(arquivo).lower().endswith(".lnk") else executavel_do_comando(comando)
    existe = exe is None or Path(exe).exists()
    aprovado_nome = Path(arquivo).name if tipo == "Pasta" else nome
    return {
        "id": f"{'M' if origem_todos else 'U'}|{tipo}|{aprovado_nome}",
        "nome": nome, "comando": comando, "origem": "Todos" if origem_todos else "Usuário",
        "tipo": tipo, "ativo": not _desativado(origem_todos, tipo, aprovado_nome),
        "descricao": windows.descricao_arquivo(exe) if exe and existe else "",
        "arquivo_existe": existe,
    }


def listar() -> list[dict]:
    if not windows.EH_WINDOWS:
        return []
    import winreg
    itens = []
    for todos, caminho, tipo in ((False, RUN, "Run"), (True, RUN, "Run"), (True, RUN32, "Run32")):
        try:
            with winreg.OpenKey(_raiz(todos), caminho, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    nome, comando, _ = winreg.EnumValue(k, i)
                    if nome:
                        itens.append(_item(nome, str(comando), todos, tipo))
        except OSError:
            pass
    for pasta, todos in _pastas():
        if pasta.is_dir():
            for arq in pasta.iterdir():
                if arq.is_file() and arq.name.lower() != "desktop.ini":
                    itens.append(_item(arq.stem, str(arq), todos, "Pasta", str(arq)))
    return sorted(itens, key=lambda i: i["nome"].lower())


def _achar(item_id: str) -> dict | None:
    return next((i for i in listar() if i["id"] == item_id), None)


def definir_ativo(item_id: str, ativo: bool) -> dict:
    import winreg
    item = _achar(item_id)
    if not item:
        return {"ok": False, "mensagem": "Esse item não existe mais. Atualize a lista."}
    todos, tipo, nome = item["origem"] == "Todos", item["tipo"], item_id.split("|", 2)[2]
    dado = bytearray(12)
    dado[0] = 2 if ativo else 3
    if not ativo:  # data da desativação (FILETIME), como o Gerenciador de Tarefas grava
        dado[4:12] = int((time.time() + 11644473600) * 10**7).to_bytes(8, "little")
    try:
        with winreg.CreateKeyEx(_raiz(todos), APROVADOS + _tipo_aprovado(tipo), 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, nome, 0, winreg.REG_BINARY, bytes(dado))
    except PermissionError:
        return {"ok": False, "mensagem": PRECISA_ADMIN}
    return {"ok": True, "mensagem": f"{item['nome']} {'vai' if ativo else 'não vai mais'} abrir com o Windows."}


# ------------------------------------------------------------------ remover / restaurar

def _arquivo_removidos() -> Path:
    return config.pasta_dados() / "inicializacao_removidos.json"


def removidos() -> list[dict]:
    try:
        return json.loads(_arquivo_removidos().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _salvar_removidos(lista):
    _arquivo_removidos().write_text(json.dumps(lista, ensure_ascii=False, indent=1), encoding="utf-8")


def remover(item_id: str) -> dict:
    import winreg
    item = _achar(item_id)
    if not item:
        return {"ok": False, "mensagem": "Esse item não existe mais. Atualize a lista."}
    todos, tipo, nome = item["origem"] == "Todos", item["tipo"], item_id.split("|", 2)[2]
    copia = {"nome": item["nome"], "tipo": tipo, "todos": todos, "comando": item["comando"],
             "quando": datetime.now().strftime("%d/%m/%Y %H:%M")}
    try:
        if tipo == "Pasta":
            pasta = config.pasta_dados() / "inicializacao_removidos"
            pasta.mkdir(exist_ok=True)
            destino = pasta / f"{datetime.now():%Y%m%d-%H%M%S}-{nome}"
            shutil.move(item["comando"], destino)
            copia.update(backup=str(destino), original=item["comando"])
        else:
            caminho = RUN32 if tipo == "Run32" else RUN
            with winreg.OpenKey(_raiz(todos), caminho, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
                winreg.DeleteValue(k, nome)
    except PermissionError:
        return {"ok": False, "mensagem": PRECISA_ADMIN}
    except OSError as e:
        return {"ok": False, "mensagem": f"Não foi possível remover: {e}"}
    try:
        with winreg.OpenKey(_raiz(todos), APROVADOS + _tipo_aprovado(tipo), 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, nome)
    except OSError:
        pass
    _salvar_removidos(removidos() + [copia])
    return {"ok": True, "mensagem": f"{item['nome']} foi removido da inicialização. Dá para desfazer em Removidos."}


def restaurar(indice: int) -> dict:
    import winreg
    lista = removidos()
    if not 0 <= indice < len(lista):
        return {"ok": False, "mensagem": "Item não encontrado."}
    c = lista[indice]
    try:
        if c["tipo"] == "Pasta":
            if not Path(c.get("backup", "")).exists():
                return {"ok": False, "mensagem": "A cópia guardada não existe mais."}
            Path(c["original"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(c["backup"], c["original"])
        else:
            caminho = RUN32 if c["tipo"] == "Run32" else RUN
            with winreg.CreateKeyEx(_raiz(c["todos"]), caminho, 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
                winreg.SetValueEx(k, c["nome"], 0, winreg.REG_SZ, c["comando"])
    except PermissionError:
        return {"ok": False, "mensagem": PRECISA_ADMIN}
    except OSError as e:
        return {"ok": False, "mensagem": f"Não foi possível restaurar: {e}"}
    del lista[indice]
    _salvar_removidos(lista)
    return {"ok": True, "mensagem": f"{c['nome']} voltou para a inicialização."}
