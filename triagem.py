from pathlib import Path
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from contextlib import redirect_stdout
from datetime import datetime
from difflib import SequenceMatcher

import io
import json
import os
import re
import unicodedata

import requests
from dotenv import load_dotenv

from services.email_reader import listar_alertas_nao_lidos


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

OLLAMA_URL = os.getenv(
    "OLLAMA_URL",
    "http://127.0.0.1:11434",
)

OLLAMA_MODEL = os.getenv(
    "OLLAMA_MODEL",
    "llama3.2:3b",
)

PROMPT_FILE = BASE_DIR / "prompts" / "triagem.txt"
DATA_DIR = BASE_DIR / "data"

# Seu PC é mais antigo, então continuamos com lotes pequenos.
BATCH_SIZE = 5

# Quantas vezes tentar novamente quando a IA esquecer algum ID
# ou devolver estrutura inválida.
MAX_TENTATIVAS = 3

# Similaridade mínima entre títulos.
#
# 0.68 funcionou bem para títulos do tipo:
# "Barueri está entre os 10 municípios..."
# "Barueri é a 10ª cidade mais competitiva..."
#
# Não baixe muito esse valor, senão histórias diferentes podem
# começar a ser agrupadas.
SIMILARIDADE_TITULO = 0.68


# ==========================================================
# UTILIDADES
# ==========================================================

def remover_acentos(texto):
    texto = unicodedata.normalize(
        "NFKD",
        str(texto),
    )

    return "".join(
        caractere
        for caractere in texto
        if not unicodedata.combining(caractere)
    )


def normalizar_enum(texto):
    texto = remover_acentos(
        str(texto or "")
    )

    return texto.strip().upper()


def normalizar_titulo(titulo):
    """
    Normalização usada SOMENTE para comparar títulos.

    O título original continua preservado.
    """

    texto = remover_acentos(
        titulo
    ).lower()

    # Remove alguns sufixos comuns de veículo.
    # Exemplo:
    # "... - CNN Brasil"
    # "... | UOL"
    texto = re.sub(
        r"\s+[-|]\s+[^-|]{2,35}$",
        "",
        texto,
    )

    # Normaliza ordinal:
    # 10ª -> 10
    texto = re.sub(
        r"(\d+)[ao]?\b",
        r"\1",
        texto,
    )

    texto = re.sub(
        r"[^a-z0-9\s]",
        " ",
        texto,
    )

    texto = re.sub(
        r"\s+",
        " ",
        texto,
    ).strip()

    return texto


def similaridade_titulos(titulo_a, titulo_b):
    a = normalizar_titulo(
        titulo_a
    )

    b = normalizar_titulo(
        titulo_b
    )

    if not a or not b:
        return 0.0

    if a == b:
        return 1.0

    # Se um título estiver praticamente contido no outro,
    # já é um ótimo sinal de duplicidade.
    menor = min(
        len(a),
        len(b),
    )

    if menor >= 25 and (
        a in b or b in a
    ):
        return 0.95

    return SequenceMatcher(
        None,
        a,
        b,
    ).ratio()


# ==========================================================
# URL
# ==========================================================

def canonicalizar_url(url):
    """
    Normaliza a URL para detectar a mesma página recebida
    através de parâmetros diferentes.

    A URL original continua preservada.
    """

    parsed = urlparse(
        url.strip()
    )

    host = parsed.netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    parametros_ignorados = {
        "fbclid",
        "gclid",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "ref",
    }

    query = []

    for chave, valor in parse_qsl(
        parsed.query,
        keep_blank_values=False,
    ):
        if chave.lower() not in parametros_ignorados:
            query.append(
                (
                    chave,
                    valor,
                )
            )

    path = parsed.path.rstrip("/")

    if not path:
        path = "/"

    # http e https são considerados a mesma página
    # para fins de deduplicação.
    return urlunparse(
        (
            "https",
            host,
            path,
            "",
            urlencode(query),
            "",
        )
    )


def dominio(url):
    host = urlparse(
        url
    ).netloc.lower()

    if host.startswith("www."):
        host = host[4:]

    return host


# ==========================================================
# COLETA DOS GOOGLE ALERTS
# ==========================================================

def coletar_sinais():
    """
    Lê os Google Alerts.

    Primeiro elimina URLs exatamente repetidas.
    """

    buffer = io.StringIO()

    with redirect_stdout(buffer):
        alertas = listar_alertas_nao_lidos(
            limit=50
        )

    sinais = []
    urls_vistas = set()

    for alerta in alertas:
        for link in alerta["links"]:

            url_original = link["url"]

            url_normalizada = canonicalizar_url(
                url_original
            )

            if url_normalizada in urls_vistas:
                continue

            urls_vistas.add(
                url_normalizada
            )

            sinais.append(
                {
                    "id_original": len(sinais) + 1,
                    "alerta": alerta["subject"],
                    "titulo": link["titulo"],
                    "fonte": dominio(
                        url_original
                    ),
                    "url": url_original,
                    "url_canonica": url_normalizada,
                }
            )

    return sinais


# ==========================================================
# DEDUPLICAÇÃO DE HISTÓRIAS
# ==========================================================

def pertence_a_historia(sinal, historia):
    """
    Compara o novo título contra TODOS os membros já
    pertencentes à história.

    Isso permite agrupamento transitivo.

    Exemplo:

    título A parecido com B
    título B parecido com C

    mesmo que A e C sejam um pouco menos parecidos,
    os três podem acabar corretamente na mesma história.
    """

    for membro in historia["sinais"]:
        score = similaridade_titulos(
            sinal["titulo"],
            membro["titulo"],
        )

        if score >= SIMILARIDADE_TITULO:
            return True

    return False


def escolher_titulo_representativo(sinais):
    """
    Prefere o título mais informativo (normalmente o mais longo).
    """

    return max(
        sinais,
        key=lambda item: len(
            item["titulo"]
        ),
    )["titulo"]


def deduplicar_historias(sinais):
    historias = []

    for sinal in sinais:

        historia_encontrada = None

        for historia in historias:
            if pertence_a_historia(
                sinal,
                historia,
            ):
                historia_encontrada = historia
                break

        if historia_encontrada:
            historia_encontrada[
                "sinais"
            ].append(
                sinal
            )

        else:
            historias.append(
                {
                    "sinais": [
                        sinal
                    ]
                }
            )

    resultado = []

    for numero, historia in enumerate(
        historias,
        start=1,
    ):
        sinais_historia = historia[
            "sinais"
        ]

        titulo = escolher_titulo_representativo(
            sinais_historia
        )

        fontes = sorted(
            {
                sinal["fonte"]
                for sinal in sinais_historia
            }
        )

        alertas = sorted(
            {
                sinal["alerta"]
                for sinal in sinais_historia
            }
        )

        resultado.append(
            {
                "id": numero,
                "titulo": titulo,
                "quantidade_fontes": len(
                    fontes
                ),
                "fontes": fontes,
                "alertas": alertas,
                "variantes": [
                    {
                        "titulo": sinal["titulo"],
                        "fonte": sinal["fonte"],
                        "url": sinal["url"],
                    }
                    for sinal in sinais_historia
                ],
            }
        )

    return resultado


# ==========================================================
# LOTES
# ==========================================================

def dividir_lotes(lista, tamanho):
    for inicio in range(
        0,
        len(lista),
        tamanho,
    ):
        yield lista[
            inicio:inicio + tamanho
        ]


# ==========================================================
# OLLAMA
# ==========================================================

def chamar_ollama(lote):
    prompt = PROMPT_FILE.read_text(
        encoding="utf-8"
    )

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "format": "json",
        "keep_alive": "5m",
        "messages": [
            {
                "role": "system",
                "content": prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    lote,
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        ],
       "options": {
            "temperature": 0.1,
            "num_ctx": 2048,
            "num_thread": 2,
            "num_predict": 512,
        },
    }

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=300,
    )

    response.raise_for_status()

    content = response.json()[
        "message"
    ]["content"]

    return json.loads(
        content
    )


# ==========================================================
# VALIDAÇÃO
# ==========================================================

def validar_item(item, ids_esperados):
    """
    Retorna (id, item_normalizado) ou None.

    Resultado inválido NÃO entra silenciosamente.
    """

    if not isinstance(item, dict):
        return None

    try:
        item_id = int(
            item.get("id")
        )
    except (
        TypeError,
        ValueError,
    ):
        return None

    if item_id not in ids_esperados:
        return None

    decisao = normalizar_enum(
        item.get("decisao")
    )

    prioridade = normalizar_enum(
        item.get("prioridade")
    )

    decisoes_validas = {
        "MANTER",
        "DESCARTAR",
    }

    prioridades_validas = {
        "CRITICA",
        "ALTA",
        "NORMAL",
        "BAIXA",
    }

    if decisao not in decisoes_validas:
        return None

    if prioridade not in prioridades_validas:
        return None

    grupo = str(
        item.get(
            "grupo",
            ""
        )
    ).strip()

    motivo = str(
        item.get(
            "motivo",
            ""
        )
    ).strip()

    fonte_primaria = str(
        item.get(
            "fonte_primaria",
            ""
        )
    ).strip()

    if not grupo:
        return None

    if not motivo:
        return None

    if not fonte_primaria:
        return None

    normalizado = {
        "id": item_id,
        "decisao": decisao,
        "prioridade": prioridade,
        "grupo": grupo,
        "motivo": motivo,
        "fonte_primaria": fonte_primaria,
    }

    return (
        item_id,
        normalizado,
    )


def analisar_com_validacao(lote):
    """
    Garante uma resposta válida para cada ID do lote.

    Se um ID ficar ausente, apenas ele é enviado novamente.
    """

    por_id = {
        historia["id"]: historia
        for historia in lote
    }

    pendentes = dict(
        por_id
    )

    resultados_validos = {}

    for tentativa in range(
        1,
        MAX_TENTATIVAS + 1,
    ):

        if not pendentes:
            break

        lote_atual = list(
            pendentes.values()
        )

        if tentativa > 1:
            print(
                f"      ↳ Reprocessando "
                f"{len(lote_atual)} ID(s) "
                f"ausente(s)/inválido(s)..."
            )

        try:
            resposta = chamar_ollama(
                lote_atual
            )

        except Exception as exc:
            print(
                f"      ↳ Falha na tentativa "
                f"{tentativa}: {exc}"
            )

            continue

        items = resposta.get(
            "items",
            []
        )

        if not isinstance(
            items,
            list,
        ):
            continue

        ids_esperados = set(
            pendentes.keys()
        )

        encontrados_nesta_tentativa = set()

        for item in items:

            validado = validar_item(
                item,
                ids_esperados,
            )

            if not validado:
                continue

            item_id, item_normalizado = (
                validado
            )

            # Se o modelo repetir o mesmo ID,
            # apenas a primeira resposta válida é usada.
            if item_id in encontrados_nesta_tentativa:
                continue

            encontrados_nesta_tentativa.add(
                item_id
            )

            resultados_validos[
                item_id
            ] = item_normalizado

        for item_id in encontrados_nesta_tentativa:
            pendentes.pop(
                item_id,
                None,
            )

    # IMPORTANTE:
    # Falha da IA não pode apagar uma possível notícia.
    #
    # Se após todas as tentativas um item não foi analisado,
    # ele é mantido como NORMAL para revisão humana/editorial.
    if pendentes:

        print(
            f"      ↳ ATENÇÃO: "
            f"{len(pendentes)} item(ns) "
            f"não receberam resposta válida."
        )

        for item_id in pendentes:

            resultados_validos[
                item_id
            ] = {
                "id": item_id,
                "decisao": "MANTER",
                "prioridade": "NORMAL",
                "grupo": "VALIDACAO_PENDENTE",
                "motivo": (
                    "O modelo não devolveu "
                    "uma classificação válida "
                    "após as tentativas."
                ),
                "fonte_primaria": (
                    "PRECISA IDENTIFICAR"
                ),
            }

    # Retorna exatamente um registro para cada ID,
    # na ordem original.
    return [
        resultados_validos[
            historia["id"]
        ]
        for historia in lote
    ]


# ==========================================================
# RESULTADO
# ==========================================================

def ordenar_mantidos(lista):
    ordem = {
        "CRITICA": 0,
        "ALTA": 1,
        "NORMAL": 2,
        "BAIXA": 3,
    }

    lista.sort(
        key=lambda item: ordem.get(
            item.get(
                "prioridade",
                "BAIXA",
            ),
            99,
        )
    )


def salvar_resultado(
    sinais,
    historias,
    resultados,
    mantidos,
):
    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    agora = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    arquivo = (
        DATA_DIR
        / f"triagem_{agora}.json"
    )

    conteudo = {
        "generated_at": (
            datetime.now().isoformat()
        ),
        "model": OLLAMA_MODEL,
        "similaridade_titulo": (
            SIMILARIDADE_TITULO
        ),
        "total_sinais_urls_unicas": len(
            sinais
        ),
        "total_historias_apos_deduplicacao": len(
            historias
        ),
        "duplicatas_agrupadas": (
            len(sinais)
            - len(historias)
        ),
        "total_analisados": len(
            resultados
        ),
        "total_mantidos": len(
            mantidos
        ),
        "historias": historias,
        "mantidos": mantidos,
    }

    arquivo.write_text(
        json.dumps(
            conteudo,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print(
        f"Resultado salvo em:"
        f"\n{arquivo}"
    )


# ==========================================================
# EXECUÇÃO PRINCIPAL
# ==========================================================

def executar():
    print(
        "=" * 70
    )

    print(
        "TRIAGEM — FATO & PONTO"
    )

    print(
        f"Modelo: {OLLAMA_MODEL}"
    )

    print(
        "=" * 70
    )

    print(
        "\n1. Coletando Google Alerts..."
    )

    sinais = coletar_sinais()

    print(
        f"   URLs únicas encontradas: "
        f"{len(sinais)}"
    )

    print(
        "\n2. Deduplicando histórias..."
    )

    historias = deduplicar_historias(
        sinais
    )

    duplicatas = (
        len(sinais)
        - len(historias)
    )

    print(
        f"   Histórias encontradas: "
        f"{len(historias)}"
    )

    print(
        f"   Duplicatas agrupadas: "
        f"{duplicatas}"
    )

    # Mostra na tela apenas os grupos que realmente
    # possuam mais de uma publicação.
    grupos_duplicados = [
        historia
        for historia in historias
        if len(
            historia["variantes"]
        ) > 1
    ]

    if grupos_duplicados:
        print(
            "\n   Grupos detectados:"
        )

        for historia in grupos_duplicados:

            print()
            print(
                f"   HISTÓRIA "
                f"{historia['id']}: "
                f"{historia['titulo']}"
            )

            for variante in historia[
                "variantes"
            ]:
                print(
                    f"      - "
                    f"{variante['fonte']}: "
                    f"{variante['titulo']}"
                )

    print(
        "\n3. Iniciando triagem editorial..."
    )

    lotes = list(
        dividir_lotes(
            historias,
            BATCH_SIZE,
        )
    )

    total_lotes = len(
        lotes
    )

    resultados = []

    for numero, lote in enumerate(
        lotes,
        start=1,
    ):
        print(
            f"   Analisando lote "
            f"{numero}/{total_lotes}..."
        )

        resultado_lote = (
            analisar_com_validacao(
                lote
            )
        )

        resultados.extend(
            resultado_lote
        )

    # ======================================================
    # VALIDAÇÃO GLOBAL
    # ======================================================

    ids_historias = {
        historia["id"]
        for historia in historias
    }

    ids_resultados = {
        resultado["id"]
        for resultado in resultados
    }

    faltantes = (
        ids_historias
        - ids_resultados
    )

    extras = (
        ids_resultados
        - ids_historias
    )

    print()
    print(
        "4. Validação global:"
    )

    print(
        f"   Histórias enviadas: "
        f"{len(ids_historias)}"
    )

    print(
        f"   Histórias analisadas: "
        f"{len(ids_resultados)}"
    )

    print(
        f"   IDs faltantes: "
        f"{len(faltantes)}"
    )

    print(
        f"   IDs inesperados: "
        f"{len(extras)}"
    )

    if faltantes:
        raise RuntimeError(
            "Validação global falhou: "
            f"IDs faltantes: "
            f"{sorted(faltantes)}"
        )

    if extras:
        raise RuntimeError(
            "Validação global falhou: "
            f"IDs inesperados: "
            f"{sorted(extras)}"
        )

    # ======================================================
    # JUNTA RESULTADO DA IA À HISTÓRIA
    # ======================================================

    historias_por_id = {
        historia["id"]: historia
        for historia in historias
    }

    mantidos = []

    for resultado in resultados:

        historia = historias_por_id[
            resultado["id"]
        ]

        registro = {
            **historia,
            **resultado,
        }

        if (
            resultado["decisao"]
            == "MANTER"
        ):
            mantidos.append(
                registro
            )

    ordenar_mantidos(
        mantidos
    )

    # ======================================================
    # TELA FINAL
    # ======================================================

    print()
    print(
        "=" * 70
    )

    print(
        "CANDIDATOS À MESA DE PAUTA"
    )

    print(
        "=" * 70
    )

    print(
        f"\nMantidos: "
        f"{len(mantidos)} "
        f"de {len(historias)} histórias."
    )

    for numero, item in enumerate(
        mantidos,
        start=1,
    ):
        print()

        print(
            f"{numero}. "
            f"[{item['prioridade']}] "
            f"{item['titulo']}"
        )

        print(
            f"   Grupo: "
            f"{item['grupo']}"
        )

        print(
            f"   Publicações encontradas: "
            f"{len(item['variantes'])}"
        )

        print(
            f"   Fontes: "
            f"{', '.join(item['fontes'])}"
        )

        print(
            f"   Motivo: "
            f"{item['motivo']}"
        )

        print(
            f"   Fonte primária: "
            f"{item['fonte_primaria']}"
        )

        print(
            "   Links:"
        )

        for variante in item[
            "variantes"
        ]:
            print(
                f"      - "
                f"{variante['url']}"
            )

    salvar_resultado(
        sinais,
        historias,
        resultados,
        mantidos,
    )


if __name__ == "__main__":
    executar()