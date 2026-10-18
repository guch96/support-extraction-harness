import json
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr


class ScriptedModel(BaseChatModel):
    responses: list[Any]
    _calls: list[list[Any]] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "scripted-test-model"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise AssertionError("Use the asynchronous model path")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls.append(messages)
        response = self.responses[len(self._calls) - 1]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = await response()
        if not isinstance(response, AIMessage):
            response = AIMessage(content=response)
        return ChatResult(generations=[ChatGeneration(message=response)])

    @property
    def calls(self):
        return self._calls


@pytest.fixture
def valid_data():
    return {
        "customer_id": "CUST-98234",
        "issue_category": "BILLING",
        "urgency": "MEDIUM",
        "summary": "重复扣款，退款已发起，预计 3-5 个工作日到账。",
        "actionable_steps": ["等待已发起的退款到账"],
        "detected_pii": False,
    }


@pytest.fixture
def valid_json(valid_data):
    return json.dumps(valid_data, ensure_ascii=False)
