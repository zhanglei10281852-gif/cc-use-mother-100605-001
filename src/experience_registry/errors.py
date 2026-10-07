"""知识库领域错误。"""


class RegistryError(Exception):
    """所有业务错误的基类，附带稳定的 HTTP 状态码。"""

    status_code = 400
    code = "bad_request"


class ValidationError(RegistryError):
    status_code = 400
    code = "validation_error"


class AuthError(RegistryError):
    status_code = 401
    code = "unauthorized"


class PermissionDenied(RegistryError):
    status_code = 403
    code = "forbidden"


class NotFound(RegistryError):
    status_code = 404
    code = "not_found"


class Conflict(RegistryError):
    status_code = 409
    code = "conflict"
