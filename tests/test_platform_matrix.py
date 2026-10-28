"""Keep the execution matrix tied to real packs and source locations."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_generation_platform_matrix_references_existing_packs_and_sources():
    content = (ROOT / "docs/reference/where-things-run.md").read_text(encoding="utf-8")
    rows = [line.split("|")[1:-1] for line in content.splitlines() if line.startswith("| `")]
    assert rows, "matrix has no pack rows"
    for row in rows:
        pack = re.match(r"\s*`([a-z0-9_]+)`", row[0]).group(1)
        assert (ROOT / "astrid/packs" / pack / "pack.yaml").is_file(), pack
        assert len(row) == 5, "expected pack plus four execution locations"
        for cell in row:
            references = re.findall(r"\]\(\.\./\.\./([^)#]+)#L(\d+)\)", cell)
            assert references, f"matrix cell lacks file:line evidence: {cell}"
            for source, line in references:
                path = ROOT / source
                assert path.is_file(), source
                assert 0 < int(line) <= len(path.read_text(encoding="utf-8").splitlines()), (source, line)
    for source in ("docs/getting-started.md", "astrid/packs/_core/docs/SKILL.md"):
        assert "reference/where-things-run.md" in (ROOT / source).read_text(encoding="utf-8")
