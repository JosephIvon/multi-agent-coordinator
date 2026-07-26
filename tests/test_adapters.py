from pathlib import Path

from mac.adapters import GenericContextAdapter, discover_adapters


def test_generic_adapter_materializes_portable_context(tmp_path: Path) -> None:
    result = GenericContextAdapter().prepare_context(
        task_id="t-1", context="# Current task\nContinue the implementation.", output_dir=tmp_path
    )
    assert result.path == tmp_path / "task-t-1.md"
    assert result.path.read_text(encoding="utf-8").startswith("# Current task")


def test_builtin_adapter_is_always_available() -> None:
    adapters = discover_adapters()
    assert "generic-context" in adapters
    assert "context_file" in adapters["generic-context"].manifest.capabilities
