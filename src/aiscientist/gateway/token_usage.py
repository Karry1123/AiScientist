"""Input-token accounting shared by the run log and persisted call records."""

from __future__ import annotations


def split_usage(usage: dict) -> tuple[int, int, int, int, int]:
    """(New input, 5-minute writes, 1-hour writes, cache reads, output).

    Anthropic reports new input separately; OpenAI includes cached input in
    ``prompt_tokens``. Keep cache creation separate, as the usage record does.
    """
    details = usage.get("prompt_tokens_details") or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    reads = int(usage.get("cache_read_input_tokens") or details.get("cached_tokens")
                or usage.get("prompt_cache_hit_tokens") or 0)
    writes = int(usage.get("cache_creation_input_tokens") or details.get("cache_write_tokens") or 0)
    writes_1h = min(writes, int(usage.get("cache_creation_1h_input_tokens") or 0))
    if usage.get("input_tokens") is not None:
        uncached = int(usage.get("input_tokens") or 0)
    else:
        uncached = max(0, prompt - reads - writes)
    return uncached, writes - writes_1h, writes_1h, reads, int(usage.get("completion_tokens") or 0)


def input_token_usage(usage: dict | None) -> dict[str, int]:
    """The same input fields written to each model call's usage record."""
    usage = usage or {}
    uncached, writes_5m, writes_1h, reads, _ = split_usage(usage)
    return {"input_tokens": uncached, "cache_read_tokens": reads,
            "cache_write_tokens": writes_5m + writes_1h}
