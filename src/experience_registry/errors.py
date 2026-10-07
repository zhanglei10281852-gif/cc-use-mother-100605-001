"""统一的业务异常类型。

错误码（code）对外保持稳定，CLI 与 HTTP 层都依赖它给出确定性结果，
调用方不应解析 message 文本。
"""


class KnowledgeError(Exception):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(self, message, *, detail=None):
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ValidationError(KnowledgeError):
    code = "VALIDATION_ERROR"
    http_status = 422


class AuthenticationError(KnowledgeError):
    code = "AUTHENTICATION_FAILED"
    http_status = 401


class PermissionDeniedError(KnowledgeError):
    code = "PERMISSION_DENIED"
    http_status = 403


class NotFoundError(KnowledgeError):
    code = "NOT_FOUND"
    http_status = 404


class ConflictError(KnowledgeError):
    """并发或状态冲突。code 由调用处指定，如 VERSION_CONFLICT。"""

    http_status = 409

    def __init__(self, message, *, code="CONFLICT", detail=None):
        super().__init__(message, detail=detail)
        self.code = code
