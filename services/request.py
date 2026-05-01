from typing import Any


def extract_args(body: Any) -> dict[str, Any]:
    """Webhook tool calls arrive as either {"arguments": {...}} or {...}
    depending on how the platform's dispatcher framed them. Normalize to
    a plain dict so handlers don't have to guess."""
    args = body.get("arguments") if isinstance(body, dict) else None
    if not isinstance(args, dict):
        args = body if isinstance(body, dict) else {}
    return args
