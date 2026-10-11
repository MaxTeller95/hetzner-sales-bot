class UserError(Exception):
    """An error whose message (Persian) can be shown to the end user as-is."""


class InsufficientBalance(UserError):
    def __init__(self, required: int, balance: int):
        self.required = required
        self.balance = balance
        super().__init__(f"موجودی کافی نیست. حداقل {required:,} ریال لازم است؛ موجودی شما {balance:,} ریال است.")
