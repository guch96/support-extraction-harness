"""Local failing model and fixed text for the manual-review demo entry.

This module exists only for :meth:`ExtractionService.seed_review_demo`. It
never opens a network connection, never reads environment variables and is not
part of the normal extraction model configuration.

Every call returns the same schema-invalid payload, so the real Harness stores
one PENDING review after exhausting its repair budget. The payload is fixed so
the demo is repeatable.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

DEMO_TEXT = "演示：客户反馈支付页面一直转圈，订单无法完成，要求尽快处理。"
DEMO_INVALID_OUTPUT = '{"customer_id": null, "issue_category": "TECHICAL"}'


class DemoFailureModel(BaseChatModel):
    """A deterministic local model that always returns invalid extraction JSON."""

    @property
    def _llm_type(self) -> str:
        return "review-demo-failure-model"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("Use the asynchronous model path")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=DEMO_INVALID_OUTPUT))]
        )
