"""Token counting with tiktoken's ``cl100k_base``.

Gemini uses its own tokenizer, so these counts are an approximation used for
chunk sizing and the embedding input limit.

tiktoken downloads the encoding file once and caches it. Unless
``TIKTOKEN_CACHE_DIR`` is set, the cache lives in ``backend/.cache/tiktoken`` so
it survives reboots (the default is a temp directory).
"""

import os
from functools import lru_cache

import tiktoken

from app.core.config import BACKEND_DIR

ENCODING_NAME = "cl100k_base"

os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(BACKEND_DIR / ".cache" / "tiktoken"))


@lru_cache(maxsize=1)
def get_encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    return len(get_encoding().encode(text, disallowed_special=()))


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Return ``text`` cut to at most ``max_tokens`` tokens."""
    encoding = get_encoding()
    tokens = encoding.encode(text, disallowed_special=())
    if len(tokens) <= max_tokens:
        return text
    return encoding.decode(tokens[:max_tokens])
