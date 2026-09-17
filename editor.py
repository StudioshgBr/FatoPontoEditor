from pathlib import Path
import os
import requests

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")

PROMPT_FILE = BASE_DIR / "prompts" / "editor.txt"


def carregar_prompt():
    return PROMPT_FILE.read_text(encoding="utf-8")


def analisar_pauta(texto):
    system_prompt = carregar_prompt()

    payload = {
        "model": OLLAMA_MODEL,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": texto,
            },
        ],
        "options": {
            "temperature": 0.2,
        },
    }

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json=payload,
        timeout=300,
    )

    response.raise_for_status()

    data = response.json()

    return data["message"]["content"]


def main():
    print("=" * 60)
    print("EDITOR DE PAUTAS — FATO & PONTO")
    print(f"Modelo: {OLLAMA_MODEL}")
    print("=" * 60)

    print("\nCole abaixo uma notícia, alerta ou informação.")
    print("Digite SAIR para encerrar.\n")

    while True:
        texto = input("PAUTA > ").strip()

        if texto.lower() in {"sair", "exit", "quit"}:
            break

        if not texto:
            continue

        print("\nAnalisando...\n")

        try:
            resultado = analisar_pauta(texto)

            print("-" * 60)
            print(resultado)
            print("-" * 60)

        except requests.exceptions.ConnectionError:
            print(
                "\nERRO: não foi possível conectar ao Ollama.\n"
                "Confirme se o Ollama está em execução."
            )

        except requests.exceptions.Timeout:
            print("\nERRO: o modelo demorou demais para responder.")

        except Exception as exc:
            print(f"\nERRO: {exc}")

        print()


if __name__ == "__main__":
    main()