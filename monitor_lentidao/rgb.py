"""Iluminação RGB: um efeito só para todas as luzes, pelas pontes do OpenRGB, do Corsair iCUE e do Razer Chroma.

- OpenRGB (servidor SDK ligado, porta 6742): placas-mãe MSI, Gigabyte, ASUS e ASRock, memórias, placas de vídeo, coolers,
  fitas e periféricos de várias marcas. Ao parar, cada aparelho volta ao modo que estava antes.
- Corsair iCUE (SDK 4 da Corsair): com o iCUE aberto e o SDK ligado nas configurações dele. A DLL do SDK não vem junto: é
  baixada do GitHub oficial da Corsair só quando você pede, e só é usada se a assinatura digital for da Corsair.
- Razer Chroma (Razer Synapse aberto): uma cor por tipo de aparelho (teclado, mouse, headset, mousepad, Chroma Link).

Efeitos: cor fixa, respirar, arco-íris, temperatura do processador ou da placa de vídeo, carga do processador e apagar.
Nada é gravado nos aparelhos: as cores valem enquanto o efeito está ligado e voltam ao programa de cada marca ao parar.
Protocolos e funções públicas de cada fabricante; ideia da tela do ResetExplode (MainWindow.Integ.cs e LightBackends.cs,
licença MIT); o código é próprio.
"""

import colorsys
import ctypes
import io
import json
import logging
import math
import os
import re
import socket
import struct
import threading
import time
import urllib.request
import zipfile
from collections import deque
from datetime import datetime
from pathlib import Path

import psutil

from . import __version__, config, sensores, windows

log = logging.getLogger(__name__)

EFEITOS = {"fixa": "Cor fixa", "respirar": "Respirar", "arco_iris": "Arco-íris", "temp_cpu": "Temperatura do processador",
           "temp_gpu": "Temperatura da placa de vídeo", "carga_cpu": "Carga do processador", "apagar": "Apagar"}
VELOCIDADES = {"lenta": 0.5, "media": 1.0, "rapida": 2.0}


class Aparelho:
    def __init__(self, ponte, id_: str, nome: str, detalhe: str, leds: int, posicoes: list[float] | None = None, extra=None):
        self.ponte, self.id, self.nome, self.detalhe = ponte, id_, nome, detalhe
        self.leds = max(1, int(leds))
        self.x = posicoes if posicoes and len(posicoes) == self.leds else \
            [i / (self.leds - 1) if self.leds > 1 else 0.0 for i in range(self.leds)]
        self.extra = extra
        self.chave = f"{ponte.nome}|{id_}"

    def json(self) -> dict:
        return {"chave": self.chave, "ponte": self.ponte.nome, "nome": self.nome, "detalhe": self.detalhe, "leds": self.leds}


# ------------------------------------------------------------------ OpenRGB (protocolo do servidor SDK)
class _Leitor:
    def __init__(self, dados: bytes):
        self.d, self.p = dados, 0

    def pega(self, fmt: str):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v[0] if len(v) == 1 else v

    def pula(self, n: int) -> None:
        if self.p + n > len(self.d):
            raise ValueError("pacote curto")
        self.p += n

    def texto(self) -> str:
        n = self.pega("H")
        b = self.d[self.p:self.p + n]
        self.pula(n)
        return b.rstrip(b"\0").decode("utf-8", "replace")


def ler_controlador(dados: bytes, proto: int = 3) -> dict:
    """Campos que interessam do pacote REQUEST_CONTROLLER_DATA: nome, marca, LEDs e o modo ativo (para devolver no fim)."""
    r = _Leitor(dados)
    r.pega("I")  # tamanho
    tipo = r.pega("i")
    nome = r.texto()
    marca = r.texto() if proto >= 1 else ""
    for _ in range(4):  # descrição, versão, serial, local
        r.texto()
    n_modos = r.pega("H")
    ativo = r.pega("i")
    bytes_ativo = None
    for m in range(n_modos):
        ini = r.p
        r.texto()
        r.pula(4 * 4)  # valor, flags, velocidade mín/máx
        if proto >= 3:
            r.pula(4 * 2)  # brilho mín/máx
        r.pula(4 * 3)  # cores mín/máx, velocidade
        if proto >= 3:
            r.pula(4)  # brilho
        r.pula(4 * 2)  # direção, modo de cor
        r.pula(4 * r.pega("H"))
        if m == ativo:
            bytes_ativo = dados[ini:r.p]
    for _ in range(r.pega("H")):  # zonas
        r.texto()
        r.pula(4 * 4)
        r.pula(r.pega("H"))  # matriz
    n_leds = r.pega("H")
    return {"tipo": tipo, "nome": nome, "marca": marca, "leds": n_leds, "modo_ativo": ativo, "bytes_modo": bytes_ativo}


class OpenRGB:
    nome = "OpenRGB"
    intervalo = 0.025
    PEDIR_QTD, PEDIR_DADOS, VERSAO, NOME_CLIENTE = 0, 1, 40, 50
    ATUALIZAR_LEDS, MODO_PERSONALIZADO, ATUALIZAR_MODO = 1050, 1100, 1101

    def __init__(self, host="127.0.0.1", porta=6742):
        self.host, self.porta = host, porta
        self._s: socket.socket | None = None
        self._proto = 3
        self._trava = threading.Lock()

    def _enviar(self, dev: int, tipo: int, carga: bytes = b"") -> None:
        self._s.sendall(b"ORGB" + struct.pack("<III", dev, tipo, len(carga)) + carga)

    def _exato(self, n: int) -> bytes:
        b = bytearray()
        while len(b) < n:
            p = self._s.recv(n - len(b))
            if not p:
                raise OSError("o OpenRGB fechou a conexão")
            b += p
        return bytes(b)

    def _receber(self, tipo: int) -> bytes:
        for _ in range(20):  # o servidor pode mandar avisos no meio
            cab = self._exato(16)
            if cab[:4] != b"ORGB":
                raise OSError("resposta que não é do OpenRGB")
            _, t, tam = struct.unpack("<III", cab[4:])
            if tam > 4_000_000:
                raise OSError("pacote grande demais")
            dados = self._exato(tam) if tam else b""
            if t == tipo:
                return dados
        raise OSError("o OpenRGB não respondeu o esperado")

    def conectar(self) -> tuple[list[Aparelho], str]:
        self.encerrar()
        try:
            self._s = socket.create_connection((self.host, self.porta), timeout=2)
            self._s.settimeout(3)
            self._s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._enviar(0, self.NOME_CLIENTE, b"Destini Monitor\0")
            self._enviar(0, self.VERSAO, struct.pack("<I", 3))
            try:
                servidor = struct.unpack("<I", self._receber(self.VERSAO)[:4])[0]
            except (OSError, socket.timeout):
                self.encerrar()
                return [], "o OpenRGB é antigo demais; atualize para a versão 0.9 ou mais nova"
            self._proto = min(3, servidor)
            if self._proto < 3:
                self.encerrar()
                return [], "o OpenRGB é antigo demais; atualize para a versão 0.9 ou mais nova"
            self._enviar(0, self.PEDIR_QTD)
            qtd = struct.unpack("<I", self._receber(self.PEDIR_QTD)[:4])[0]
            lista = []
            for i in range(min(qtd, 256)):
                self._enviar(i, self.PEDIR_DADOS, struct.pack("<I", self._proto))
                c = ler_controlador(self._receber(self.PEDIR_DADOS), self._proto)
                if c["leds"] <= 0:
                    continue
                lista.append(Aparelho(self, f"{i}:{c['nome']}", c["nome"], f"OpenRGB · {c['marca'] or '—'} · {c['leds']} LEDs",
                                      c["leds"], extra=(i, c["modo_ativo"], c["bytes_modo"])))
            return lista, f"conectado · {len(lista)} aparelho(s) com LEDs"
        except (OSError, socket.timeout, struct.error, ValueError) as e:
            self.encerrar()
            if isinstance(e, ConnectionRefusedError) or "refused" in str(e).lower() or isinstance(e, socket.timeout):
                return [], f"o servidor SDK do OpenRGB não está ligado em {self.host}:{self.porta}"
            return [], f"falha na conversa com o OpenRGB: {e}"

    def assumir(self, a: Aparelho) -> bool:
        try:
            with self._trava:
                self._enviar(a.extra[0], self.MODO_PERSONALIZADO)
            return True
        except (OSError, AttributeError):
            return False

    def enviar(self, a: Aparelho, cores: list[tuple[int, int, int]]) -> bool:
        n = min(len(cores), a.leds)
        corpo = struct.pack("<H", n) + b"".join(struct.pack("<BBBx", *c) for c in cores[:n])
        try:
            with self._trava:
                self._enviar(a.extra[0], self.ATUALIZAR_LEDS, struct.pack("<I", 4 + len(corpo)) + corpo)
            return True
        except (OSError, AttributeError):
            return False

    def liberar(self, a: Aparelho) -> None:
        idx, ativo, modo = a.extra
        if modo is None:
            return
        try:
            with self._trava:
                self._enviar(idx, self.ATUALIZAR_MODO, struct.pack("<Ii", 8 + len(modo), ativo) + modo)
        except (OSError, AttributeError):
            pass

    def encerrar(self) -> None:
        if self._s:
            try:
                self._s.close()
            except OSError:
                pass
        self._s = None


# ------------------------------------------------------------------ Razer Chroma (API REST local do Synapse)
class RazerChroma:
    nome = "Razer Chroma"
    intervalo = 0.1
    ENDERECO = "http://localhost:54235/razer/chromasdk"
    TIPOS = (("keyboard", "Teclado"), ("mouse", "Mouse"), ("headset", "Headset"), ("mousepad", "Mousepad"),
             ("keypad", "Keypad"), ("chromalink", "Chroma Link (luzes do gabinete)"))

    def __init__(self, endereco: str | None = None):
        self.endereco = endereco or self.ENDERECO
        self._uri: str | None = None
        self._batida = 0.0
        self._ultimo: dict[str, int] = {}

    def _pedir(self, metodo: str, url: str, corpo: dict | None = None, timeout: float = 3) -> dict:
        dados = json.dumps(corpo).encode() if corpo is not None else None
        req = urllib.request.Request(url, data=dados, method=metodo, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            texto = r.read().decode("utf-8", "replace")
        try:
            return json.loads(texto) if texto.strip() else {}
        except ValueError:
            return {}

    def conectar(self) -> tuple[list[Aparelho], str]:
        self.encerrar()
        try:
            r = self._pedir("POST", self.endereco, {
                "title": "Destini Monitor", "description": "Iluminação do Destini · Monitor de Lentidão",
                "author": {"name": "Destini", "contact": "https://github.com/MateusDestini/All-in-One"},
                "device_supported": [t for t, _ in self.TIPOS], "category": "application"})
        except OSError:
            return [], "não achei o Razer Synapse com o Chroma; abra o Synapse e ative o Chroma Connect"
        uri = str(r.get("uri") or "")
        if not re.match(r"^http://(localhost|127\.0\.0\.1):\d+/", uri):
            return [], "o Razer Synapse respondeu, mas não abriu a sessão do Chroma"
        self._uri, self._batida = uri.rstrip("/"), time.time()
        lista = [Aparelho(self, t, n, "Razer Chroma · uma cor (se você tiver esse tipo de aparelho)", 1) for t, n in self.TIPOS]
        return lista, "conectado ao Razer Synapse; os tipos que você não tem só não reagem"

    def assumir(self, a: Aparelho) -> bool:
        if self._uri is None:  # a sessão fecha ao parar: abre outra
            self.conectar()
        return self._uri is not None

    def enviar(self, a: Aparelho, cores) -> bool:
        if not self._uri or not cores:
            return False
        r, g, b = cores[0]
        bgr = r | (g << 8) | (b << 16)  # o Chroma usa BGR
        agora = time.time()
        try:
            if self._ultimo.get(a.id) != bgr:
                self._pedir("PUT", f"{self._uri}/{a.id}", {"effect": "CHROMA_STATIC", "param": {"color": bgr}}, timeout=1.5)
                self._ultimo[a.id] = bgr
            if agora - self._batida > 8:  # sem batida por 15 s o Synapse fecha a sessão
                self._batida = agora
                self._pedir("PUT", f"{self._uri}/heartbeat", {}, timeout=1.5)
            return True
        except OSError:
            return False

    def liberar(self, a: Aparelho) -> None:
        self._ultimo.pop(a.id, None)

    def encerrar(self) -> None:
        uri, self._uri = self._uri, None
        self._ultimo.clear()
        if uri:
            try:
                self._pedir("DELETE", uri, timeout=1.5)
            except OSError:
                pass


# ------------------------------------------------------------------ Corsair iCUE (SDK 4)
DLL_CORSAIR = "iCUESDK.x64_2019.dll"
REPO_CORSAIR = "CorsairOfficial/cue-sdk"
TEXTO_CORSAIR = 128  # CORSAIR_STRING_SIZE_M
TIPOS_CORSAIR = {1: "Teclado", 2: "Mouse", 4: "Mousepad", 8: "Headset", 16: "Suporte de headset", 32: "Controlador de ventoinhas",
                 64: "Controlador de LED", 128: "Memória", 256: "Cooler", 512: "Placa-mãe", 1024: "Placa de vídeo", 2048: "Touchbar"}
ERROS_CORSAIR = {0: "ok", 1: "não conectado ao iCUE", 2: "outro programa controla o aparelho", 3: "versão do SDK incompatível com o iCUE",
                 4: "argumentos inválidos", 5: "operação inválida", 6: "aparelho não encontrado", 7: "o iCUE não permitiu (SDK desligado nas configurações?)"}


def pasta_corsair() -> Path:
    return config.pasta_dados() / "corsair"


def ler_aparelhos_corsair(buf: bytes, qtd: int) -> list[dict]:
    """CorsairDeviceInfo: tipo (int), id[128], serial[128], modelo[128], ledCount (int), channelCount (int)."""
    passo = 4 + TEXTO_CORSAIR * 3 + 8
    saida = []
    for i in range(max(0, qtd)):
        b = buf[i * passo:(i + 1) * passo]
        if len(b) < passo:
            break
        tipo = struct.unpack_from("<i", b, 0)[0]
        id_ = b[4:4 + TEXTO_CORSAIR].split(b"\0", 1)[0].decode("utf-8", "replace")
        modelo = b[4 + TEXTO_CORSAIR * 2:4 + TEXTO_CORSAIR * 3].split(b"\0", 1)[0].decode("utf-8", "replace")
        leds = struct.unpack_from("<i", b, 4 + TEXTO_CORSAIR * 3)[0]
        if not id_ or not all(32 <= ord(c) < 127 for c in id_):
            raise ValueError("estrutura do SDK diferente da esperada")
        saida.append({"tipo": tipo, "id": id_, "modelo": modelo or TIPOS_CORSAIR.get(tipo, "Aparelho Corsair"), "leds": leds})
    return saida


def ler_posicoes_corsair(buf: bytes, qtd: int) -> tuple[list[int], list[float]]:
    """CorsairLedPosition: id (uint32) + 4 de alinhamento + cx (double) + cy (double) = 24 bytes."""
    ids, xs = [], []
    for i in range(max(0, qtd)):
        lid, cx = struct.unpack_from("<I4xd", buf, i * 24)
        ids.append(lid)
        xs.append(cx if math.isfinite(cx) else 0.0)
    if xs:
        a, b = min(xs), max(xs)
        xs = [(x - a) / (b - a) if b - a > 1e-9 else (i / (len(xs) - 1) if len(xs) > 1 else 0.0) for i, x in enumerate(xs)]
    return ids, xs


class Corsair:
    nome = "Corsair iCUE"
    intervalo = 0.0

    def __init__(self):
        self._dll = None
        self._cb = None
        self.estado = 0

    @staticmethod
    def caminho_dll() -> Path:
        return pasta_corsair() / DLL_CORSAIR

    def _carregar(self) -> str | None:
        if self._dll:
            return None
        p = self.caminho_dll()
        if not windows.EH_WINDOWS:
            return "só funciona no Windows"
        if not p.exists():
            return "falta o SDK da Corsair (botão Baixar o SDK)"
        try:
            dll = ctypes.CDLL(str(p))
            for nome in ("CorsairConnect", "CorsairGetDevices", "CorsairGetLedPositions", "CorsairSetLedColors",
                         "CorsairRequestControl", "CorsairReleaseControl", "CorsairDisconnect"):
                getattr(dll, nome)
        except (OSError, AttributeError) as e:
            return f"a DLL do SDK não carregou ({e}); use o SDK 4.x"
        self._dll = dll
        return None

    def conectar(self) -> tuple[list[Aparelho], str]:
        erro = self._carregar()
        if erro:
            return [], erro
        d = self._dll
        if self.estado != 6:
            TIPO_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p)

            def mudou(_ctx, dados):
                if dados:
                    self.estado = ctypes.cast(dados, ctypes.POINTER(ctypes.c_int))[0]
            self._cb = TIPO_CB(mudou)
            e = d.CorsairConnect(self._cb, None)
            if e:
                return [], f"o iCUE recusou: {ERROS_CORSAIR.get(e, e)}"
            fim = time.time() + 5
            while self.estado != 6 and time.time() < fim:
                time.sleep(0.1)
            if self.estado != 6:
                return [], "o iCUE não respondeu; abra o iCUE e ligue o SDK em Configurações > Geral"
        filtro = ctypes.c_int(-1)
        maximo = 64
        buf = ctypes.create_string_buffer(maximo * (4 + TEXTO_CORSAIR * 3 + 8))
        qtd = ctypes.c_int(0)
        e = d.CorsairGetDevices(ctypes.byref(filtro), maximo, buf, ctypes.byref(qtd))
        if e:
            return [], f"não deu para listar: {ERROS_CORSAIR.get(e, e)}"
        try:
            infos = ler_aparelhos_corsair(buf.raw, min(qtd.value, maximo))
        except ValueError as ex:
            return [], str(ex)
        lista = []
        for info in infos:
            pos = ctypes.create_string_buffer(512 * 24)
            n = ctypes.c_int(0)
            if d.CorsairGetLedPositions(info["id"].encode(), 512, pos, ctypes.byref(n)) or n.value <= 0:
                continue
            ids, xs = ler_posicoes_corsair(pos.raw, min(n.value, 512))
            lista.append(Aparelho(self, info["id"], info["modelo"], f"Corsair iCUE · {TIPOS_CORSAIR.get(info['tipo'], 'aparelho')} · {len(ids)} LEDs",
                                  len(ids), xs, extra=ids))
        return lista, f"conectado ao iCUE · {len(lista)} aparelho(s)"

    def assumir(self, a: Aparelho) -> bool:
        return bool(self._dll) and self._dll.CorsairRequestControl(a.id.encode(), 1) == 0

    def enviar(self, a: Aparelho, cores) -> bool:
        if not self._dll:
            return False
        n = min(len(cores), len(a.extra))
        buf = (ctypes.c_uint8 * (8 * n))()
        for i in range(n):
            struct.pack_into("<IBBBB", buf, i * 8, a.extra[i], *cores[i], 255)
        return self._dll.CorsairSetLedColors(a.id.encode(), n, buf) == 0

    def liberar(self, a: Aparelho) -> None:
        if self._dll:
            try:
                self._dll.CorsairReleaseControl(a.id.encode())
            except OSError:
                pass

    def encerrar(self) -> None:
        pass  # a sessão fica aberta; liberar o controle já devolve as luzes ao iCUE


_PS_ASSINATURA = r"""
$s = Get-AuthenticodeSignature -LiteralPath '%s'
@{ status = [string]$s.Status; quem = [string]$s.SignerCertificate.Subject } | ConvertTo-Json -Compress
"""


def baixar_sdk_corsair(progresso=lambda *a: None) -> dict:
    """Baixa a última versão do SDK no GitHub oficial da Corsair e extrai só a DLL de 64 bits. Assinatura da Corsair obrigatória."""
    progresso(0.1, "Consultando a última versão do SDK da Corsair no GitHub...")
    cab = {"User-Agent": f"Destini-Monitor/{__version__}", "Accept": "application/vnd.github+json"}
    with urllib.request.urlopen(urllib.request.Request(f"https://api.github.com/repos/{REPO_CORSAIR}/releases/latest", headers=cab), timeout=20) as r:
        rel = json.loads(r.read().decode("utf-8"))
    urls = [a["browser_download_url"] for a in rel.get("assets") or [] if str(a.get("name", "")).lower().endswith(".zip")]
    if rel.get("zipball_url"):
        urls.append(rel["zipball_url"])
    urls = [u for u in urls if u.startswith("https://")]
    for url in urls:
        progresso(0.3, "Baixando o SDK...")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": cab["User-Agent"]}), timeout=120) as r:
            dados = r.read(200_000_000)
        try:
            z = zipfile.ZipFile(io.BytesIO(dados))
        except zipfile.BadZipFile:
            continue
        nome = next((n for n in z.namelist() if n.lower().endswith("/" + DLL_CORSAIR.lower()) or n.lower() == DLL_CORSAIR.lower()), None)
        if not nome:
            continue
        pasta_corsair().mkdir(parents=True, exist_ok=True)
        destino = Corsair.caminho_dll()
        temp = destino.with_suffix(".baixando")
        temp.write_bytes(z.read(nome))
        progresso(0.8, "Conferindo a assinatura digital...")
        if windows.EH_WINDOWS:
            r = windows.powershell_json(_PS_ASSINATURA % str(temp).replace("'", "''"), timeout=60)
            info = r[0] if r else {}
            if info.get("status") != "Valid" or "corsair" not in (info.get("quem") or "").lower():
                temp.unlink(missing_ok=True)
                raise OSError("a DLL baixada não está assinada pela Corsair; apaguei por segurança")
        os.replace(temp, destino)
        return {"ok": True, "mensagem": f"SDK da Corsair {rel.get('tag_name', '')} pronto. Toque em Conectar."}
    raise OSError("não achei a DLL de 64 bits na última versão do SDK da Corsair")


# ------------------------------------------------------------------ efeitos
def _hsv(h: float, s: float = 1.0, v: float = 1.0) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def cor_hex(texto: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(texto or "").strip())
    if not m:
        raise ValueError("cor inválida")
    v = int(m.group(1), 16)
    return v >> 16, (v >> 8) & 255, v & 255


def cores_do_efeito(efeito: str, cor, t: float, xs: list[float], velocidade: float, valor: float | None) -> list[tuple[int, int, int]]:
    """Cor de cada LED no instante t. `valor` é a temperatura (°C) ou a carga (%) já suavizada."""
    n = len(xs)
    if efeito == "apagar":
        return [(0, 0, 0)] * n
    if efeito == "respirar":
        k = 0.12 + 0.88 * (0.5 + 0.5 * math.sin(2 * math.pi * t * 0.25 * velocidade))
        return [tuple(round(c * k) for c in cor)] * n
    if efeito == "arco_iris":
        return [_hsv(x * 0.8 + t * 0.12 * velocidade) for x in xs]
    if efeito in ("temp_cpu", "temp_gpu"):
        if valor is None:
            return [(40, 40, 40)] * n
        frac = min(1.0, max(0.0, (valor - 35) / 50))  # 35 °C azul, 85 °C vermelho
        return [_hsv((240 - 240 * frac) / 360)] * n
    if efeito == "carga_cpu":
        frac = min(1.0, max(0.0, (valor or 0) / 100))  # 0 % verde, 100 % vermelho
        return [_hsv((120 - 120 * frac) / 360)] * n
    return [tuple(cor)] * n


class Luzes:
    """Liga as pontes, mantém a lista de aparelhos e roda o efeito escolhido numa thread (~30 quadros por segundo)."""

    def __init__(self):
        self._trava = threading.RLock()
        self._pontes = {"OpenRGB": OpenRGB(), "Corsair iCUE": Corsair(), "Razer Chroma": RazerChroma()}
        self._status: dict[str, str] = {}
        self._aparelhos: list[Aparelho] = []
        self._ativos: list[Aparelho] = []
        self._rodando = False
        self._thread: threading.Thread | None = None
        self._log: deque = deque(maxlen=60)
        self._valor: float | None = None
        self._ao_vivo = ""
        self.cfg = {"efeito": "fixa", "cor": "#ff6a00", "velocidade": "media", "desligados": []}
        try:
            salvo = json.loads(self._arquivo().read_text(encoding="utf-8"))
            self.cfg.update({k: v for k, v in salvo.items() if k in self.cfg})
        except (OSError, ValueError):
            pass

    @staticmethod
    def _arquivo() -> Path:
        return config.pasta_dados() / "rgb.json"

    def _registrar(self, texto: str) -> None:
        self._log.append(f"{datetime.now():%H:%M:%S}  {texto}")

    def conectar(self, progresso=lambda *a: None) -> dict:
        self.parar()
        with self._trava:
            self._aparelhos = []
            for i, (nome, ponte) in enumerate(self._pontes.items()):
                progresso(0.1 + 0.8 * i / len(self._pontes), f"Conectando ao {nome}...")
                try:
                    lista, status = ponte.conectar()
                except Exception as e:
                    log.exception("rgb: falha ao conectar %s", nome)
                    lista, status = [], f"falha: {e}"
                self._status[nome] = status
                self._registrar(f"{nome}: {status}")
                self._aparelhos += lista
        return self.estado()

    def estado(self) -> dict:
        desl = set(self.cfg.get("desligados") or [])
        return {"pontes": [{"nome": n, "status": self._status.get(n, "ainda não conectado"),
                            "conectado": any(a.ponte is p for a in self._aparelhos)} for n, p in self._pontes.items()],
                "aparelhos": [{**a.json(), "ligado": a.chave not in desl} for a in self._aparelhos],
                "rodando": self._rodando, "cfg": self.cfg, "efeitos": EFEITOS, "ao_vivo": self._ao_vivo,
                "log": list(self._log), "sdk_corsair": Corsair.caminho_dll().exists(), "openrgb": achar_openrgb() is not None}

    def configurar(self, efeito=None, cor=None, velocidade=None, desligados=None) -> dict:
        if efeito is not None:
            if efeito not in EFEITOS:
                raise ValueError("efeito desconhecido")
            self.cfg["efeito"] = efeito
        if cor is not None:
            cor_hex(cor)
            self.cfg["cor"] = "#" + str(cor).lstrip("#").lower()
        if velocidade is not None:
            if velocidade not in VELOCIDADES:
                raise ValueError("velocidade desconhecida")
            self.cfg["velocidade"] = velocidade
        if desligados is not None:
            self.cfg["desligados"] = [str(x) for x in desligados]
        try:
            self._arquivo().write_text(json.dumps(self.cfg, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        if self._rodando and desligados is not None:
            self.iniciar()  # muda a lista de aparelhos controlados
        return self.estado()

    def iniciar(self) -> dict:
        self.parar()
        desl = set(self.cfg.get("desligados") or [])
        with self._trava:
            alvos = [a for a in self._aparelhos if a.chave not in desl]
            if not alvos:
                return {"ok": False, "mensagem": "Nenhum aparelho para controlar: conecte e marque pelo menos um."}
            self._ativos = []
            for a in alvos:
                if a.ponte.assumir(a):
                    self._ativos.append(a)
                else:
                    self._registrar(f"{a.nome}: não deu para assumir o controle")
            if not self._ativos:
                return {"ok": False, "mensagem": "Nenhum aparelho aceitou o controle. Veja o diagnóstico."}
            self._rodando = True
            self._thread = threading.Thread(target=self._laco, name="rgb", daemon=True)
            self._thread.start()
        self._registrar(f"efeito {EFEITOS[self.cfg['efeito']]} em {len(self._ativos)} aparelho(s)")
        return {"ok": True, "mensagem": f"{EFEITOS[self.cfg['efeito']]} em {len(self._ativos)} aparelho(s)."}

    def parar(self) -> dict:
        with self._trava:
            if not self._rodando and not self._ativos:
                return {"ok": True, "mensagem": "Parado."}
            self._rodando = False
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(2)
        with self._trava:
            for a in self._ativos:
                try:
                    a.ponte.liberar(a)
                except Exception:
                    log.exception("rgb: falha ao liberar %s", a.nome)
            self._ativos = []
            for p in self._pontes.values():
                if isinstance(p, RazerChroma):
                    p.encerrar()  # fechar a sessão devolve as luzes ao Synapse na hora
        self._ao_vivo = ""
        self._registrar("parado: as luzes voltaram aos programas de cada marca")
        return {"ok": True, "mensagem": "Parado. As luzes voltaram ao programa de cada marca."}

    def _ler_valor(self, efeito: str) -> float | None:
        if efeito == "carga_cpu":
            return psutil.cpu_percent(interval=None)
        try:
            r = sensores.instancia().leitura(False)["resumo"]
        except Exception:
            return None
        return r.get("cpu_temp" if efeito == "temp_cpu" else "gpu_temp")

    def _laco(self) -> None:
        inicio, ultimo_valor = time.time(), 0.0
        enviado: dict[str, tuple[float, list]] = {}
        erros: dict[str, int] = {}
        while self._rodando:
            agora = time.time()
            cfg = dict(self.cfg)
            efeito = cfg["efeito"]
            cor = cor_hex(cfg["cor"])
            vel = VELOCIDADES.get(cfg["velocidade"], 1.0)
            if efeito in ("temp_cpu", "temp_gpu", "carga_cpu") and agora - ultimo_valor >= 1.0:
                ultimo_valor = agora
                v = self._ler_valor(efeito)
                if v is not None:
                    self._valor = v if self._valor is None else self._valor * 0.6 + v * 0.4
                quem = "Processador" if efeito != "temp_gpu" else "Placa de vídeo"
                self._ao_vivo = "" if self._valor is None else \
                    (f"{quem} {self._valor:.0f}% de carga" if efeito == "carga_cpu" else f"{quem} a {self._valor:.0f} °C").replace(".", ",")
            elif efeito not in ("temp_cpu", "temp_gpu", "carga_cpu"):
                self._valor, self._ao_vivo = None, ""
            for a in list(self._ativos):
                cores = cores_do_efeito(efeito, cor, agora - inicio, a.x, vel, self._valor)
                ult = enviado.get(a.chave)
                parado = ult is not None and ult[1] == cores
                # Cor parada: reenvia só de vez em quando (mantém a sessão); mudando: respeita o intervalo da ponte.
                if ult and ((parado and agora - ult[0] < 2.0) or agora - ult[0] < a.ponte.intervalo):
                    continue
                if a.ponte.enviar(a, cores):
                    enviado[a.chave] = (agora, cores)
                    erros.pop(a.chave, None)
                else:
                    erros[a.chave] = erros.get(a.chave, 0) + 1
                    if erros[a.chave] in (1, 30):
                        self._registrar(f"{a.nome}: falha ao enviar as cores")
                    enviado[a.chave] = (agora, [])
            time.sleep(1 / 30)

    @property
    def rodando(self) -> bool:
        return self._rodando


def achar_openrgb() -> Path | None:
    if not windows.EH_WINDOWS:
        return None
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", ""),
                 os.environ.get("LOCALAPPDATA", ""), str(Path.home() / "Downloads")):
        if not base:
            continue
        for p in (Path(base) / "OpenRGB" / "OpenRGB.exe", Path(base) / "Programs" / "OpenRGB" / "OpenRGB.exe"):
            if p.exists():
                return p
    return None


def abrir_openrgb() -> dict:
    """Abre o OpenRGB já com o servidor SDK ligado (o monitor conversa com ele por essa porta)."""
    p = achar_openrgb()
    if not p:
        return {"ok": False, "mensagem": "Não achei o OpenRGB instalado."}
    try:
        import subprocess
        subprocess.Popen([str(p), "--server", "--startminimized"], cwd=str(p.parent), close_fds=True)
    except OSError as e:
        return {"ok": False, "mensagem": f"Não deu para abrir o OpenRGB: {e}"}
    return {"ok": True, "mensagem": "OpenRGB aberto com o servidor ligado. Espere uns segundos e toque em Conectar."}


_instancia: Luzes | None = None


def encerrar() -> None:
    """Ao fechar o monitor: devolve as luzes aos programas de cada marca (só se algo foi ligado)."""
    if _instancia is not None and (_instancia.rodando or _instancia._ativos):
        _instancia.parar()


def instancia() -> Luzes:
    global _instancia
    if _instancia is None:
        _instancia = Luzes()
    return _instancia
