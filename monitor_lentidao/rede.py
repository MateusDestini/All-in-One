"""Diagnóstico de rede para lag: latência, jitter e perda até o roteador e a internet, DNS, velocidade e bufferbloat.

A velocidade usa o servidor público speed.cloudflare.com. Nada é gravado no disco.
Método e critérios adaptados do ResetExplode (NetworkTest.cs, licença MIT, © 2026 Gustavo).
"""

import logging
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import windows

log = logging.getLogger(__name__)

URL_DOWN = "https://speed.cloudflare.com/__down?bytes=25000000"
URL_UP = "https://speed.cloudflare.com/__up"


# ------------------------------------------------------------------ ping

class _Pinger:
    """Um ping por vez. No Windows usa a API ICMP (não precisa de administrador)."""

    def __init__(self):
        self.h = None
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class OPCOES(ctypes.Structure):
                _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                            ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]

            class RESPOSTA(ctypes.Structure):
                _fields_ = [("Address", ctypes.c_ulong), ("Status", ctypes.c_ulong), ("RoundTripTime", ctypes.c_ulong),
                            ("DataSize", ctypes.c_ushort), ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p),
                            ("Options", OPCOES)]
            ip = ctypes.WinDLL("iphlpapi")
            ip.IcmpCreateFile.restype = wintypes.HANDLE
            ip.IcmpSendEcho.argtypes = [wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p,
                                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD]
            ip.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
            self._ct, self._ip, self._resp = ctypes, ip, RESPOSTA
            self.h = ip.IcmpCreateFile()
            self._dados = ctypes.create_string_buffer(b"MonitorLentidao!" * 2)
            self._buf = ctypes.create_string_buffer(ctypes.sizeof(RESPOSTA) + 64)

    def ping(self, ip: str, timeout_ms: int = 1500) -> float | None:
        """Tempo de resposta em ms, ou None se não respondeu."""
        if self.h:
            destino = struct.unpack("<I", socket.inet_aton(ip))[0]
            n = self._ip.IcmpSendEcho(self.h, destino, self._dados, 32, None, self._buf, len(self._buf), timeout_ms)
            if not n:
                return None
            r = self._resp.from_buffer(self._buf)
            return max(1.0, float(r.RoundTripTime)) if r.Status == 0 else None
        try:
            saida = subprocess.run(["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip],
                                   capture_output=True, text=True, timeout=timeout_ms / 1000 + 2).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        m = re.search(r"time[=<]([\d.]+)", saida)
        return max(1.0, float(m.group(1))) if m else None

    def fechar(self):
        if self.h:
            self._ip.IcmpCloseHandle(self.h)


def _sequencia(ip: str, quantos: int, intervalo: float, parar: threading.Event | None = None):
    """Pinga em sequência; devolve (tempos, enviados)."""
    p, tempos, enviados = _Pinger(), [], 0
    try:
        i = 0
        while (quantos <= 0 or i < quantos) and not (parar and parar.is_set()):
            t = time.perf_counter()
            enviados += 1
            rtt = p.ping(ip)
            if rtt is not None:
                tempos.append(rtt)
            espera = intervalo - (time.perf_counter() - t)
            if espera > 0:
                if parar:
                    parar.wait(espera)
                else:
                    time.sleep(espera)
            i += 1
    finally:
        p.fechar()
    return tempos, enviados


def estatistica(nome: str, ip: str, tempos: list[float], enviados: int) -> dict:
    if not tempos:
        return {"nome": nome, "ip": ip, "respondeu": False, "perda": 100.0 if enviados else 0.0,
                "media": 0, "min": 0, "max": 0, "jitter": 0}
    jitter = sum(abs(b - a) for a, b in zip(tempos, tempos[1:])) / (len(tempos) - 1) if len(tempos) > 1 else 0
    return {"nome": nome, "ip": ip, "respondeu": True, "perda": round(100 * (enviados - len(tempos)) / enviados, 1),
            "media": round(sum(tempos) / len(tempos), 1), "min": min(tempos), "max": max(tempos), "jitter": round(jitter, 1)}


# ------------------------------------------------------------------ conexão ativa

_SCRIPT_CONEXAO = r"""
$r=Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | Sort-Object {$_.RouteMetric+$_.InterfaceMetric} | Select-Object -First 1
if($r){ $a=Get-NetAdapter -InterfaceIndex $r.ifIndex -ErrorAction SilentlyContinue
  $ip=(Get-NetIPAddress -InterfaceIndex $r.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -First 1).IPAddress
  $dns=(Get-DnsClientServerAddress -InterfaceIndex $r.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses | Select-Object -First 1
  [pscustomobject]@{Gateway=$r.NextHop;Nome="$($a.Name)";Descricao="$($a.InterfaceDescription)";Midia="$($a.PhysicalMediaType)";
    Velocidade="$($a.LinkSpeed)";Ip="$ip";Dns="$dns"} | ConvertTo-Json -Compress }
"""


def conexao() -> dict | None:
    if windows.EH_WINDOWS:
        dados = windows.powershell_json(_SCRIPT_CONEXAO, timeout=30)
        if not dados or not dados[0].get("Gateway") or dados[0]["Gateway"] == "0.0.0.0":
            return None
        d = dados[0]
        wifi = "802.11" in d.get("Midia", "") or "wi-fi" in d.get("Descricao", "").lower() or "wireless" in d.get("Descricao", "").lower()
        return {"gateway": d["Gateway"], "adaptador": d.get("Nome") or d.get("Descricao", ""), "wifi": wifi,
                "velocidade": d.get("Velocidade", ""), "ip": d.get("Ip", ""), "dns": d.get("Dns", "")}
    try:
        rota = subprocess.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5).stdout
    except OSError:
        return None
    m = re.search(r"via (\S+) dev (\S+)", rota)
    if not m:
        return None
    return {"gateway": m.group(1), "adaptador": m.group(2), "wifi": m.group(2).startswith("wl"),
            "velocidade": "", "ip": "", "dns": ""}


# ------------------------------------------------------------------ velocidade

def _download(segundos: float) -> float | None:
    total, trava, fim = [0], threading.Lock(), time.perf_counter() + segundos

    def baixar():
        try:
            while time.perf_counter() < fim:
                req = urllib.request.Request(URL_DOWN, headers={"User-Agent": "MonitorLentidao"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    while time.perf_counter() < fim and (bloco := r.read(65536)):
                        with trava:
                            total[0] += len(bloco)
        except OSError:
            log.info("falha no download de teste", exc_info=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda _: baixar(), range(4)))
    seg = time.perf_counter() - t0
    return total[0] * 8 / 1e6 / seg if total[0] else None


def _upload(segundos: float) -> float | None:
    total, trava, fim = [0], threading.Lock(), time.perf_counter() + segundos
    dados = os.urandom(1_000_000)

    def enviar():
        try:
            while time.perf_counter() < fim:
                req = urllib.request.Request(URL_UP, data=dados, method="POST",
                                             headers={"User-Agent": "MonitorLentidao", "Content-Type": "application/octet-stream"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    r.read()
                with trava:
                    total[0] += len(dados)
        except OSError:
            log.info("falha no upload de teste", exc_info=True)
    t0 = time.perf_counter()
    with ThreadPoolExecutor(3) as ex:
        list(ex.map(lambda _: enviar(), range(3)))
    seg = time.perf_counter() - t0
    return total[0] * 8 / 1e6 / seg if total[0] else None


# ------------------------------------------------------------------ diagnóstico

def testar(progresso) -> dict:
    progresso(0.02, "Procurando a conexão ativa...")
    con = conexao()
    quando = datetime.now().strftime("%d/%m/%Y %H:%M")
    if not con:
        return {"quando": quando, "nivel": "critico", "rotulo": "Sem rede", "conexao": None, "pings": [],
                "motivos": ["O Windows não encontrou uma conexão ativa com acesso ao roteador. "
                            "Verifique o cabo, o Wi-Fi ou o adaptador de rede."]}

    progresso(0.08, "Medindo latência e perda de pacotes (≈ 7 s)...")
    alvos = [("Roteador", con["gateway"]), ("Internet · Cloudflare", "1.1.1.1"), ("Internet · Google", "8.8.8.8")]
    with ThreadPoolExecutor(3) as ex:
        res = list(ex.map(lambda a: _sequencia(a[1], 30, 0.2), alvos))
    pings = [estatistica(n, ip, *r) for (n, ip), r in zip(alvos, res)]

    progresso(0.30, "Testando o DNS...")
    dns_ms = None
    try:
        t = time.perf_counter()
        socket.getaddrinfo(f"teste-{int(time.time())}.cloudflare.com", 443)
    except OSError:
        pass  # nome inexistente responde rápido do mesmo jeito: o que importa é o tempo
    finally:
        dns_ms = (time.perf_counter() - t) * 1000

    progresso(0.36, "Medindo a velocidade de download (≈ 8 s)...")
    parar = threading.Event()
    carga = {}
    th = threading.Thread(target=lambda: carga.update(r=_sequencia("1.1.1.1", 0, 0.2, parar)), daemon=True)
    th.start()
    down = _download(8)
    parar.set()
    th.join(3)
    tempos_carga = carga.get("r", ([], 0))[0]
    sob_carga = sum(tempos_carga) / len(tempos_carga) if len(tempos_carga) >= 5 else None

    progresso(0.66, "Medindo a velocidade de upload (≈ 6 s)...")
    up = _upload(6)
    progresso(0.97, "Analisando...")
    r = analisar(con, pings, dns_ms, down, up, sob_carga)
    r["quando"] = quando
    progresso(1.0, "Concluído")
    return r


def analisar(con, pings, dns_ms, down, up, sob_carga) -> dict:
    roteador, internet = pings[0], [p for p in pings[1:] if p["respondeu"]]
    media = sum(p["media"] for p in internet) / len(internet) if internet else 0
    perda = sum(p["perda"] for p in pings[1:]) / max(1, len(pings) - 1)
    jitter = max((p["jitter"] for p in internet), default=0)
    motivos, ruim = [], False

    lan_ruim = roteador["respondeu"] and (roteador["perda"] > 2 or roteador["media"] > 30 or roteador["jitter"] > 15)
    if lan_ruim:
        motivos.append(f"A rede local está instável (roteador: {roteador['media']:.0f} ms, perda {roteador['perda']:.0f}%, "
                       f"jitter {roteador['jitter']:.0f} ms). " +
                       ("No Wi-Fi, aproxime-se do roteador ou use cabo." if con["wifi"] else "Verifique o cabo e o roteador."))
    if not internet:
        ruim = True
        motivos.append("Nenhum servidor da internet respondeu: sem acesso à internet ou ping bloqueado pela rede.")
    else:
        if perda > 5 or media > 150:
            ruim = True
        if perda > 1:
            motivos.append(f"Há perda de pacotes até a internet ({perda:.0f}%). " +
                           ("Provavelmente vem da rede local." if lan_ruim else
                            "Como o roteador está bem, o problema deve estar na linha ou no provedor."))
        if media > 80:
            motivos.append(f"Latência alta até a internet ({media:.0f} ms)." +
                           ("" if lan_ruim else " O roteador responde bem, então o atraso vem do provedor."))
        if jitter > 20:
            motivos.append(f"O jitter está alto ({jitter:.0f} ms): a latência oscila, o que causa engasgos em jogos e chamadas.")
    if sob_carga and internet and sob_carga - min(p["media"] for p in internet) > 100:
        motivos.append(f"A latência sobe para {sob_carga:.0f} ms quando a internet está sendo usada (bufferbloat). "
                       "Ative SQM/QoS no roteador ou limite a velocidade a ~90%.")
    if dns_ms and dns_ms > 150:
        motivos.append(f"O DNS demorou {dns_ms:.0f} ms. Trocar para 1.1.1.1 ou 8.8.8.8 pode acelerar a abertura de sites.")
    if down is not None and down < 10:
        motivos.append(f"Download baixo ({down:.1f} Mbps) para streaming, jogos e atualizações.")
    if down is None:
        motivos.append("Não foi possível medir a velocidade (speed.cloudflare.com inacessível ou bloqueado).")
    aviso_real = [m for m in motivos if not m.startswith("Não foi possível medir")]
    if not roteador["respondeu"]:
        motivos.append("O roteador não responde a ping (alguns modelos bloqueiam). Isso por si só não é problema.")

    nivel = "critico" if ruim else "aviso" if aviso_real else "ok"
    if not motivos:
        motivos.append("Latência baixa, sem perda de pacotes e sem sinais de bufferbloat. "
                       "Se algo ainda parece lento, a causa provavelmente não é a internet.")
    return {"nivel": nivel, "rotulo": {"critico": "Ruim", "aviso": "Atenção", "ok": "Estável"}[nivel],
            "conexao": con, "pings": pings, "dns_ms": round(dns_ms) if dns_ms else None,
            "download": round(down, 1) if down else None, "upload": round(up, 1) if up else None,
            "sob_carga": round(sob_carga) if sob_carga else None, "motivos": motivos}
