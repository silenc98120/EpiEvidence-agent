from pathlib import Path

from app.core.mesh_normalizer import MeshRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MESH_DATABASE_PATH = PROJECT_ROOT / "data" / "mesh.sqlite3"

def test_final_exact_returns_preferred_label_and_entry_terms()->None:
    repository = MeshRepository(MESH_DATABASE_PATH)

    matches = repository.find_exact(" Semaglutide ")
    assert len(matches) == 1

    match = matches[0]
    assert match.mesh_id == "D000099194"
    assert match.preferred_label == "Semaglutide"
    assert match.matched_term == "Semaglutide"
    assert match.matched_term_type == "preferred"
    assert set(match.entry_terms) == {
        "Ozempic",
        "Rybelsus",
        "Wegovy",
    }