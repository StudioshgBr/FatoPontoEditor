from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import threading
import unicodedata
import webbrowser
from contextlib import redirect_stdout
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import tkinter as tk
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from services.email_reader import listar_alertas_nao_lidos
from services.codex_client import codex_healthcheck, run_codex_json


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PROMPTS_DIR = BASE_DIR / "prompts"
SCHEMAS_DIR = BASE_DIR / "schemas"
DB_PATH = DATA_DIR / "editor.db"

load_dotenv(BASE_DIR / ".env")

AI_PROVIDER = os.getenv("AI_PROVIDER", "codex").strip().lower()

CODEX_TRIAGE_MODEL = os.getenv("CODEX_TRIAGE_MODEL", "gpt-5.6-luna")
CODEX_EDITOR_MODEL = os.getenv("CODEX_EDITOR_MODEL", "gpt-5.6-luna")
ALLOW_OLLAMA_FALLBACK = os.getenv("ALLOW_OLLAMA_FALLBACK", "false").strip().lower() in {
    "1", "true", "yes", "sim",
}

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_TRIAGE_MODEL = os.getenv(
    "OLLAMA_TRIAGE_MODEL",
    os.getenv("OLLAMA_MODEL", "llama3.2:1b"),
)
OLLAMA_EDITOR_MODEL = os.getenv(
    "OLLAMA_EDITOR_MODEL",
    os.getenv("OLLAMA_MODEL", "llama3.2:3b"),
)
OLLAMA_THREADS = int(os.getenv("OLLAMA_THREADS", "2"))

TRIAGE_MODEL = CODEX_TRIAGE_MODEL if AI_PROVIDER == "codex" else OLLAMA_TRIAGE_MODEL
EDITOR_MODEL = CODEX_EDITOR_MODEL if AI_PROVIDER == "codex" else OLLAMA_EDITOR_MODEL

SIMILARIDADE_TITULO = 0.68
TRIAGE_BATCH_SIZE = 15 if AI_PROVIDER == "codex" else 5
MAX_TRIAGE_RETRIES = 3

PRIORITY_ORDER = {
    "CRITICA": 0,
    "ALTA": 1,
    "NORMAL": 2,
    "BAIXA": 3,
}

OBVIOUS_NOISE_DOMAINS = {
    "ingresso.com",
    "365scores.com",
}


# ==========================================================
# Banco de dados
# ==========================================================

def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    with db_connect() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_subject TEXT NOT NULL,
                title TEXT NOT NULL,
                source_domain TEXT NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                processed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                priority TEXT NOT NULL,
                group_name TEXT NOT NULL,
                reason TEXT NOT NULL,
                primary_source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'candidate',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS story_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                content TEXT,
                fetch_status TEXT,
                UNIQUE(story_id, canonical_url),
                FOREIGN KEY(story_id) REFERENCES stories(id)
            );

            CREATE TABLE IF NOT EXISTS drafts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                story_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                subtitle TEXT,
                body TEXT NOT NULL,
                seo_title TEXT,
                meta_description TEXT,
                tags_json TEXT,
                pending_checks_json TEXT,
                sources_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(story_id) REFERENCES stories(id)
            );
            """
        )


# ==========================================================
# Normalização e deduplicação
# ==========================================================

def remover_acentos(texto):
    texto = unicodedata.normalize("NFKD", str(texto))
    return "".join(c for c in texto if not unicodedata.combining(c))


def normalizar_titulo(titulo):
    texto = remover_acentos(titulo).lower()
    texto = re.sub(r"\s+[-|]\s+[^-|]{2,35}$", "", texto)
    texto = re.sub(r"[^a-z0-9\s]", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def similaridade_titulos(a, b):
    a = normalizar_titulo(a)
    b = normalizar_titulo(b)

    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    menor = min(len(a), len(b))
    if menor >= 25 and (a in b or b in a):
        return 0.95

    return SequenceMatcher(None, a, b).ratio()


def canonicalizar_url(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    ignorados = {
        "fbclid", "gclid", "utm_source", "utm_medium", "utm_campaign",
        "utm_term", "utm_content", "utm_id", "utm_name", "ref",
    }

    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in ignorados
    ]

    path = parsed.path.rstrip("/") or "/"

    return urlunparse(("https", host, path, "", urlencode(query), ""))


def dominio(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_obvious_noise(source_domain):
    return source_domain.lower() in OBVIOUS_NOISE_DOMAINS


def deduplicar_sinais(sinais):
    historias = []

    for sinal in sinais:
        encontrada = None

        for historia in historias:
            if any(
                similaridade_titulos(sinal["title"], membro["title"])
                >= SIMILARIDADE_TITULO
                for membro in historia["signals"]
            ):
                encontrada = historia
                break

        if encontrada:
            encontrada["signals"].append(sinal)
        else:
            historias.append({"signals": [sinal]})

    resultado = []

    for idx, historia in enumerate(historias, start=1):
        membros = historia["signals"]
        titulo = max(membros, key=lambda x: len(x["title"]))["title"]
        urls = sorted(m["canonical_url"] for m in membros)
        fingerprint = hashlib.sha256("\n".join(urls).encode("utf-8")).hexdigest()

        resultado.append(
            {
                "id": idx,
                "fingerprint": fingerprint,
                "titulo": titulo,
                "fontes": sorted({m["source_domain"] for m in membros}),
                "alertas": sorted({m["alert_subject"] for m in membros}),
                "variantes": [
                    {
                        "signal_id": m["id"],
                        "titulo": m["title"],
                        "fonte": m["source_domain"],
                        "url": m["url"],
                        "canonical_url": m["canonical_url"],
                    }
                    for m in membros
                ],
            }
        )

    return resultado


# ==========================================================
# E-mail / sinais
# ==========================================================

def importar_alertas():
    buffer = io.StringIO()

    with redirect_stdout(buffer):
        alertas = listar_alertas_nao_lidos(limit=50)

    agora = datetime.now().isoformat()
    novos = 0

    with db_connect() as conn:
        for alerta in alertas:
            for link in alerta["links"]:
                url = link["url"]
                canonica = canonicalizar_url(url)

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO signals
                    (alert_subject, title, source_domain, url, canonical_url, processed, created_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        alerta["subject"],
                        link["titulo"],
                        dominio(url),
                        url,
                        canonica,
                        agora,
                    ),
                )
                novos += cursor.rowcount

    return novos


def sinais_pendentes():
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM signals WHERE processed = 0 ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# ==========================================================
# Ollama / triagem
# ==========================================================

def carregar_prompt(nome):
    return (PROMPTS_DIR / nome).read_text(encoding="utf-8")


def ollama_chat(model, system_prompt, user_payload, *, num_ctx, num_predict, temperature=0.1, json_mode=False):
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": "5m",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ],
        "options": {
            "temperature": temperature,
            "num_ctx": num_ctx,
            "num_thread": OLLAMA_THREADS,
            "num_predict": num_predict,
        },
    }

    if json_mode:
        payload["format"] = "json"

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=600,
    )
    response.raise_for_status()
    return response.json()["message"]["content"]


def ai_json(*, role, system_prompt, user_payload):
    """Executa a etapa editorial no provedor configurado.

    Codex é o provedor principal. O Ollama pode permanecer como fallback
    offline caso ALLOW_OLLAMA_FALLBACK esteja habilitado.
    """

    if role not in {"triage", "editor"}:
        raise ValueError(f"Papel de IA inválido: {role}")

    if role == "triage":
        codex_model = CODEX_TRIAGE_MODEL
        ollama_model = OLLAMA_TRIAGE_MODEL
        schema = SCHEMAS_DIR / "triagem.schema.json"
        num_ctx = 2048
        num_predict = 700
        temperature = 0.1
    else:
        codex_model = CODEX_EDITOR_MODEL
        ollama_model = OLLAMA_EDITOR_MODEL
        schema = SCHEMAS_DIR / "redator.schema.json"
        num_ctx = 4096
        num_predict = 1600
        temperature = 0.2

    if AI_PROVIDER == "codex":
        try:
            return run_codex_json(
                model=codex_model,
                system_prompt=system_prompt,
                user_payload=user_payload,
                schema_path=schema,
                timeout=900 if role == "editor" else 600,
            )
        except Exception:
            if not ALLOW_OLLAMA_FALLBACK:
                raise

    content = ollama_chat(
        ollama_model,
        system_prompt,
        user_payload if isinstance(user_payload, str) else json.dumps(user_payload, ensure_ascii=False, indent=2),
        num_ctx=num_ctx,
        num_predict=num_predict,
        temperature=temperature,
        json_mode=True,
    )
    return json.loads(content)


def normalizar_enum(value):
    return remover_acentos(str(value or "")).strip().upper()


def validar_triagem_item(item, ids_esperados):
    if not isinstance(item, dict):
        return None

    try:
        item_id = int(item.get("id"))
    except (TypeError, ValueError):
        return None

    if item_id not in ids_esperados:
        return None

    decisao = normalizar_enum(item.get("decisao"))
    prioridade = normalizar_enum(item.get("prioridade"))

    if decisao not in {"MANTER", "DESCARTAR"}:
        return None
    if prioridade not in {"CRITICA", "ALTA", "NORMAL", "BAIXA"}:
        return None

    grupo = str(item.get("grupo", "")).strip()
    motivo = str(item.get("motivo", "")).strip()
    fonte_primaria = str(item.get("fonte_primaria", "")).strip()

    if not grupo or not motivo or not fonte_primaria:
        return None

    return item_id, {
        "id": item_id,
        "decisao": decisao,
        "prioridade": prioridade,
        "grupo": grupo,
        "motivo": motivo,
        "fonte_primaria": fonte_primaria,
    }


def analisar_lote_validado(lote):
    prompt = carregar_prompt("triagem.txt")
    pendentes = {h["id"]: h for h in lote}
    resultados = {}

    for _ in range(MAX_TRIAGE_RETRIES):
        if not pendentes:
            break

        payload = list(pendentes.values())
        try:
            data = ai_json(
                role="triage",
                system_prompt=prompt,
                user_payload=payload,
            )
        except Exception:
            continue

        items = data.get("items", [])
        if not isinstance(items, list):
            continue

        esperados = set(pendentes.keys())
        achados = set()

        for item in items:
            validado = validar_triagem_item(item, esperados)
            if not validado:
                continue
            item_id, normalizado = validado
            if item_id in achados:
                continue
            achados.add(item_id)
            resultados[item_id] = normalizado

        for item_id in achados:
            pendentes.pop(item_id, None)

    for item_id in pendentes:
        resultados[item_id] = {
            "id": item_id,
            "decisao": "MANTER",
            "prioridade": "NORMAL",
            "grupo": "VALIDACAO_PENDENTE",
            "motivo": "A IA não devolveu classificação válida após as tentativas.",
            "fonte_primaria": "PRECISA IDENTIFICAR",
        }

    return [resultados[h["id"]] for h in lote]


def prioridade_mais_alta(a, b):
    return a if PRIORITY_ORDER.get(a, 99) <= PRIORITY_ORDER.get(b, 99) else b


def encontrar_story_semelhante(conn, titulo):
    rows = conn.execute(
        "SELECT id, title, priority, status FROM stories ORDER BY id DESC LIMIT 300"
    ).fetchall()

    melhor = None
    melhor_score = 0.0

    for row in rows:
        score = similaridade_titulos(titulo, row["title"])
        if score > melhor_score:
            melhor_score = score
            melhor = row

    if melhor and melhor_score >= 0.78:
        return melhor
    return None


def persistir_historia(conn, historia, resultado):
    agora = datetime.now().isoformat()
    status = "candidate" if resultado["decisao"] == "MANTER" else "discarded"

    existente = encontrar_story_semelhante(conn, historia["titulo"])

    if existente:
        story_id = existente["id"]
        prioridade = prioridade_mais_alta(existente["priority"], resultado["prioridade"])
        conn.execute(
            """
            UPDATE stories
            SET priority = ?, updated_at = ?
            WHERE id = ?
            """,
            (prioridade, agora, story_id),
        )
    else:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO stories
            (fingerprint, title, priority, group_name, reason, primary_source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                historia["fingerprint"],
                historia["titulo"],
                resultado["prioridade"],
                resultado["grupo"],
                resultado["motivo"],
                resultado["fonte_primaria"],
                status,
                agora,
                agora,
            ),
        )
        if cursor.lastrowid:
            story_id = cursor.lastrowid
        else:
            row = conn.execute(
                "SELECT id FROM stories WHERE fingerprint = ?",
                (historia["fingerprint"],),
            ).fetchone()
            story_id = row["id"]

    for variante in historia["variantes"]:
        conn.execute(
            """
            INSERT OR IGNORE INTO story_sources
            (story_id, title, domain, url, canonical_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                story_id,
                variante["titulo"],
                variante["fonte"],
                variante["url"],
                variante["canonical_url"],
            ),
        )
        conn.execute(
            "UPDATE signals SET processed = 1 WHERE id = ?",
            (variante["signal_id"],),
        )

    return story_id


def rodar_triagem():
    sinais = sinais_pendentes()
    if not sinais:
        return {"signals": 0, "stories": 0, "kept": 0}

    # Ruído mecânico evidente: não gasta IA.
    uteis = []
    ruido = []
    for sinal in sinais:
        (ruido if is_obvious_noise(sinal["source_domain"]) else uteis).append(sinal)

    historias = deduplicar_sinais(uteis)
    resultados = []

    for inicio in range(0, len(historias), TRIAGE_BATCH_SIZE):
        lote = historias[inicio:inicio + TRIAGE_BATCH_SIZE]
        resultados.extend(analisar_lote_validado(lote))

    por_id = {r["id"]: r for r in resultados}
    mantidos = 0

    with db_connect() as conn:
        for historia in historias:
            resultado = por_id[historia["id"]]
            persistir_historia(conn, historia, resultado)
            if resultado["decisao"] == "MANTER":
                mantidos += 1

        for sinal in ruido:
            conn.execute(
                "UPDATE signals SET processed = 1 WHERE id = ?",
                (sinal["id"],),
            )

    return {
        "signals": len(sinais),
        "stories": len(historias),
        "kept": mantidos,
        "noise": len(ruido),
    }


# ==========================================================
# Fontes / apuração
# ==========================================================

def prioridade_fonte(url):
    host = dominio(url)
    oficial = (
        ".gov.br",
        ".jus.br",
        "gov.br",
        "sp.gov.br",
        "camara.leg.br",
        "senado.leg.br",
        "stf.jus.br",
        "stj.jus.br",
        "tjsp.jus.br",
        "mpsp.mp.br",
        "mpf.mp.br",
    )
    return 0 if any(x in host for x in oficial) else 1


def extrair_texto_web(url, limite=5500):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; FatoEPonto-EditorLocal/1.0)",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.5",
    }

    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").lower()
    if "html" not in content_type:
        raise ValueError(f"Conteúdo não HTML: {content_type}")

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup([
        "script", "style", "noscript", "svg", "form", "nav", "footer", "header", "aside"
    ]):
        tag.decompose()

    paragrafos = []
    vistos = set()

    for p in soup.find_all(["p", "h1", "h2"]):
        texto = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
        if len(texto) < 35:
            continue
        if texto in vistos:
            continue
        vistos.add(texto)
        paragrafos.append(texto)

    texto = "\n\n".join(paragrafos)
    return texto[:limite]


def buscar_material_story(story_id, max_fontes=3):
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM story_sources WHERE story_id = ?",
            (story_id,),
        ).fetchall()

    fontes = sorted([dict(r) for r in rows], key=lambda r: prioridade_fonte(r["url"]))
    blocos = []

    for fonte in fontes[:max_fontes]:
        try:
            texto = fonte.get("content") or extrair_texto_web(fonte["url"])
            status = "ok"
        except Exception as exc:
            texto = ""
            status = f"erro: {exc}"

        with db_connect() as conn:
            conn.execute(
                "UPDATE story_sources SET content = ?, fetch_status = ? WHERE id = ?",
                (texto or None, status, fonte["id"]),
            )

        if texto:
            blocos.append(
                f"FONTE: {fonte['domain']}\n"
                f"URL: {fonte['url']}\n"
                f"TÍTULO ENCONTRADO: {fonte['title']}\n"
                f"CONTEÚDO EXTRAÍDO:\n{texto}"
            )

    return "\n\n" + ("\n\n" + "=" * 70 + "\n\n").join(blocos) if blocos else ""


# ==========================================================
# Geração de rascunho
# ==========================================================

def get_story(story_id):
    with db_connect() as conn:
        story = conn.execute(
            "SELECT * FROM stories WHERE id = ?",
            (story_id,),
        ).fetchone()
        fontes = conn.execute(
            "SELECT * FROM story_sources WHERE story_id = ? ORDER BY id",
            (story_id,),
        ).fetchall()

    return dict(story), [dict(f) for f in fontes]


def gerar_rascunho(story_id, material):
    story, fontes = get_story(story_id)
    prompt = carregar_prompt("redator.txt")

    payload = {
        "pauta": {
            "titulo_interno": story["title"],
            "prioridade": story["priority"],
            "grupo": story["group_name"],
            "motivo": story["reason"],
            "fonte_primaria_provavel": story["primary_source"],
        },
        "fontes": [
            {"titulo": f["title"], "dominio": f["domain"], "url": f["url"]}
            for f in fontes
        ],
        "material_de_apuracao": material[:12000],
    }

    data = ai_json(
        role="editor",
        system_prompt=prompt,
        user_payload=payload,
    )

    obrigatorios = ["titulo", "corpo", "pendencias_de_confirmacao"]
    for campo in obrigatorios:
        if campo not in data:
            raise ValueError(f"Resposta do redator sem campo obrigatório: {campo}")

    agora = datetime.now().isoformat()

    with db_connect() as conn:
        conn.execute(
            """
            INSERT INTO drafts
            (story_id, title, subtitle, body, seo_title, meta_description,
             tags_json, pending_checks_json, sources_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                story_id,
                data.get("titulo", ""),
                data.get("subtitulo", ""),
                data.get("corpo", ""),
                data.get("seo_title", ""),
                data.get("meta_description", ""),
                json.dumps(data.get("tags", []), ensure_ascii=False),
                json.dumps(data.get("pendencias_de_confirmacao", []), ensure_ascii=False),
                json.dumps(data.get("fontes_utilizadas", []), ensure_ascii=False),
                agora,
            ),
        )
        conn.execute(
            "UPDATE stories SET status = 'draft_ready', updated_at = ? WHERE id = ?",
            (agora, story_id),
        )

    return data


def formatar_rascunho(data):
    tags = ", ".join(data.get("tags", []))
    pendencias = data.get("pendencias_de_confirmacao", [])
    fontes = data.get("fontes_utilizadas", [])

    pend_text = "\n".join(f"- {p}" for p in pendencias) or "- Nenhuma indicada pela IA"
    fontes_text = "\n".join(f"- {f}" for f in fontes) or "- Conferir lista de fontes da pauta"

    return (
        f"TÍTULO\n{data.get('titulo', '')}\n\n"
        f"SUBTÍTULO\n{data.get('subtitulo', '')}\n\n"
        f"MATÉRIA\n{data.get('corpo', '')}\n\n"
        f"SEO TITLE\n{data.get('seo_title', '')}\n\n"
        f"META DESCRIPTION\n{data.get('meta_description', '')}\n\n"
        f"TAGS\n{tags}\n\n"
        f"PENDÊNCIAS DE CONFIRMAÇÃO\n{pend_text}\n\n"
        f"FONTES UTILIZADAS\n{fontes_text}\n\n"
        "STATUS: AGUARDANDO REVISÃO HUMANA"
    )


# ==========================================================
# Interface
# ==========================================================

class EditorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fato & Ponto — Editor Local")
        self.geometry("1380x850")
        self.minsize(1100, 700)

        self.selected_story_id = None
        self.busy = False

        self._build_ui()
        self.refresh_stories()
        provider_label = "Codex/ChatGPT" if AI_PROVIDER == "codex" else "Ollama local"
        self.set_status(
            f"Pronto | IA: {provider_label} | Triagem: {TRIAGE_MODEL} | Redator: {EDITOR_MODEL}"
        )

    def _build_ui(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")

        self.btn_email = ttk.Button(top, text="Atualizar e-mails", command=self.on_import_email)
        self.btn_email.pack(side="left", padx=(0, 6))

        self.btn_triage = ttk.Button(top, text="Rodar triagem", command=self.on_triage)
        self.btn_triage.pack(side="left", padx=(0, 6))

        self.btn_refresh = ttk.Button(top, text="Atualizar lista", command=self.refresh_stories)
        self.btn_refresh.pack(side="left", padx=(0, 6))

        self.btn_ai = ttk.Button(top, text="Testar IA", command=self.on_test_ai)
        self.btn_ai.pack(side="left", padx=(0, 6))

        self.btn_source = ttk.Button(top, text="Abrir 1ª fonte", command=self.open_first_source)
        self.btn_source.pack(side="left", padx=(18, 6))

        self.btn_fetch = ttk.Button(top, text="Buscar fontes", command=self.on_fetch_sources)
        self.btn_fetch.pack(side="left", padx=(0, 6))

        self.btn_draft = ttk.Button(top, text="Gerar rascunho", command=self.on_generate_draft)
        self.btn_draft.pack(side="left", padx=(0, 6))

        self.btn_copy = ttk.Button(top, text="Copiar rascunho", command=self.copy_draft)
        self.btn_copy.pack(side="left", padx=(0, 6))

        self.btn_approved = ttk.Button(top, text="Marcar revisado", command=self.mark_reviewed)
        self.btn_approved.pack(side="left")

        paned = ttk.Panedwindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(paned, padding=4)
        right = ttk.Frame(paned, padding=4)
        paned.add(left, weight=2)
        paned.add(right, weight=5)

        ttk.Label(left, text="Mesa de pauta").pack(anchor="w", pady=(0, 4))

        self.tree = ttk.Treeview(
            left,
            columns=("priority", "status", "sources"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Pauta")
        self.tree.heading("priority", text="Prior.")
        self.tree.heading("status", text="Status")
        self.tree.heading("sources", text="Fontes")
        self.tree.column("#0", width=330, stretch=True)
        self.tree.column("priority", width=75, anchor="center")
        self.tree.column("status", width=95, anchor="center")
        self.tree.column("sources", width=55, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.on_story_select)

        notebook = ttk.Notebook(right)
        notebook.pack(fill="both", expand=True)

        pauta_tab = ttk.Frame(notebook)
        apuracao_tab = ttk.Frame(notebook)
        draft_tab = ttk.Frame(notebook)

        notebook.add(pauta_tab, text="Pauta")
        notebook.add(apuracao_tab, text="Apuração")
        notebook.add(draft_tab, text="Rascunho")

        self.details = ScrolledText(pauta_tab, wrap="word", font=("Segoe UI", 10))
        self.details.pack(fill="both", expand=True, padx=6, pady=6)
        self.details.configure(state="disabled")

        ttk.Label(
            apuracao_tab,
            text="Conteúdo obtido das fontes. Você pode complementar ou corrigir manualmente antes de gerar.",
        ).pack(anchor="w", padx=6, pady=(6, 0))
        self.research = ScrolledText(apuracao_tab, wrap="word", font=("Segoe UI", 10))
        self.research.pack(fill="both", expand=True, padx=6, pady=6)

        self.draft = ScrolledText(draft_tab, wrap="word", font=("Segoe UI", 10))
        self.draft.pack(fill="both", expand=True, padx=6, pady=6)

        self.status_var = tk.StringVar(value="Pronto")
        status = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=5)
        status.pack(fill="x", side="bottom")

    def set_status(self, text):
        self.status_var.set(text)
        self.update_idletasks()

    def set_busy(self, busy, status=None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in (
            self.btn_email, self.btn_triage, self.btn_refresh, self.btn_ai, self.btn_source,
            self.btn_fetch, self.btn_draft, self.btn_copy, self.btn_approved,
        ):
            button.configure(state=state)
        if status:
            self.set_status(status)

    def background(self, status, worker, success_message=None, callback=None):
        if self.busy:
            return

        self.set_busy(True, status)

        def run():
            try:
                result = worker()
                self.after(0, lambda: done(result))
            except Exception as exc:
                self.after(0, lambda: failed(exc))

        def done(result):
            self.set_busy(False, success_message or "Concluído")
            if callback:
                callback(result)

        def failed(exc):
            self.set_busy(False, "Erro")
            messagebox.showerror("Fato & Ponto", str(exc))

        threading.Thread(target=run, daemon=True).start()

    def refresh_stories(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM story_sources ss WHERE ss.story_id = s.id) AS source_count
                FROM stories s
                WHERE s.status <> 'discarded'
                ORDER BY
                  CASE s.priority
                    WHEN 'CRITICA' THEN 0
                    WHEN 'ALTA' THEN 1
                    WHEN 'NORMAL' THEN 2
                    WHEN 'BAIXA' THEN 3
                    ELSE 9
                  END,
                  s.updated_at DESC
                """
            ).fetchall()

        for row in rows:
            self.tree.insert(
                "",
                "end",
                iid=str(row["id"]),
                text=row["title"],
                values=(row["priority"], row["status"], row["source_count"]),
            )

        self.set_status(f"Mesa atualizada: {len(rows)} pauta(s)")

    def on_test_ai(self):
        if AI_PROVIDER != "codex":
            messagebox.showinfo(
                "Fato & Ponto",
                f"Provedor atual: Ollama local ({TRIAGE_MODEL}).",
            )
            return

        def worker():
            return codex_healthcheck(CODEX_TRIAGE_MODEL)

        def finished(info):
            self.set_status(
                f"Codex pronto | {info['version']} | {info['login']} | {info['model']}"
            )
            messagebox.showinfo(
                "Fato & Ponto",
                "Codex conectado corretamente.\n\n"
                f"{info['version']}\n"
                f"{info['login']}\n\n"
                f"Modelo editorial: {CODEX_TRIAGE_MODEL}\n"
                f"Disponível no catálogo: {info['model']}",
            )

        self.background(
            "Verificando Codex e autenticação ChatGPT...",
            worker,
            callback=finished,
        )

    def on_import_email(self):
        self.background(
            "Lendo mensagens da caixa IMAP configurada...",
            importar_alertas,
            callback=lambda n: self.set_status(f"E-mails lidos: {n} novo(s) sinal(is) armazenado(s)"),
        )

    def on_triage(self):
        def finished(result):
            self.refresh_stories()
            self.set_status(
                f"Triagem concluída: {result['signals']} sinais, {result['stories']} histórias, "
                f"{result['kept']} mantidas, {result.get('noise', 0)} ruídos mecânicos"
            )

        self.background(
            "Triando somente sinais novos...",
            rodar_triagem,
            callback=finished,
        )

    def on_story_select(self, _event=None):
        selecionados = self.tree.selection()
        if not selecionados:
            return

        self.selected_story_id = int(selecionados[0])
        story, fontes = get_story(self.selected_story_id)

        texto = (
            f"PAUTA\n{story['title']}\n\n"
            f"PRIORIDADE\n{story['priority']}\n\n"
            f"STATUS\n{story['status']}\n\n"
            f"GRUPO\n{story['group_name']}\n\n"
            f"POR QUE FOI MANTIDA\n{story['reason']}\n\n"
            f"FONTE PRIMÁRIA PROVÁVEL\n{story['primary_source']}\n\n"
            "FONTES ENCONTRADAS\n"
        )

        for idx, fonte in enumerate(fontes, start=1):
            texto += f"{idx}. {fonte['domain']}\n   {fonte['url']}\n"

        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", texto)
        self.details.configure(state="disabled")

        blocos = []
        for fonte in fontes:
            if fonte.get("content"):
                blocos.append(
                    f"FONTE: {fonte['domain']}\nURL: {fonte['url']}\n\n{fonte['content']}"
                )
        self.research.delete("1.0", "end")
        if blocos:
            self.research.insert("1.0", "\n\n" + ("\n\n" + "=" * 70 + "\n\n").join(blocos))

        with db_connect() as conn:
            draft = conn.execute(
                "SELECT * FROM drafts WHERE story_id = ? ORDER BY id DESC LIMIT 1",
                (self.selected_story_id,),
            ).fetchone()

        self.draft.delete("1.0", "end")
        if draft:
            data = {
                "titulo": draft["title"],
                "subtitulo": draft["subtitle"],
                "corpo": draft["body"],
                "seo_title": draft["seo_title"],
                "meta_description": draft["meta_description"],
                "tags": json.loads(draft["tags_json"] or "[]"),
                "pendencias_de_confirmacao": json.loads(draft["pending_checks_json"] or "[]"),
                "fontes_utilizadas": json.loads(draft["sources_json"] or "[]"),
            }
            self.draft.insert("1.0", formatar_rascunho(data))

    def require_story(self):
        if not self.selected_story_id:
            messagebox.showinfo("Fato & Ponto", "Selecione uma pauta primeiro.")
            return False
        return True

    def open_first_source(self):
        if not self.require_story():
            return
        _, fontes = get_story(self.selected_story_id)
        if not fontes:
            messagebox.showinfo("Fato & Ponto", "Essa pauta não possui fontes cadastradas.")
            return
        webbrowser.open(fontes[0]["url"])

    def on_fetch_sources(self):
        if not self.require_story():
            return
        story_id = self.selected_story_id

        def finished(material):
            self.research.delete("1.0", "end")
            if material:
                self.research.insert("1.0", material)
                self.set_status("Fontes carregadas. Revise a aba Apuração antes de gerar o rascunho.")
            else:
                self.set_status("Nenhuma fonte pôde ser extraída automaticamente. Abra a fonte e cole o material na aba Apuração.")

        self.background(
            "Buscando conteúdo das fontes...",
            lambda: buscar_material_story(story_id),
            callback=finished,
        )

    def on_generate_draft(self):
        if not self.require_story():
            return

        story_id = self.selected_story_id
        material = self.research.get("1.0", "end").strip()

        def worker():
            nonlocal material
            if not material:
                material = buscar_material_story(story_id)
            if not material:
                raise RuntimeError(
                    "Não foi possível obter conteúdo das fontes. Abra uma fonte, copie as informações relevantes para a aba Apuração e tente novamente."
                )
            return gerar_rascunho(story_id, material)

        def finished(data):
            self.draft.delete("1.0", "end")
            self.draft.insert("1.0", formatar_rascunho(data))
            self.refresh_stories()
            self.set_status("Rascunho gerado. Confira fatos, pendências e fontes antes de publicar.")

        self.background(
            "Gerando rascunho jornalístico com a IA...",
            worker,
            callback=finished,
        )

    def copy_draft(self):
        texto = self.draft.get("1.0", "end").strip()
        if not texto:
            return
        self.clipboard_clear()
        self.clipboard_append(texto)
        self.set_status("Rascunho copiado para a área de transferência")

    def mark_reviewed(self):
        if not self.require_story():
            return
        agora = datetime.now().isoformat()
        with db_connect() as conn:
            conn.execute(
                "UPDATE stories SET status = 'reviewed', updated_at = ? WHERE id = ?",
                (agora, self.selected_story_id),
            )
        self.refresh_stories()
        self.set_status("Pauta marcada como revisada. A publicação continua sendo manual.")


if __name__ == "__main__":
    init_db()
    app = EditorApp()
    app.mainloop()
