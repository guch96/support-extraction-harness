import re
from pathlib import Path

from runtime import engine
from runtime.engine import Harness
from support_extraction.policy import contract


def test_application_contract_injects_repair_prompt_and_budget():
    app_contract = contract()
    assert "{{repair_nonce}}" in app_contract.repair_prompt
    assert "{{repair_packet_json}}" in app_contract.repair_prompt
    assert "repair_feedback_" in app_contract.repair_prompt
    assert app_contract.max_model_calls == 3


def test_runtime_has_no_domain_vocabulary():
    runtime = Path(__file__).resolve().parents[1] / "src" / "runtime" / "engine.py"
    assert "六字段" not in runtime.read_text()


def test_agent_is_built_without_tools(monkeypatch):
    captured = {}

    def fake_create_agent(**options):
        captured.update(options)
        return object()

    monkeypatch.setattr(engine, "create_agent", fake_create_agent)
    Harness(object(), contract(), object())

    assert captured["tools"] == []


def test_runtime_and_middleware_register_no_tools():
    root = Path(__file__).resolve().parents[1] / "src"
    for directory in ("runtime", "middleware"):
        for path in sorted((root / directory).glob("*.py")):
            text = path.read_text()
            assert "BaseTool" not in text, path
            assert "bind_tools" not in text, path
            assert not re.search(r"tools\s*=\s*(?!\[\])", text), path
