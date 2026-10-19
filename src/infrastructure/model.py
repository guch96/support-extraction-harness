import os

from langchain_openai import ChatOpenAI

from harness.contracts import ServiceError

MODEL_PARAMETERS = {
    "max_retries": 0,
    "timeout": 30,
    "temperature": 0,
    "max_tokens": 2048,
    "streaming": False,
}


def configured_model():
    key, model = os.getenv("OPENAI_API_KEY"), os.getenv("OPENAI_MODEL")
    if not key or not model:
        raise ServiceError("MODEL_NOT_CONFIGURED", "")
    return ChatOpenAI(
        model=model,
        api_key=key,
        base_url=os.getenv("OPENAI_BASE_URL") or None,
        **MODEL_PARAMETERS,
    )
