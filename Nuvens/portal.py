#!/usr/bin/env python3
"""
portal.py — portal web para publicar levantamentos a partir de um E57.

    python portal.py --raiz C:\\xampp\\htdocs\\nuvens
                     --entrada C:\\e57
                     --modelo C:\\xampp\\htdocs\\nuvens\\_modelo

Sobe um servidor em http://127.0.0.1:8800 com um formulario: nome da planta,
escolha do E57 (da pasta de entrada ou por upload), opcoes de conversao.
Ao iniciar, roda o publicar.py em segundo plano e mostra barra de progresso
com o historico ao vivo. No fim, entrega o link do visualizador.

Requisitos: apenas a biblioteca padrao. O publicar.py, o extrair_direto.py e
o index.html precisam estar na mesma pasta deste script.
"""

import argparse
import html
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

AQUI = os.path.dirname(os.path.abspath(__file__))
TAREFAS = {}
TRAVA = threading.Lock()

# publicar.py imprime "[n/m] Titulo"; e dai que sai a barra de progresso.
PADRAO_ETAPA = re.compile(r"^\[(\d+)/(\d+)\]\s*(.+)$")


# ------------------------------------------------------------------ utilidades

def apelidar(texto):
    """Transforma 'Planta Sul nº 2' em 'planta-sul-n-2'."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = texto.encode("ascii", "ignore").decode("ascii")
    texto = texto.lower().strip()
    texto = re.sub(r"[^a-z0-9]+", "-", texto)
    texto = re.sub(r"-{2,}", "-", texto).strip("-")
    return texto[:60]


def humano(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.1f} {u}"
        n /= 1024


def listar_e57(pasta):
    if not pasta or not os.path.isdir(pasta):
        return []
    achados = []
    for nome in sorted(os.listdir(pasta)):
        if nome.lower().endswith(".e57"):
            caminho = os.path.join(pasta, nome)
            try:
                achados.append({"nome": nome, "tamanho": os.path.getsize(caminho)})
            except OSError:
                pass
    return achados


# --------------------------------------------------------------------- tarefas

class Tarefa:
    def __init__(self, apelido, e57, opcoes, cfg, nome_exibido=None):
        self.id = uuid.uuid4().hex[:12]
        self.apelido = apelido
        self.nome_exibido = (nome_exibido or apelido).strip()
        self.e57 = e57
        self.opcoes = opcoes
        self.cfg = cfg
        self.linhas = []
        self.estado = "na fila"      # na fila | rodando | concluida | falhou
        self.etapa = ""
        self.progresso = 0
        self.link = None
        self.inicio = time.time()
        self.fim = None
        self.trava = threading.Lock()

    def anotar(self, texto, tipo="info"):
        with self.trava:
            self.linhas.append({"t": time.strftime("%H:%M:%S"),
                                "texto": texto, "tipo": tipo})

    def instantaneo(self, desde=0):
        with self.trava:
            return {
                "id": self.id,
                "estado": self.estado,
                "etapa": self.etapa,
                "progresso": self.progresso,
                "link": self.link,
                "total": len(self.linhas),
                "linhas": self.linhas[desde:],
                "segundos": round((self.fim or time.time()) - self.inicio),
            }


def preparar_pasta(tarefa, destino):
    """Copia vendor/ e potree/ do modelo, sem sobrescrever o que ja existe."""
    modelo = tarefa.cfg["modelo"]
    if not modelo or not os.path.isdir(modelo):
        tarefa.anotar("Nenhuma pasta modelo configurada — vendor/ e potree/ "
                      "terao de ser copiados na mao.", "aviso")
        return

    for pasta in ("vendor", "potree"):
        origem = os.path.join(modelo, pasta)
        alvo = os.path.join(destino, pasta)
        if not os.path.isdir(origem):
            tarefa.anotar(f"modelo/{pasta} nao existe — pulando", "aviso")
            continue
        if os.path.isdir(alvo):
            tarefa.anotar(f"{pasta}/ ja existe no destino — mantido")
            continue
        tarefa.anotar(f"copiando {pasta}/ do modelo...")
        shutil.copytree(origem, alvo)
        tarefa.anotar(f"{pasta}/ copiado ({humano(soma_pasta(alvo))})", "ok")


def soma_pasta(caminho):
    total = 0
    for raiz, _, arquivos in os.walk(caminho):
        for a in arquivos:
            try:
                total += os.path.getsize(os.path.join(raiz, a))
            except OSError:
                pass
    return total


def registrar_planta(tarefa, destino):
    """Mantem <raiz>/plantas.json, que alimenta a pagina inicial."""
    indice = os.path.join(tarefa.cfg["raiz"], "plantas.json")
    plantas = []
    if os.path.isfile(indice):
        try:
            with open(indice, encoding="utf-8") as fh:
                plantas = json.load(fh).get("plantas", [])
        except ValueError:
            plantas = []

    estacoes = 0
    manifesto = os.path.join(destino, "estacoes.json")
    if os.path.isfile(manifesto):
        try:
            with open(manifesto, encoding="utf-8") as fh:
                estacoes = len(json.load(fh).get("estacoes", []))
        except ValueError:
            pass

    registro = {
        "apelido": tarefa.apelido,
        "nome": tarefa.nome_exibido,
        "quando": time.strftime("%Y-%m-%d %H:%M"),
        "estacoes": estacoes,
        "nuvem": os.path.isfile(os.path.join(destino, "nuvem", "metadata.json")),
        "tamanho": soma_pasta(destino),
    }
    plantas = [p for p in plantas if p.get("apelido") != tarefa.apelido]
    plantas.append(registro)
    plantas.sort(key=lambda p: p.get("quando", ""), reverse=True)

    with open(indice, "w", encoding="utf-8") as fh:
        json.dump({"atualizado": registro["quando"], "plantas": plantas},
                  fh, indent=1, ensure_ascii=False)
    tarefa.anotar(f"indice atualizado: {len(plantas)} planta(s)", "ok")


def executar(tarefa):
    cfg = tarefa.cfg
    destino = os.path.join(cfg["raiz"], tarefa.apelido)

    try:
        tarefa.estado = "rodando"
        tarefa.etapa = "Preparando a pasta"
        tarefa.anotar(f"destino: {destino}")
        os.makedirs(destino, exist_ok=True)
        preparar_pasta(tarefa, destino)

        comando = [sys.executable, "-u", os.path.join(AQUI, "publicar.py"),
                   tarefa.e57, "-o", destino]
        if tarefa.opcoes.get("sem_nuvem"):
            comando.append("--sem-nuvem")
        if tarefa.opcoes.get("voxel"):
            comando += ["--voxel", str(tarefa.opcoes["voxel"])]
        if tarefa.opcoes.get("precisao"):
            comando += ["--precisao", str(tarefa.opcoes["precisao"])]
        if cfg.get("pdal"):
            comando += ["--pdal", cfg["pdal"]]
        if cfg.get("potree_converter"):
            comando += ["--potree-converter", cfg["potree_converter"]]

        tarefa.etapa = "Executando o publicador"
        tarefa.anotar("$ " + " ".join(comando))

        processo = subprocess.Popen(
            comando, cwd=destino, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
            encoding="utf-8", errors="replace")

        for linha in processo.stdout:
            linha = linha.rstrip("\n")
            if not linha.strip() or set(linha.strip()) <= {"-", "="}:
                continue
            m = PADRAO_ETAPA.match(linha.strip())
            if m:
                atual, total, titulo = int(m.group(1)), int(m.group(2)), m.group(3)
                tarefa.etapa = titulo
                # 10% reservados para o preparo da pasta
                tarefa.progresso = 10 + int(85 * (atual - 1) / max(total, 1))
                tarefa.anotar(titulo, "etapa")
            else:
                tipo = "erro" if linha.strip().startswith("!") else "info"
                tarefa.anotar(linha, tipo)

        codigo = processo.wait()

        if codigo == 0:
            tarefa.progresso = 100
            tarefa.estado = "concluida"
            tarefa.etapa = "Concluido"
            tarefa.link = cfg["url_base"].rstrip("/") + "/" + tarefa.apelido + "/index.html"
            try:
                registrar_planta(tarefa, destino)
            except OSError as erro:
                tarefa.anotar(f"nao foi possivel atualizar plantas.json: {erro}", "aviso")
            tarefa.anotar("Publicacao concluida.", "ok")
        else:
            tarefa.estado = "falhou"
            tarefa.etapa = "Interrompido"
            tarefa.anotar(f"O publicador terminou com codigo {codigo}.", "erro")

    except Exception as erro:                                   # noqa: BLE001
        tarefa.estado = "falhou"
        tarefa.etapa = "Erro"
        tarefa.anotar(f"{type(erro).__name__}: {erro}", "erro")
    finally:
        tarefa.fim = time.time()


# ------------------------------------------------------------------- servidor

class Portal(BaseHTTPRequestHandler):
    cfg = {}

    def log_message(self, formato, *args):
        pass                       # o console fica limpo para o log das tarefas

    # -------------------------------------------------------------- auxiliares

    def responder(self, corpo, tipo="text/html; charset=utf-8", codigo=200):
        if isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def responder_json(self, dados, codigo=200):
        self.responder(json.dumps(dados, ensure_ascii=False),
                       "application/json; charset=utf-8", codigo)

    TIPOS = {".ico": "image/x-icon", ".png": "image/png", ".jpg": "image/jpeg",
             ".jpeg": "image/jpeg", ".svg": "image/svg+xml", ".webp": "image/webp"}

    def servir_asset(self, nome):
        """Serve favicon e logotipo da pasta assets/, ao lado do portal.py."""
        nome = os.path.basename(nome)
        extensao = os.path.splitext(nome)[1].lower()
        if extensao not in self.TIPOS:
            return self.responder("", "text/plain", 404)
        caminho = os.path.join(AQUI, "assets", nome)
        if not os.path.isfile(caminho):
            return self.responder("", "text/plain", 404)
        with open(caminho, "rb") as fh:
            dados = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", self.TIPOS[extensao])
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(dados)

    # ------------------------------------------------------------------- rotas

    def do_GET(self):
        rota = urlparse(self.path)
        partes = [p for p in rota.path.split("/") if p]

        if not partes:
            return self.responder(pagina_inicial(self.cfg))

        if partes[0] == "tarefa" and len(partes) == 2:
            return self.responder(pagina_tarefa(partes[1]))

        if partes[0] == "estado" and len(partes) == 2:
            tarefa = TAREFAS.get(partes[1])
            if not tarefa:
                return self.responder_json({"erro": "tarefa desconhecida"}, 404)
            desde = int(parse_qs(rota.query).get("desde", ["0"])[0])
            return self.responder_json(tarefa.instantaneo(desde))

        if partes[0] == "arquivos":
            return self.responder_json({"arquivos": listar_e57(self.cfg["entrada"])})

        if partes[0] == "assets" and len(partes) == 2:
            return self.servir_asset(partes[1])

        self.responder("nao encontrado", "text/plain; charset=utf-8", 404)

    def do_PUT(self):
        """Recebe upload cru, gravando em blocos — nao carrega nada na memoria."""
        rota = urlparse(self.path)
        if not rota.path.startswith("/enviar"):
            return self.responder("nao encontrado", "text/plain; charset=utf-8", 404)

        parametros = parse_qs(rota.query)
        nome = os.path.basename(parametros.get("nome", ["upload.e57"])[0])
        if not nome.lower().endswith(".e57"):
            return self.responder_json({"erro": "so arquivos .e57"}, 400)

        pasta = self.cfg["entrada"]
        os.makedirs(pasta, exist_ok=True)
        destino = os.path.join(pasta, nome)

        restante = int(self.headers.get("Content-Length", "0"))
        try:
            with open(destino, "wb") as saida:
                while restante > 0:
                    bloco = self.rfile.read(min(4 * 1024 * 1024, restante))
                    if not bloco:
                        break
                    saida.write(bloco)
                    restante -= len(bloco)
        except OSError as erro:
            return self.responder_json({"erro": str(erro)}, 500)

        if restante > 0:
            os.remove(destino)
            return self.responder_json({"erro": "envio interrompido"}, 400)

        self.responder_json({"ok": True, "nome": nome,
                             "tamanho": os.path.getsize(destino)})

    def do_POST(self):
        if urlparse(self.path).path != "/iniciar":
            return self.responder("nao encontrado", "text/plain; charset=utf-8", 404)

        tamanho = int(self.headers.get("Content-Length", "0"))
        try:
            dados = json.loads(self.rfile.read(tamanho).decode("utf-8"))
        except ValueError:
            return self.responder_json({"erro": "json invalido"}, 400)

        apelido = apelidar(dados.get("planta", ""))
        if not apelido:
            return self.responder_json({"erro": "informe o nome da planta"}, 400)

        arquivo = os.path.basename(dados.get("arquivo", ""))
        caminho = os.path.join(self.cfg["entrada"], arquivo)
        if not arquivo or not os.path.isfile(caminho):
            return self.responder_json({"erro": "arquivo E57 nao encontrado"}, 400)

        destino = os.path.join(self.cfg["raiz"], apelido)
        if os.path.exists(destino) and not dados.get("sobrescrever"):
            return self.responder_json(
                {"erro": "ja existe", "apelido": apelido,
                 "detalhe": f"A pasta {apelido} ja existe."}, 409)

        opcoes = {
            "sem_nuvem": bool(dados.get("sem_nuvem")),
            "voxel": dados.get("voxel") or None,
            "precisao": dados.get("precisao") or None,
        }
        tarefa = Tarefa(apelido, caminho, opcoes, self.cfg,
                        nome_exibido=dados.get("planta", ""))
        with TRAVA:
            TAREFAS[tarefa.id] = tarefa
        threading.Thread(target=executar, args=(tarefa,), daemon=True).start()

        self.responder_json({"id": tarefa.id, "apelido": apelido})


# ------------------------------------------------------------------- interface

ESTILO = """
@import url('https://fonts.googleapis.com/css2?family=Archivo:wght@400;600;800&family=IBM+Plex+Mono:wght@400;500&display=swap');

:root{
  --paper:#E4EAF1; --paper-2:#D3DCE7; --face:#F8FAFC;
  --ink:#0C2A4D; --ink-2:#3F5C7E;
  --blue:#1B6FC0; --cyan:#43B0F1;
  --rule:rgba(12,42,77,.14); --grid:rgba(27,111,192,.16);
  --erro:#B3261E; --ok:#1E7A46; --aviso:#9A6700;
  --display:"Archivo",system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  min-height:100dvh; background:var(--paper); color:var(--ink);
  font-family:var(--display);
  display:flex; justify-content:center;
  padding:clamp(26px,5vh,56px) clamp(16px,4vw,40px) 60px;
  /* malha de prancheta ao fundo */
  background-image:
    linear-gradient(var(--grid) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid) 1px, transparent 1px);
  background-size:34px 34px;
}
.folha{width:100%;max-width:680px}

/* ---------- cabecalho ---------- */
.marca{display:flex;align-items:baseline;gap:11px;margin-bottom:5px}
.marca .nome{
  font-weight:800;font-size:19px;letter-spacing:.30em;text-transform:uppercase;
  color:var(--ink);
}
.marca .lema{
  font-family:var(--mono);font-size:10px;letter-spacing:.22em;
  text-transform:uppercase;color:var(--ink-2);
}
h1{font-size:clamp(21px,3.4vw,27px);font-weight:600;letter-spacing:-.015em;margin:14px 0 4px}
.eyebrow{
  font-family:var(--mono);font-size:11px;letter-spacing:.30em;
  text-transform:uppercase;color:var(--ink-2);
  display:flex;align-items:center;gap:11px;margin-bottom:26px;
}
.eyebrow::after{content:"";flex:1;height:1px;background:var(--rule)}

/* linha de cota, como separador de secao */
.cota{
  display:flex;align-items:center;gap:10px;margin:22px 0 11px;
  font-family:var(--mono);font-size:10px;letter-spacing:.24em;
  text-transform:uppercase;color:var(--ink-2);
}
.cota::before{content:"";width:9px;height:7px;flex:0 0 auto;
  border-left:1px solid var(--ink-2);border-top:1px solid var(--ink-2);
  border-bottom:1px solid var(--ink-2)}
.cota::after{content:"";flex:1;height:1px;background:var(--rule)}

/* ---------- folhas ---------- */
.bloco{
  background:var(--face);border:1px solid rgba(12,42,77,.16);border-radius:4px;
  padding:18px 20px;margin-bottom:13px;
  box-shadow:0 1px 0 rgba(255,255,255,.9) inset, 0 1px 2px rgba(12,42,77,.05);
}
label{
  display:block;font-family:var(--mono);font-size:10px;letter-spacing:.2em;
  text-transform:uppercase;color:var(--ink-2);margin-bottom:7px;
}
input[type=text],select{
  width:100%;background:#fff;color:var(--ink);
  border:1px solid var(--rule);border-radius:3px;padding:10px 12px;
  font-family:var(--display);font-size:14.5px;
}
input[type=text]:focus,select:focus{outline:2px solid var(--cyan);outline-offset:1px;border-color:var(--blue)}
input[type=file]{font-family:var(--mono);font-size:12px;color:var(--ink-2)}
.apelido{font-family:var(--mono);font-size:11px;color:var(--ink-2);margin-top:7px}
.apelido b{color:var(--blue)}

.opcao{display:flex;align-items:flex-start;gap:10px;font-size:14px;margin-top:13px}
.opcao:first-of-type{margin-top:0}
.opcao input[type=checkbox]{accent-color:var(--blue);margin-top:3px;width:15px;height:15px}
.opcao small{display:block;color:var(--ink-2);font-size:12px;margin-top:3px;line-height:1.5}
.medida{width:74px;display:inline-block;padding:3px 7px;
  font-family:var(--mono);font-size:12.5px;text-align:center}

/* ---------- abas ---------- */
.abas{display:flex;margin-bottom:13px;gap:-1px}
.abas button{
  flex:1;background:var(--paper-2);border:1px solid rgba(12,42,77,.16);
  color:var(--ink-2);font-family:var(--mono);font-size:10.5px;padding:10px;
  cursor:pointer;letter-spacing:.18em;text-transform:uppercase;
}
.abas button:first-child{border-radius:4px 0 0 4px}
.abas button:last-child{border-radius:0 4px 4px 0;border-left:0}
.abas button[aria-selected=true]{background:var(--face);color:var(--blue);
  border-color:var(--blue);box-shadow:inset 0 -2px 0 var(--blue)}

/* ---------- acao ---------- */
button.principal{
  width:100%;background:var(--ink);color:#F2F7FC;border:0;border-radius:3px;
  padding:14px;font-family:var(--mono);font-size:11.5px;letter-spacing:.24em;
  text-transform:uppercase;cursor:pointer;font-weight:500;
}
button.principal:hover:not(:disabled){background:var(--blue)}
button.principal:disabled{background:var(--paper-2);color:var(--ink-2);cursor:not-allowed}

.recado{
  font-family:var(--mono);font-size:12px;line-height:1.6;padding:10px 12px;margin-top:12px;
  border-left:2px solid var(--blue);background:rgba(27,111,192,.07);color:var(--ink);
  border-radius:0 3px 3px 0;
}
.recado.ruim{border-color:var(--erro);background:rgba(179,38,30,.07)}
.recado button{background:none;border:0;color:var(--blue);cursor:pointer;
  font-family:var(--mono);font-size:12px;text-decoration:underline;padding:0}

/* ---------- progresso ---------- */
.etapa{font-family:var(--mono);font-size:12.5px;display:flex;
  justify-content:space-between;align-items:baseline;color:var(--ink)}
.etapa span{color:var(--ink-2);font-size:11.5px}
.barra{position:relative;height:6px;background:var(--paper-2);
  border:1px solid var(--rule);border-radius:2px;overflow:hidden;margin:13px 0 0}
.barra i{display:block;height:100%;width:0;
  background:linear-gradient(90deg,var(--blue),var(--cyan));transition:width .45s ease}
.reguas{display:flex;justify-content:space-between;margin-top:5px;
  font-family:var(--mono);font-size:9px;letter-spacing:.14em;color:var(--ink-2)}

.registro{
  background:#fff;border:1px solid var(--rule);border-radius:3px;
  height:320px;overflow-y:auto;padding:11px 13px;margin-top:14px;
  font-family:var(--mono);font-size:11.5px;line-height:1.7;
}
.registro div{display:flex;gap:10px;white-space:pre-wrap;word-break:break-word}
.registro .hora{color:#A7B5C4;flex:0 0 auto}
.registro .etapa-linha{color:var(--blue);font-weight:500}
.registro .erro{color:var(--erro)}
.registro .ok{color:var(--ok)}
.registro .aviso{color:var(--aviso)}

a.saida{
  display:block;text-align:center;background:var(--blue);color:#fff;
  padding:14px;font-family:var(--mono);font-size:11.5px;letter-spacing:.24em;
  text-transform:uppercase;text-decoration:none;margin-top:13px;border-radius:3px;
}
a.saida:hover{background:var(--ink)}
a.voltar{color:var(--ink-2);font-family:var(--mono);font-size:11px;
  text-decoration:none;letter-spacing:.1em}
a.voltar:hover{color:var(--blue)}
progress{width:100%;height:6px;accent-color:var(--blue);margin-top:9px}
footer{margin-top:26px;font-family:var(--mono);font-size:10px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--ink-2);text-align:center}
"""

CABECA = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="/assets/favicon.ico" type="image/x-icon">"""

MARCA = """<div class="marca">
  <span class="nome">Adimensa</span>
  <span class="lema">Access the Immense</span>
</div>"""

RODAPE = """<footer>Adimensa &middot; publicacao de levantamentos</footer>"""


def pagina_inicial(cfg):
    return f"""<!DOCTYPE html><html lang="pt-BR"><head>{CABECA}
<title>Adimensa | Publicar levantamento</title>
<style>{ESTILO}</style></head><body>
<div class="folha">
{MARCA}
<h1>Publicar levantamento</h1>
<p class="eyebrow">E57 &rarr; bolhas &rarr; nuvem &rarr; visualizador</p>

<p class="cota">Identificação</p>
<div class="bloco">
  <label for="planta">Nome da planta</label>
  <input type="text" id="planta" placeholder="ex.: Planta Sul — Unidade 2" autofocus>
  <div class="apelido">pasta: <b id="apelido">—</b></div>
</div>

<p class="cota">Origem</p>
<div class="abas">
  <button id="aba-lista" aria-selected="true">arquivo na máquina</button>
  <button id="aba-envio" aria-selected="false">enviar arquivo</button>
</div>

<div class="bloco" id="painel-lista">
  <label for="arquivo">Arquivo E57</label>
  <select id="arquivo"><option value="">carregando…</option></select>
  <div class="apelido">pasta de entrada: {html.escape(cfg['entrada'])}</div>
</div>

<div class="bloco" id="painel-envio" hidden>
  <label for="upload">Enviar um .e57</label>
  <input type="file" id="upload" accept=".e57">
  <progress id="progresso-envio" value="0" max="100" hidden></progress>
  <div class="apelido" id="estado-envio"></div>
  <div class="recado">Arquivos grandes levam muito tempo pelo navegador e não
  têm retomada. Acima de uns poucos GB, copie o E57 direto para a pasta de
  entrada e use a outra aba.</div>
</div>

<p class="cota">Conversão</p>
<div class="bloco">
  <div class="opcao"><input type="checkbox" id="sem-nuvem">
    <div>Somente as bolhas
      <small>Pula PDAL e PotreeConverter. Publica em minutos.</small></div></div>
  <div class="opcao"><input type="checkbox" id="usar-voxel" checked>
    <div>Subamostrar em
      <input type="text" id="voxel" value="0.01" class="medida"> m
      <small>Reduz muito o tempo e o disco, com perda visual pequena.</small></div></div>
  <div class="opcao"><input type="checkbox" id="usar-precisao" checked>
    <div>Precisão de coordenada
      <input type="text" id="precisao" value="0.001" class="medida"> m
      <small>O padrão do PDAL arredonda para 1 cm. Não deixe assim.</small></div></div>
</div>

<button class="principal" id="publicar" disabled>Publicar</button>
<div id="recado"></div>
{RODAPE}
</div>

<script>
const $ = (id) => document.getElementById(id);
let enviado = null;

function apelidar(t) {{
  return (t || "").normalize("NFKD").replace(/[\\u0300-\\u036f]/g, "")
    .toLowerCase().trim().replace(/[^a-z0-9]+/g, "-")
    .replace(/-{{2,}}/g, "-").replace(/^-|-$/g, "").slice(0, 60);
}}

function revisar() {{
  const ok = apelidar($("planta").value) && (
    ($("painel-envio").hidden && $("arquivo").value) ||
    (!$("painel-envio").hidden && enviado));
  $("publicar").disabled = !ok;
}}

$("planta").addEventListener("input", () => {{
  const a = apelidar($("planta").value);
  $("apelido").textContent = a || "—";
  revisar();
}});

function trocarAba(lista) {{
  $("aba-lista").setAttribute("aria-selected", String(lista));
  $("aba-envio").setAttribute("aria-selected", String(!lista));
  $("painel-lista").hidden = !lista;
  $("painel-envio").hidden = lista;
  revisar();
}}
$("aba-lista").addEventListener("click", () => trocarAba(true));
$("aba-envio").addEventListener("click", () => trocarAba(false));
$("arquivo").addEventListener("change", revisar);

function humano(n) {{
  const u = ["B","KB","MB","GB","TB"];
  let i = 0; while (n >= 1024 && i < 4) {{ n /= 1024; i++; }}
  return n.toFixed(1) + " " + u[i];
}}

fetch("/arquivos").then((r) => r.json()).then((d) => {{
  const sel = $("arquivo");
  sel.innerHTML = "";
  if (!d.arquivos.length) {{
    sel.innerHTML = "<option value=''>nenhum .e57 na pasta de entrada</option>";
    return;
  }}
  sel.appendChild(new Option("selecione…", ""));
  d.arquivos.forEach((a) =>
    sel.appendChild(new Option(a.nome + "  —  " + humano(a.tamanho), a.nome)));
}});

$("upload").addEventListener("change", (ev) => {{
  const arq = ev.target.files[0];
  if (!arq) return;
  enviado = null; revisar();
  const barra = $("progresso-envio"), estado = $("estado-envio");
  barra.hidden = false; barra.value = 0;
  estado.textContent = "enviando " + humano(arq.size) + "…";

  const req = new XMLHttpRequest();
  req.open("PUT", "/enviar?nome=" + encodeURIComponent(arq.name));
  req.upload.addEventListener("progress", (e) => {{
    if (e.lengthComputable) barra.value = (e.loaded / e.total) * 100;
  }});
  req.addEventListener("load", () => {{
    try {{
      const r = JSON.parse(req.responseText);
      if (r.ok) {{ enviado = r.nome; estado.textContent = "enviado: " + r.nome; }}
      else estado.textContent = "falhou: " + (r.erro || req.status);
    }} catch (e) {{ estado.textContent = "resposta inesperada do servidor"; }}
    revisar();
  }});
  req.addEventListener("error", () => {{ estado.textContent = "erro de rede"; }});
  req.send(arq);
}});

async function publicar(sobrescrever) {{
  $("publicar").disabled = true;
  $("recado").innerHTML = "";
  const corpo = {{
    planta: $("planta").value,
    arquivo: $("painel-envio").hidden ? $("arquivo").value : enviado,
    sem_nuvem: $("sem-nuvem").checked,
    voxel: $("usar-voxel").checked ? $("voxel").value : null,
    precisao: $("usar-precisao").checked ? $("precisao").value : null,
    sobrescrever: !!sobrescrever,
  }};
  const r = await fetch("/iniciar", {{
    method: "POST", headers: {{ "Content-Type": "application/json" }},
    body: JSON.stringify(corpo),
  }});
  const d = await r.json();
  if (r.ok) {{ location.href = "/tarefa/" + d.id; return; }}

  if (r.status === 409) {{
    $("recado").innerHTML =
      '<div class="recado ruim">' + d.detalhe +
      ' <button id="mesmo-assim">republicar por cima</button></div>';
    $("mesmo-assim").addEventListener("click", () => publicar(true));
  }} else {{
    $("recado").innerHTML = '<div class="recado ruim">' + (d.erro || "falhou") + '</div>';
  }}
  $("publicar").disabled = false;
}}

$("publicar").addEventListener("click", () => publicar(false));
</script></body></html>"""


def pagina_tarefa(ident):
    return f"""<!DOCTYPE html><html lang="pt-BR"><head>{CABECA}
<title>Adimensa | Publicando…</title>
<style>{ESTILO}</style></head><body>
<div class="folha">
{MARCA}
<h1 id="cabecalho">Publicando…</h1>
<p class="eyebrow" id="sub">preparando</p>

<div class="bloco">
  <div class="etapa"><b id="etapa">iniciando</b><span id="relogio">0s</span></div>
  <div class="barra"><i id="barra"></i></div>
  <div class="reguas"><span>0</span><span>bolhas</span><span>nuvem</span>
    <span>octree</span><span>100</span></div>
  <div class="registro" id="registro"></div>
</div>

<div id="final"></div>
<p style="margin-top:13px"><a class="voltar" href="/">&larr; publicar outro levantamento</a></p>
{RODAPE}
</div>

<script>
const $ = (id) => document.getElementById(id);
const ID = {json.dumps(ident)};
let desde = 0, encerrado = false;

function acrescentar(linhas) {{
  const caixa = $("registro");
  const colado = caixa.scrollTop + caixa.clientHeight >= caixa.scrollHeight - 30;
  for (const l of linhas) {{
    const div = document.createElement("div");
    const hora = document.createElement("span");
    hora.className = "hora"; hora.textContent = l.t;
    const txt = document.createElement("span");
    txt.className = l.tipo === "etapa" ? "etapa-linha" : l.tipo;
    txt.textContent = l.texto;
    div.append(hora, txt);
    caixa.appendChild(div);
  }}
  if (colado) caixa.scrollTop = caixa.scrollHeight;
}}

async function conferir() {{
  if (encerrado) return;
  try {{
    const r = await fetch("/estado/" + ID + "?desde=" + desde);
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    desde = d.total;
    acrescentar(d.linhas);
    $("barra").style.width = d.progresso + "%";
    $("etapa").textContent = d.etapa || d.estado;
    $("relogio").textContent = d.segundos + "s";
    $("sub").textContent = d.estado;

    if (d.estado === "concluida") {{
      encerrado = true;
      $("cabecalho").textContent = "Publicado";
      $("final").innerHTML =
        '<a class="saida" href="' + d.link + '" target="_blank">abrir o visualizador</a>';
    }} else if (d.estado === "falhou") {{
      encerrado = true;
      $("cabecalho").textContent = "Interrompido";
      $("final").innerHTML =
        '<div class="recado ruim">A publicação parou. O histórico acima mostra onde. ' +
        'Corrigido o problema, publique de novo com o mesmo nome: as etapas ' +
        'já concluídas são puladas.</div>';
    }}
  }} catch (erro) {{ /* tenta de novo no proximo ciclo */ }}
  if (!encerrado) setTimeout(conferir, 800);
}}
conferir();
</script></body></html>"""


# ------------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(description="Portal de publicacao de levantamentos.")
    ap.add_argument("--raiz", default=".",
                    help="pasta onde os sites sao criados (dentro do htdocs)")
    ap.add_argument("--entrada", default="./entrada",
                    help="pasta onde ficam os arquivos .e57")
    ap.add_argument("--modelo", default="./_modelo",
                    help="pasta com vendor/ e potree/ para copiar em cada site")
    ap.add_argument("--url-base", default="http://localhost/nuvens",
                    help="URL publica correspondente a --raiz")
    ap.add_argument("--porta", type=int, default=8800)
    ap.add_argument("--host", default="127.0.0.1",
                    help="use 0.0.0.0 para expor na rede local")
    ap.add_argument("--pdal", default=None)
    ap.add_argument("--potree-converter", default=None)
    args = ap.parse_args()

    for exigido in ("publicar.py", "extrair_direto.py"):
        if not os.path.isfile(os.path.join(AQUI, exigido)):
            print(f"Falta {exigido} na pasta {AQUI}")
            return 1

    cfg = {
        "raiz": os.path.abspath(args.raiz),
        "entrada": os.path.abspath(args.entrada),
        "modelo": os.path.abspath(args.modelo),
        "url_base": args.url_base,
        "pdal": args.pdal,
        "potree_converter": args.potree_converter,
    }
    os.makedirs(cfg["raiz"], exist_ok=True)
    os.makedirs(cfg["entrada"], exist_ok=True)
    Portal.cfg = cfg

    servidor = ThreadingHTTPServer((args.host, args.porta), Portal)
    print("=" * 60)
    print(f"  Portal em http://{args.host}:{args.porta}")
    print(f"  sites   : {cfg['raiz']}")
    print(f"  entrada : {cfg['entrada']}")
    print(f"  modelo  : {cfg['modelo']}")
    print(f"  publico : {cfg['url_base']}")
    print("=" * 60)
    print("  Ctrl+C encerra.")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nencerrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
