"""Exercise a real process crash through the public service interface."""

import asyncio
import os
import sys
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel

from support_extraction.service import ExtractionService, Submission


class CrashingModel(BaseChatModel):
    @property
    def _llm_type(self):
        return "crash-test-model"

    def _generate(self, *args, **kwargs):
        raise AssertionError("Use async")

    async def _agenerate(self, *args, **kwargs):
        os._exit(17)


async def main():
    service = await ExtractionService.open(Path(sys.argv[1]), CrashingModel)
    await service.submit(Submission(text="崩溃前已保存的支持对话。"))


if __name__ == "__main__":
    asyncio.run(main())
