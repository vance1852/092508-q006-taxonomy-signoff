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


class PublicationBlocked(ServiceError):
    """鉴定稿通过终审前命中了硬性发布阻断条件。"""

    code = "publication_blocked"
    status = 422

    def __init__(self, reasons: list[str]) -> None:
        if not reasons:
            raise ValueError("阻断原因不能为空")
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))
