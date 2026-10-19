from langchain_core.exceptions import (
    ModelAPIError,
    ModelAuthenticationError,
    ModelConnectionError,
    ModelInvalidRequestError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)


class ModelCallFailure(Exception):
    pass


CONFIG_ERROR_TYPES = (ModelInvalidRequestError, ModelNotFoundError)
CALL_FAILURE_TYPES = (
    ModelAuthenticationError,
    ModelPermissionDeniedError,
    ModelConnectionError,
    ModelRateLimitError,
    ModelAPIError,
)


def model_error_code(exc: Exception) -> str | None:
    if isinstance(exc, (TimeoutError, ModelTimeoutError)):
        return "MODEL_TIMEOUT"
    if isinstance(exc, CONFIG_ERROR_TYPES):
        return "MODEL_CONFIG_ERROR"
    if isinstance(exc, CALL_FAILURE_TYPES):
        return "MODEL_CALL_FAILED"
    if isinstance(exc, ConnectionError) or type(exc).__module__.startswith(("openai", "httpx")):
        return "MODEL_CALL_FAILED"
    return None
