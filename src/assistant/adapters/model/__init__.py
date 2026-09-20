"""Model adapters: the only place a provider's API is spoken (ADR-0017).

`fake.py` implements the port for deterministic tests; `deepseek.py` implements it against the
DeepSeek Responses API. Nothing outside this package may import a provider SDK, know a base
URL, or see a credential.
"""

from assistant.adapters.model.deepseek import DEEPSEEK_BASE_URL, DeepSeekAdapter
from assistant.adapters.model.fake import FakeModelAdapter

__all__ = ["DEEPSEEK_BASE_URL", "DeepSeekAdapter", "FakeModelAdapter"]
