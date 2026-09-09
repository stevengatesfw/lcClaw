"""Model API 错误应映射为可识别的运行时异常，而不是一律当成未授权。"""

from copaw.exceptions import convert_model_exception
from agentscope_runtime.engine.schemas.exception import (
    ModelQuotaExceededException,
    UnauthorizedModelAccessException,
)


def test_not_enough_balance_403_is_quota_not_unauthorized():
    wrapped = Exception(
        "Error code: 403 - {'reason': 'NOT_ENOUGH_BALANCE', 'message': 'not enough balance'}"
    )
    wrapped.status_code = 403  # type: ignore[attr-defined]
    result = convert_model_exception(wrapped, "deepseek/deepseek-v4-flash")
    assert isinstance(result, ModelQuotaExceededException)
    assert not isinstance(result, UnauthorizedModelAccessException)
