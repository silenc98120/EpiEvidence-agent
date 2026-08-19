"""该 Script 用于从 MeSH.csv.gz中提取 MeSH 数据到 Sqlite3数据库中。
"""
import csv
import gzip
import os
import sys
import sqlite3
import unicodedata
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = PROJECT_ROOT / "data" / "MESH.csv.gz"
DATABASE_PATH = PROJECT_ROOT / "data" / "mesh.sqlite3"

TABLE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE mesh_concepts (
    mesh_id TEXT PRIMARY KEY,
    preferred_label TEXT NOT NULL,
    cui TEXT,
    obsolete INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE mesh_terms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mesh_id TEXT NOT NULL,
    term TEXT NOT NULL,
    normalized_term TEXT NOT NULL,
    term_type TEXT NOT NULL
        CHECK (term_type IN ('preferred', 'entry')),
    FOREIGN KEY (mesh_id) REFERENCES mesh_concepts(mesh_id),
    UNIQUE (mesh_id, normalized_term)
);
"""

INDEX_SCHEMA = """
CREATE INDEX idx_mesh_terms_normalized
ON mesh_terms(normalized_term);
"""


def normalize_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.casefold().split())


def import_mesh(
    connection: sqlite3.Connection,
    source_path: Path,
) -> tuple[int, int]:
    concept_count = 0
    term_count = 0

    csv.field_size_limit(sys.maxsize)

    with gzip.open(
        source_path,
        mode="rt",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            preferred_label = row["Preferred Label"].strip()
            if not preferred_label:
                continue

            class_id = row["Class ID"].strip()
            mesh_id = class_id.rsplit("/", maxsplit=1)[-1]
            cui = (row.get("CUI") or "").strip() or None
            obsolete = int(
                (row.get("Obsolete") or "").upper() == "TRUE"
            )

            connection.execute(
                """
                INSERT INTO mesh_concepts (
                    mesh_id, preferred_label, cui, obsolete
                ) VALUES (?, ?, ?, ?)
                """,
                (mesh_id, preferred_label, cui, obsolete),
            )
            concept_count += 1

            terms = {
                normalize_term(preferred_label): (
                    preferred_label,
                    "preferred",
                )
            }

            for synonym in (row.get("Synonyms") or "").split("|"):
                synonym = synonym.strip()
                if synonym:
                    terms.setdefault(
                        normalize_term(synonym),
                        (synonym, "entry"),
                    )

            connection.executemany(
                """
                INSERT INTO mesh_terms (
                    mesh_id, term, normalized_term, term_type
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (mesh_id, term, normalized, term_type)
                    for normalized, (term, term_type) in terms.items()
                ],
            )
            term_count += len(terms)

    return concept_count, term_count

def build_mesh_index() -> None:
    temporary_path = DATABASE_PATH.with_suffix(".sqlite3.tmp")

    if temporary_path.exists():
        temporary_path.unlink()

    connection = sqlite3.connect(temporary_path)

    try:
        connection.executescript(TABLE_SCHEMA)

        concept_count, term_count = import_mesh(
            connection,
            SOURCE_PATH,
        )

        connection.executescript(INDEX_SCHEMA)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    os.replace(temporary_path, DATABASE_PATH)

    print(f"concepts={concept_count}, terms={term_count}")
    print(f"database={DATABASE_PATH}")


if __name__ == "__main__":
    build_mesh_index()