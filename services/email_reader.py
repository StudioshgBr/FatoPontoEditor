import imaplib
import os
from email import message_from_bytes
from email.header import decode_header
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

MAIL_HOST = os.getenv("MAIL_HOST")
MAIL_PORT = os.getenv("MAIL_PORT", "993")
MAIL_USER = os.getenv("MAIL_USER")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_FOLDER = os.getenv("MAIL_FOLDER", "INBOX")


def decode_text(value):
    if not value:
        return ""

    parts = decode_header(value)
    result = []

    for text, encoding in parts:
        if isinstance(text, bytes):
            result.append(
                text.decode(encoding or "utf-8", errors="replace")
            )
        else:
            result.append(text)

    return "".join(result)


def conectar():
    missing = [
        name
        for name, value in {
            "MAIL_HOST": MAIL_HOST,
            "MAIL_USER": MAIL_USER,
            "MAIL_PASSWORD": MAIL_PASSWORD,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Configura\u00e7\u00e3o de e-mail incompleta. Defina "
            + ", ".join(missing)
            + " no arquivo .env."
        )

    try:
        port = int(MAIL_PORT)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("MAIL_PORT deve conter uma porta num\u00e9rica v\u00e1lida.") from exc

    connection = imaplib.IMAP4_SSL(
        MAIL_HOST,
        port,
    )

    connection.login(
        MAIL_USER,
        MAIL_PASSWORD,
    )

    connection.select(
        MAIL_FOLDER,
        readonly=True,
    )

    return connection


def extrair_html(message):
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(
                part.get("Content-Disposition", "")
            ).lower()

            if content_type == "text/html" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)

                if payload:
                    charset = part.get_content_charset() or "utf-8"

                    return payload.decode(
                        charset,
                        errors="replace",
                    )

    elif message.get_content_type() == "text/html":
        payload = message.get_payload(decode=True)

        if payload:
            charset = message.get_content_charset() or "utf-8"

            return payload.decode(
                charset,
                errors="replace",
            )

    return ""


def extrair_texto(message):
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(
                part.get("Content-Disposition", "")
            ).lower()

            if content_type == "text/plain" and "attachment" not in disposition:
                payload = part.get_payload(decode=True)

                if payload:
                    charset = part.get_content_charset() or "utf-8"

                    return payload.decode(
                        charset,
                        errors="replace",
                    )

    elif message.get_content_type() == "text/plain":
        payload = message.get_payload(decode=True)

        if payload:
            charset = message.get_content_charset() or "utf-8"

            return payload.decode(
                charset,
                errors="replace",
            )

    return ""


def limpar_url_google(url):
    """
    Alguns links do Google Alerts passam por URLs do Google.
    Quando houver um parâmetro com a URL original, tenta recuperá-la.
    """

    try:
        parsed = urlparse(url)

        if "google." not in parsed.netloc.lower():
            return url

        params = parse_qs(parsed.query)

        for key in ("url", "q"):
            values = params.get(key)

            if values:
                candidate = unquote(values[0])

                if candidate.startswith(("http://", "https://")):
                    return candidate

    except Exception:
        pass

    return url


def link_relevante(url):
    if not url:
        return False

    lowered = url.lower()

    ignorar = (
        "google.com/alerts",
        "google.com/preferences",
        "support.google.com",
        "accounts.google.com",
        "policies.google.com",
        "unsubscribe",
        "mailto:",
    )

    return not any(item in lowered for item in ignorar)


def extrair_links_alerta(html):
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")

    links = []
    vistos = set()

    for tag in soup.find_all("a", href=True):
        titulo = tag.get_text(" ", strip=True)
        url = limpar_url_google(tag["href"].strip())

        if not titulo:
            continue

        if not url.startswith(("http://", "https://")):
            continue

        if not link_relevante(url):
            continue

        chave = (titulo, url)

        if chave in vistos:
            continue

        vistos.add(chave)

        links.append(
            {
                "titulo": titulo,
                "url": url,
            }
        )

    return links


def listar_alertas_nao_lidos(limit=20):
    connection = conectar()

    resultados = []

    try:
        status, data = connection.search(
            None,
            "UNSEEN",
        )

        if status != "OK":
            raise RuntimeError(
                "Não foi possível procurar mensagens."
            )

        message_ids = data[0].split()

        print(f"\nMensagens não lidas: {len(message_ids)}\n")

        for message_id in message_ids[-limit:]:
            status, message_data = connection.fetch(
                message_id,
                "(BODY.PEEK[])",
            )

            if status != "OK":
                continue

            raw_message = None

            for item in message_data:
                if (
                    isinstance(item, tuple)
                    and len(item) >= 2
                    and isinstance(item[1], bytes)
                ):
                    raw_message = item[1]
                    break

            if not raw_message:
                continue

            message = message_from_bytes(raw_message)

            subject = decode_text(
                message.get("Subject")
            )

            sender = decode_text(
                message.get("From")
            )

            date = decode_text(
                message.get("Date")
            )

            html = extrair_html(message)
            texto = extrair_texto(message)

            links = extrair_links_alerta(html)

            alerta = {
                "id": message_id.decode(),
                "sender": sender,
                "date": date,
                "subject": subject,
                "texto": texto,
                "links": links,
            }

            resultados.append(alerta)

            print("=" * 70)
            print(f"ID: {alerta['id']}")
            print(f"DE: {alerta['sender']}")
            print(f"DATA: {alerta['date']}")
            print(f"ASSUNTO: {alerta['subject']}")
            print()

            if not links:
                print("Nenhum link jornalístico encontrado.")
            else:
                print(f"RESULTADOS ENCONTRADOS: {len(links)}")

                for index, link in enumerate(links, start=1):
                    print()
                    print(f"{index}. {link['titulo']}")
                    print(f"   {link['url']}")

            print()

        return resultados

    finally:
        try:
            connection.close()
        except Exception:
            pass

        connection.logout()


if __name__ == "__main__":
    try:
        listar_alertas_nao_lidos()

    except imaplib.IMAP4.error as exc:
        print(f"Erro IMAP/autenticação: {exc}")

    except Exception as exc:
        print(f"Erro: {exc}")
