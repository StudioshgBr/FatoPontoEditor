from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
ERROR_LOG = DATA_DIR / "codex_errors.log"
CODEX_COMMAND = os.getenv("CODEX_COMMAND", "codex")

# O modo estrito com --output-schema fica desligado por padrão porque a
# aplicação já valida o JSON em Python e faz retry dos itens inválidos.
# Se quisermos reativá-lo depois, basta CODEX_STRICT_SCHEMA=true no .env.
CODEX_STRICT_SCHEMA = os.getenv("CODEX_STRICT_SCHEMA", "false").strip().lower() in {
    "1", "true", "yes", "sim",
}


def _log_error(context: str, details: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open("a", encoding="utf-8") as fh:
            fh.write(
                f"\n[{datetime.now().isoformat(timespec='seconds')}] {context}\n"
                f"{details.strip()}\n"
                + "-" * 80
                + "\n"
            )
    except Exception:
        pass


def _resolve_codex() -> str:
    resolved = shutil.which(CODEX_COMMAND)
    if not resolved:
        raise RuntimeError(
            "Codex CLI não encontrado no PATH. Abra o PowerShell e confirme com: codex --version"
        )
    return resolved


def _creationflags() -> int:
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        return subprocess.CREATE_NO_WINDOW
    return 0


def _command_with_args(args: list[str]) -> list[str]:
    resolved = _resolve_codex()

    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        line = subprocess.list2cmdline([resolved, *args])
        return ["cmd.exe", "/d", "/s", "/c", line]

    return [resolved, *args]


def _run_simple(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command_with_args(args),
        cwd=BASE_DIR,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
        creationflags=_creationflags(),
        env=os.environ.copy(),
    )


def codex_version() -> str:
    result = _run_simple(["--version"])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "Erro ao consultar Codex").strip())
    return result.stdout.strip()


def codex_login_status() -> str:
    result = _run_simple(["login", "status"])
    text = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise RuntimeError(text or "Codex não está autenticado.")
    return text


def codex_model_status(model: str) -> str:
    result = _run_simple(["debug", "models"], timeout=60)
    if result.returncode != 0:
        return "catálogo não pôde ser consultado"
    text = (result.stdout or "") + "\n" + (result.stderr or "")
    return "disponível" if model in text else "não listado (catálogo informativo)"


def codex_healthcheck(model: str = "gpt-5.6-luna") -> dict[str, str]:
    return {
        "version": codex_version(),
        "login": codex_login_status(),
        "model": codex_model_status(model),
    }


def _parse_json_output(text: str) -> Any:
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fallback caso o modelo acrescente cercas Markdown.
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            inner = "\n".join(lines[1:-1]).strip()
            if inner.lower().startswith("json\n"):
                inner = inner[5:]
            try:
                return json.loads(inner)
            except json.JSONDecodeError:
                pass

    # Último fallback: recorta o primeiro objeto JSON completo aparente.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])

    raise json.JSONDecodeError("Resposta do Codex não contém JSON válido", text, 0)


def run_codex_json(
    *,
    model: str,
    system_prompt: str,
    user_payload: Any,
    schema_path: Path,
    timeout: int = 600,
) -> Any:
    """Executa o Codex autenticado pelo ChatGPT e retorna JSON.

    Por padrão, não usa --output-schema. A aplicação já possui validação
    estrutural própria e retries; isso deixa o caminho compatível com o mesmo
    `codex exec` que foi validado manualmente no PowerShell.

    O agente continua read-only e efêmero.
    """

    if not schema_path.exists():
        raise RuntimeError(f"Schema não encontrado: {schema_path}")

    payload_text = (
        user_payload
        if isinstance(user_payload, str)
        else json.dumps(user_payload, ensure_ascii=False, indent=2)
    )

    full_prompt = (
        "Você está trabalhando como componente interno do Editor Local do portal Fato & Ponto.\n"
        "Não altere arquivos, não execute comandos e não escreva código.\n"
        "Faça somente a tarefa editorial solicitada usando os dados fornecidos.\n\n"
        "INSTRUÇÕES EDITORIAIS:\n"
        f"{system_prompt.strip()}\n\n"
        "DADOS DE ENTRADA:\n"
        f"{payload_text}\n\n"
        "IMPORTANTE: retorne SOMENTE JSON válido, sem Markdown, sem comentários e sem texto antes ou depois do JSON."
    )

    fd, output_name = tempfile.mkstemp(prefix="fp_codex_", suffix=".json")
    os.close(fd)
    output_path = Path(output_name)

    args = [
        "exec",
        "--model", model,
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        "--ephemeral",
    ]

    if CODEX_STRICT_SCHEMA:
        args.extend(["--output-schema", str(schema_path)])

    args.extend([
        "--output-last-message", str(output_path),
        "-",
    ])

    try:
        result = subprocess.run(
            _command_with_args(args),
            cwd=BASE_DIR,
            input=full_prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            creationflags=_creationflags(),
            env=os.environ.copy(),
        )

        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            _log_error("codex exec retornou erro", details or "sem detalhes")
            if len(details) > 3000:
                details = details[-3000:]
            raise RuntimeError(
                "Falha ao executar o Codex. "
                f"Saída: {details or 'sem detalhes'}"
            )

        if not output_path.exists():
            _log_error("codex exec sem arquivo de saída", result.stderr or result.stdout or "")
            raise RuntimeError("Codex terminou sem gerar a resposta final esperada.")

        text = output_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            _log_error("codex exec retornou saída vazia", result.stderr or result.stdout or "")
            raise RuntimeError("Codex retornou resposta vazia.")

        try:
            return _parse_json_output(text)
        except Exception as exc:
            _log_error(
                "falha ao interpretar JSON do Codex",
                f"Erro: {exc}\n\nResposta final:\n{text}",
            )
            raise RuntimeError(
                "O Codex respondeu, mas a resposta final não pôde ser interpretada como JSON. "
                "Veja data\\codex_errors.log."
            ) from exc

    except subprocess.TimeoutExpired as exc:
        _log_error("timeout do Codex", str(exc))
        raise RuntimeError(f"Codex excedeu o tempo limite de {timeout} segundos.") from exc

    finally:
        try:
            output_path.unlink(missing_ok=True)
        except Exception:
            pass
