"""Token 估算。

DeepSeek / GLM / Gemini 的 tokenizer 無法統一取得，一律以 tiktoken o200k_base 估算；
實際用量以 API 回傳的 usage 為準。tiktoken 首次使用需下載編碼檔，失敗時改用字元數估算。
"""

from functools import lru_cache

# OpenAI chat 格式每則訊息的額外開銷（角色標記與分隔）
MESSAGE_OVERHEAD = 4
REPLY_PRIMING = 3


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return None


@lru_cache(maxsize=4096)
def count(text: str) -> int:
    if not text:
        return 0
    enc = _encoder()
    if enc is not None:
        return len(enc.encode(text, disallowed_special=()))
    # 後備：CJK 約 1 字 1 token，其餘約 4 字元 1 token
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    return cjk + (len(text) - cjk + 3) // 4


def count_message(content: str) -> int:
    return count(content) + MESSAGE_OVERHEAD
