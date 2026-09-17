from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "editor.db"


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"Banco não encontrado: {DB_PATH}")

    backup_path = DATA_DIR / f"editor_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

    source = sqlite3.connect(DB_PATH)
    source.row_factory = sqlite3.Row

    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()

    rows = source.execute(
        "SELECT id FROM stories WHERE group_name = 'VALIDACAO_PENDENTE'"
    ).fetchall()
    story_ids = [row["id"] for row in rows]

    if not story_ids:
        print("Nenhuma pauta VALIDACAO_PENDENTE encontrada.")
        print(f"Backup criado em: {backup_path}")
        source.close()
        return

    placeholders = ",".join("?" for _ in story_ids)

    urls = source.execute(
        f"SELECT DISTINCT canonical_url FROM story_sources WHERE story_id IN ({placeholders})",
        story_ids,
    ).fetchall()
    canonical_urls = [row["canonical_url"] for row in urls]

    requeued = 0
    if canonical_urls:
        url_placeholders = ",".join("?" for _ in canonical_urls)
        cursor = source.execute(
            f"UPDATE signals SET processed = 0 WHERE canonical_url IN ({url_placeholders})",
            canonical_urls,
        )
        requeued = cursor.rowcount

    source.execute(
        f"DELETE FROM drafts WHERE story_id IN ({placeholders})",
        story_ids,
    )
    source.execute(
        f"DELETE FROM story_sources WHERE story_id IN ({placeholders})",
        story_ids,
    )
    source.execute(
        f"DELETE FROM stories WHERE id IN ({placeholders})",
        story_ids,
    )
    source.commit()
    source.close()

    print("Reprocessamento preparado com sucesso.")
    print(f"Pautas VALIDACAO_PENDENTE removidas: {len(story_ids)}")
    print(f"Sinais recolocados na fila: {requeued}")
    print(f"Backup criado em: {backup_path}")
    print("\nAgora abra o Editor e clique em 'Rodar triagem'.")


if __name__ == "__main__":
    main()
