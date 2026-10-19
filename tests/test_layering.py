from pathlib import Path

from support_extraction.policy import contract


def test_application_contract_injects_repair_prompt_and_budget():
    app_contract = contract()
    assert "{{repair_nonce}}" in app_contract.repair_prompt
    assert "{{repair_packet_json}}" in app_contract.repair_prompt
    assert "repair_feedback_" in app_contract.repair_prompt
    assert app_contract.max_model_calls == 3


def test_runtime_has_no_domain_vocabulary():
    runtime = Path(__file__).resolve().parents[1] / "src" / "harness" / "runtime.py"
    assert "六字段" not in runtime.read_text()
