class ProxyError(Exception):
    def __init__(self, code: str, message: str, status: int = 502, *, safe_fallback=False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.safe_fallback = safe_fallback

    def body(self):
        return {
            "error": {
                "message": self.message,
                "type": "invalid_request_error"
                if self.status in (400, 404, 413, 422)
                else "api_error",
                "param": None,
                "code": self.code,
            }
        }


def invalid(message: str):
    return ProxyError("invalid_request", message, 400)
