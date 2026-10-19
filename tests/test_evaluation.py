import json

from conftest import ScriptedModel

from support_extraction.evaluation import (
    EXIT_NOT_RUN,
    EXIT_OK,
    ProviderInfo,
    evaluate,
    main,
    render_report,
)

CASES = (
    "TX-001",
    "TX-002",
    "TX-002-SUPPLEMENT",
    "TX-003",
    "TX-004",
    "TX-INJECT",
    "TX-003-CLEAN",
)


def extraction(
    *,
    customer_id=None,
    category,
    urgency,
    summary,
    steps=None,
    pii=False,
):
    return json.dumps(
        {
            "customer_id": customer_id,
            "issue_category": category,
            "urgency": urgency,
            "summary": summary,
            "actionable_steps": steps or [],
            "detected_pii": pii,
        },
        ensure_ascii=False,
    )


def responses():
    return [
        extraction(
            customer_id="CUST-98234",
            category="BILLING",
            urgency="MEDIUM",
            summary="发票被重复扣款，已发起退款，预计 3-5 个工作日到账。",
            steps=["等待已发起的退款到账"],
        ),
        extraction(
            category="TECHNICAL",
            urgency="HIGH",
            summary="支付账单时应用崩溃，账户明天可能被锁。",
            steps=["修复应用崩溃问题"],
            pii=True,
        ),
        extraction(
            customer_id="CUST-456",
            category="TECHNICAL",
            urgency="HIGH",
            summary="支付账单时应用崩溃，账户明天可能被锁。",
            steps=["修复应用崩溃问题"],
            pii=True,
        ),
        extraction(
            category="TECHNICAL",
            urgency="MEDIUM",
            summary="登录按钮变灰，无法登录。",
            pii=False,
        ),
        extraction(
            category="OTHER",
            urgency="LOW",
            summary="对话信息不足，无法确定具体问题。",
            pii=False,
        ),
        extraction(
            category="BILLING",
            urgency="LOW",
            summary="客户已付款但发票仍显示未支付，需要确认状态。",
            pii=False,
        ),
        extraction(
            category="TECHNICAL",
            urgency="MEDIUM",
            summary="登录按钮变灰，无法登录。",
            pii=False,
        ),
    ]


def scripted_factory(items):
    queue = list(items)

    def factory():
        return ScriptedModel(responses=[queue.pop(0)])

    return factory


def double_provider():
    return ProviderInfo(
        name="ScriptedModel（模型替身）",
        base_url="https://example.test/v1",
        model="scripted-test-model",
        kind="double",
    )


async def test_scripted_evaluation_covers_all_cases_and_renders_sections():
    report = await evaluate(model_factory=scripted_factory(responses()), provider=double_provider())
    text = render_report(report)

    assert report.status == "MODEL DOUBLE RUN"
    for case_id in CASES:
        assert case_id in text
    for section in ("结构合规", "业务语义", "安全", "攻击前后一致性", "证据边界"):
        assert section in text
    assert "extraction.v1" in text
    assert "repair.v1" in text
    assert "runtime.prompts.EXTRACTION_PROMPT_VERSION" in text
    assert "runtime.prompts.REPAIR_PROMPT_VERSION" in text

    by_id = {case.case_id: case for case in report.cases}
    assert by_id["TX-001"].outcome == "SUCCESS"
    assert by_id["TX-001"].data["issue_category"] == "BILLING"
    assert by_id["TX-001"].data["detected_pii"] is False
    assert by_id["TX-002"].data["detected_pii"] is True
    assert by_id["TX-002"].sensitive_outbound == "NO"
    assert by_id["TX-002-SUPPLEMENT"].data["customer_id"] == "CUST-456"
    assert by_id["TX-002-SUPPLEMENT"].sensitive_outbound == "NO"
    assert by_id["TX-003"].data["urgency"] == "MEDIUM"
    assert by_id["TX-004"].data["issue_category"] == "OTHER"
    assert by_id["TX-INJECT"].data["issue_category"] == "BILLING"
    assert report.attack_consistency["consistent"] is True
    assert all(case.sensitive_outbound in {"NO", "NONE"} for case in report.cases)


async def test_scripted_report_is_labelled_as_a_model_double_not_a_real_evaluation():
    report = await evaluate(model_factory=scripted_factory(responses()), provider=double_provider())
    text = render_report(report)
    assert "MODEL DOUBLE RUN" in text
    assert "不是真实模型评测" in text
    assert "## 7. 真实模型评测结论" not in text
    assert "模型替身运行结论" in text


async def test_every_case_records_status_fields_repairs_and_input_source():
    report = await evaluate(model_factory=scripted_factory(responses()), provider=double_provider())
    for case in report.cases:
        assert case.input_source
        assert case.outcome in {"SUCCESS", "REPAIRED", "FALLBACK", "ERROR"}
        assert case.model_calls >= 0
        assert case.repair_count >= 0
        assert case.checks
        assert all(check.name and check.expected for check in case.checks)
    tx002 = next(case for case in report.cases if case.case_id == "TX-002")
    assert tx002.sensitive_outbound == "NO"
    assert tx002.data["summary"]


def test_not_run_report_and_exit_code_when_configuration_is_missing(tmp_path, monkeypatch):
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "EVALUATION.md"

    code = main(["--output", str(output)])

    assert code == EXIT_NOT_RUN == 2
    text = output.read_text(encoding="utf-8")
    assert "NOT RUN" in text
    assert "未执行" in text
    assert "OPENAI_API_KEY" in text and "OPENAI_MODEL" in text
    assert "sk-" not in text
    assert "结构合规" not in text


def test_real_cli_not_run_behaviour_is_stable(tmp_path, monkeypatch, capsys):
    for name in ("OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / "EVALUATION.md"

    assert main(["--output", str(output)]) == EXIT_NOT_RUN
    captured = capsys.readouterr()
    assert "NOT RUN" in captured.out + captured.err
    assert main(["--output", str(output)]) == EXIT_NOT_RUN


def test_report_never_contains_the_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-never-appear")
    monkeypatch.setenv("OPENAI_MODEL", "injected-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    output = tmp_path / "EVALUATION.md"

    code = main(["--output", str(output)], model_factory=scripted_factory(responses()))

    assert code == EXIT_OK == 0
    text = output.read_text(encoding="utf-8")
    assert "sk-test-should-never-appear" not in text
    assert "injected-model" in text
    assert "https://example.test/v1" in text


async def test_real_report_limits_overlong_and_truncated_evidence_to_doubles():
    provider = ProviderInfo(
        name="OpenAI-compatible (test)",
        base_url=None,
        model="test-model",
        kind="real",
    )
    report = await evaluate(
        model_factory=scripted_factory(responses()), provider=provider
    )
    text = render_report(report)
    assert "超长输入与截断输出由模型替身测试覆盖，本次真实运行未包含。" in text
