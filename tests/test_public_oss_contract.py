"""Public OSS metadata and documented runtime-provider contracts."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_GEMMA_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_CLIENT_FILES = (
    "src/alias_generator.py",
    "src/memory_compiler.py",
    "src/memory_extractor.py",
    "src/memory_indexer.py",
    "src/query_intent.py",
    "src/search.py",
)


def test_public_gemma_model_matches_installer_and_clients():
    assert PUBLIC_GEMMA_MODEL in (ROOT / "install.sh").read_text()
    assert PUBLIC_GEMMA_MODEL in (ROOT / "scripts/gemma_server_runner.sh").read_text()

    for relative_path in GEMMA_CLIENT_FILES:
        body = (ROOT / relative_path).read_text()
        assert PUBLIC_GEMMA_MODEL in body, relative_path
        assert "gemma-4-12B-it-4bit" not in body, relative_path


def test_readme_matches_sessionstart_provider_and_license():
    readme = (ROOT / "README.md").read_text()
    session_memory = (ROOT / "src/session_memory.py").read_text()

    assert "Claude Code Haiku로 요약" in readme
    assert 'CLAUDE_MODEL = "haiku"' in session_memory
    assert "[LICENSE](LICENSE)" in readme
    assert (ROOT / "LICENSE").is_file()
