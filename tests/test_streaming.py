from streaming.events import ChatEvent, Phase
from streaming.phase_sink import PhaseSink


def test_chat_event_carries_sequence_phase_and_label():
    chat_event = ChatEvent(seq=7, phase=Phase.VALIDATING, label="正在校验")

    assert chat_event.seq == 7
    assert chat_event.phase is Phase.VALIDATING
    assert chat_event.label == "正在校验"
    assert [phase.value for phase in Phase] == [
        "reading",
        "calling_model",
        "repairing",
        "validating",
        "succeeded",
        "failed",
    ]


async def test_phase_sink_emits_monotonic_sequence_and_consumes_in_order():
    sink = PhaseSink()

    first = sink.emit(Phase.CALLING_MODEL, "正在调用模型")
    second = sink.emit(Phase.REPAIRING, "正在修复")

    assert [first.seq, second.seq] == [1, 2]
    assert await sink.next_event() == first
    assert await sink.next_event() == second
