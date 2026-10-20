class SCIError(Exception):
    """An actionable protocol or user-input error."""

    def __init__(self, message: str, code: str = "invalid_input", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def invalid(field: str, rule: str) -> SCIError:
    return SCIError(f"{field}: {rule}")
