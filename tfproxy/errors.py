from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ProxyError(Exception):
    message: str
    status_code: int = 500
    type: str = "proxy_error"
    code: Optional[str] = None
    param: Optional[str] = None
    upstream_status: Optional[int] = None
    upstream_body: Any = None

    def to_openai_error(self) -> dict:
        err = {
            "message": self.message,
            "type": self.type,
            "param": self.param,
            "code": self.code or (str(self.upstream_status) if self.upstream_status else None),
        }
        return {"error": err}


class AuthError(ProxyError):
    def __init__(self, message: str = "Unauthorized", **kw: Any):
        super().__init__(message=message, status_code=401, type="authentication_error", **kw)


class UpstreamError(ProxyError):
    def __init__(self, message: str, upstream_status: Optional[int] = None, upstream_body: Any = None, **kw: Any):
        super().__init__(
            message=message,
            status_code=502 if (upstream_status or 500) >= 500 else 424,
            type="upstream_error",
            upstream_status=upstream_status,
            upstream_body=upstream_body,
            **kw,
        )


class BadRequestError(ProxyError):
    def __init__(self, message: str, param: Optional[str] = None, **kw: Any):
        super().__init__(message=message, status_code=400, type="invalid_request_error", param=param, **kw)


class NotFoundError(ProxyError):
    def __init__(self, message: str = "Not found", **kw: Any):
        super().__init__(message=message, status_code=404, type="invalid_request_error", **kw)
