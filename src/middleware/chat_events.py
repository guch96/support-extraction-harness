from langchain.agents.middleware import AgentMiddleware

from streaming.events import Phase
from streaming.phase_sink import PhaseSink


class ChatEventsMiddleware(AgentMiddleware):
    """Report chat phases only; validation and jumps have a different owner."""

    def __init__(self, sink: PhaseSink | None = None):
        self.sink = sink if sink is not None else PhaseSink()

    async def awrap_model_call(self, request, handler):
        ctx = request.runtime.context
        if ctx.feedback:
            self.sink.emit(Phase.REPAIRING, "正在修复")
        self.sink.emit(Phase.CALLING_MODEL, "正在调用模型")
        response = await handler(request)
        self.sink.emit(Phase.VALIDATING, "正在校验输出")
        return response
