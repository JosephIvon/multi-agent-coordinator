

from mac.bootstrap import BEGIN, bootstrap_project


def test_bootstrap_generates_thin_idempotent_ide_entries(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Existing facts\n", encoding="utf-8")
    first = bootstrap_project(tmp_path)
    second = bootstrap_project(tmp_path)
    assert first == second
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / ".cursor" / "mcp.json").exists()
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / "opencode.json").exists()
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# Existing facts" in claude
    assert claude.count(BEGIN) == 1
    for path in first:
        assert "task state" not in path.read_text(encoding="utf-8").lower() if path.suffix in {".md", ".mdc"} else True


def test_bootstrap_is_byte_idempotent_and_preserves_frontmatter(tmp_path):
    bootstrap_project(tmp_path)
    files = [tmp_path / "AGENTS.md", tmp_path / ".cursor" / "rules" / "mac.mdc",
             tmp_path / ".opencode" / "rules" / "mac.md"]
    first = {path: path.read_bytes() for path in files}
    bootstrap_project(tmp_path)
    second = {path: path.read_bytes() for path in files}
    assert first == second
    assert first[files[1]].decode().count("description: MAC coordination entry point") == 1
