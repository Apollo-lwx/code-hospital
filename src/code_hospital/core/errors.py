"""统一错误结构（契约 §0 / 设计 §6.1）。

error = {code, message, target, nextAction}；nextAction 引导宿主自愈。
"""

from __future__ import annotations

from typing import Literal

ErrorCode = Literal[
    "BadArgument",
    "NotFound",
    "InvalidOperation",
    "Conflict",
    "Overlimit",
    "Forbidden",
    "InternalError",
]


class HospitalError(Exception):
    """业务可预期错误。工具层捕获后以 {"error": payload} 结构化返回。"""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        target: str = "",
        next_action: str = "",
    ) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.target = target
        self.next_action = next_action

    def payload(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "target": self.target,
                "nextAction": self.next_action,
            }
        }


def bad_argument(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("BadArgument", message, target, next_action)


def not_found(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("NotFound", message, target, next_action)


def invalid_operation(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("InvalidOperation", message, target, next_action)


def conflict(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("Conflict", message, target, next_action)


def forbidden(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("Forbidden", message, target, next_action)


def internal_error(message: str, target: str = "", next_action: str = "") -> HospitalError:
    return HospitalError("InternalError", message, target, next_action)
