"""服务层可观察错误。"""


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class NotFound(ServiceError):
    code = "not_found"
    status = 404


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class Forbidden(ServiceError):
    code = "forbidden"
    status = 403


class InvalidState(ServiceError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ServiceError):
    code = "validation_failed"
    status = 422


class PublishBlocked(ServiceError):
    """鉴定稿因利益回避、证据缺口或学名冲突不能发布。"""

    code = "determination_publish_blocked"
    status = 422

    def __init__(self, reasons: list[dict[str, str]]) -> None:
        self.reasons = reasons
        summary = "；".join(reason["message"] for reason in reasons)
        super().__init__(f"鉴定稿不能发布: {summary}")
