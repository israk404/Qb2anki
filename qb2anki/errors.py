"""Exceptions that must be importable even when optional packages are broken."""


class GeminiError(RuntimeError):
    pass


class QuotaExhausted(GeminiError):
    """The daily quota of a model is used up. Retrying today is pointless: stop and resume after the reset."""


class NoQuotaForModel(GeminiError):
    """The project has no quota at all for this model (typical for Pro models on the free tier)."""


class KeyRejected(GeminiError):
    """Google says this API key is invalid or expired. The key is retired; another one can be used."""


class KeyBlocked(KeyRejected):
    """Google refused this key's project (permission denied / suspended / restricted)."""


class KeysFlagged(QuotaExhausted):
    """Too many keys were blocked by Google. Rotation stops so the remaining keys are left alone."""
