"""Streaming redaction of generated chat text.

``role``, tool-call ``type``, and tool-call ``id`` are copied unchanged.
``content``, ``refusal``, legacy ``function_call`` name and arguments, and
tool-call ``function.name`` / ``function.arguments`` each have a buffer.
Any other delta field is removed and is not forwarded.
"""

from __future__ import annotations

from typing import Any

from gateway_service.task3_stream_guardrail.pii_patterns import REDACTION_TOKEN as REDACTION_TOKEN
from gateway_service.task3_stream_guardrail.stream_buffer import StreamBuffer

_METADATA_FIELDS = frozenset({"role", "type", "id"})
_SPANNING_FIELDS = frozenset({"content", "refusal", "arguments"})
_DELTA_FIELDS = frozenset({"role", "content", "refusal", "tool_calls", "function_call"})
_TOOL_FIELDS = frozenset({"index", "id", "type", "function"})
_FUNCTION_FIELDS = frozenset({"name", "arguments"})
_TOP_LEVEL_FIELDS = frozenset(
    {
        "id",
        "object",
        "created",
        "model",
        "choices",
        "usage",
        "system_fingerprint",
        "service_tier",
    }
)
MAX_ACTIVE_FIELDS = 32


class ReconstructionError(Exception):
    """A generated field cannot be emitted without changing its destination.

    ``reason`` is a stable category and does not include the generated text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PiiRedactor:
    """One field's cross-delta redaction state."""

    def __init__(self) -> None:
        self._buffer = StreamBuffer()

    @property
    def buffered_length(self) -> int:
        return self._buffer.buffered_length

    def feed(self, text: str) -> str:
        return self._buffer.push(text)

    def finish(self) -> str:
        return self._buffer.flush()


class FieldRedactors:
    """Isolated redactors keyed by generated-text path."""

    def __init__(self) -> None:
        self._fields: dict[str, PiiRedactor] = {}

    @property
    def buffered_length(self) -> int:
        return sum(field.buffered_length for field in self._fields.values())

    def feed(self, key: str, text: str) -> str:
        redactor = self._fields.get(key)
        if redactor is None:
            if len(self._fields) >= MAX_ACTIVE_FIELDS:
                raise ReconstructionError("field_limit")
            redactor = PiiRedactor()
            self._fields[key] = redactor
        return redactor.feed(text)

    def finish(self, key: str) -> str:
        redactor = self._fields.pop(key, None)
        if redactor is None:
            return ""
        return redactor.finish()

    def finish_all(self) -> list[tuple[str, str]]:
        released: list[tuple[str, str]] = []
        for key in list(self._fields):
            text = self.finish(key)
            if text:
                released.append((key, text))
        return released

    def pending_keys(self) -> set[str]:
        return {key for key, field in self._fields.items() if field.buffered_length}


def prepare_chunk(chunk: dict[str, Any], redactors: FieldRedactors) -> list[dict[str, Any]]:
    """Release closed fields, then redact strings in this chunk.

    Content, refusal, tool names, and tool arguments can span deltas. A
    finish or usage chunk flushes every buffer into that chunk so withheld
    text is emitted before the stream ends. A chunk that only carried text
    still being withheld is omitted.
    """
    _validate_chunk(chunk)
    present = _string_paths(chunk)
    leading: list[dict[str, Any]] = []
    for key in list(redactors.pending_keys()):
        if key in present or _field_name(key) in _SPANNING_FIELDS:
            continue
        text = redactors.finish(key)
        if text:
            leading.append(_chunk_for_key(chunk, key, text))
    _rewrite_strings(chunk, redactors)
    trailing: list[dict[str, Any]] = []
    if _should_flush(chunk):
        for key, text in redactors.finish_all():
            if text and not _append_field(chunk, key, text):
                trailing.append(_chunk_for_key(chunk, key, text))
    if not _is_meaningful(chunk):
        return [item for item in (*leading, *trailing) if _is_meaningful(item)]
    return [*leading, *trailing, chunk]


def flush_tails(template: dict[str, Any], redactors: FieldRedactors) -> list[dict[str, Any]]:
    """Emit withheld text before the stream ends."""
    return _flush_tails(template, redactors)


def _flush_tails(template: dict[str, Any], redactors: FieldRedactors) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for key, text in redactors.finish_all():
        if text:
            chunks.append(_chunk_for_key(template, key, text))
    return chunks


def _validate_chunk(chunk: dict[str, Any]) -> None:
    for field in list(chunk):
        if field not in _TOP_LEVEL_FIELDS:
            chunk.pop(field, None)
    choices = chunk.get("choices")
    if choices is None:
        return
    if not isinstance(choices, list):
        raise ReconstructionError("choices")
    for position, choice in enumerate(choices):
        if not isinstance(choice, dict):
            raise ReconstructionError("choice")
        index = choice.get("index", position)
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ReconstructionError("choice_index")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise ReconstructionError("delta")
        calls = delta.get("tool_calls")
        if calls is None:
            continue
        if not isinstance(calls, list):
            raise ReconstructionError("tool_calls")
        for call_position, call in enumerate(calls):
            if not isinstance(call, dict):
                raise ReconstructionError("tool_call")
            _require_tool_index(call.get("index", call_position))


def _require_tool_index(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReconstructionError("tool_index")
    return value


def _rewrite_strings(chunk: dict[str, Any], redactors: FieldRedactors) -> None:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return
    for position, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        choice_index = _declared_index(choice, position)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        for field, value in list(delta.items()):
            if field not in _DELTA_FIELDS:
                delta.pop(field, None)
                continue
            path = f"{choice_index}:{field}"
            if field == "tool_calls":
                if isinstance(value, list):
                    _rewrite_tool_calls(choice_index, value, redactors)
                else:
                    delta.pop(field, None)
                continue
            if field == "function_call":
                if isinstance(value, dict):
                    _rewrite_function(f"{choice_index}:function_call", value, redactors)
                else:
                    delta.pop(field, None)
                continue
            if field in _METADATA_FIELDS:
                if not isinstance(value, str):
                    delta.pop(field, None)
                continue
            if not isinstance(value, str):
                delta.pop(field, None)
                continue
            released = redactors.feed(path, value)
            if released:
                delta[field] = released
            else:
                delta.pop(field, None)


def _rewrite_tool_calls(choice_index: int, calls: list[Any], redactors: FieldRedactors) -> None:
    for position, call in enumerate(calls):
        if not isinstance(call, dict):
            raise ReconstructionError("tool_call")
        for field in list(call):
            if field not in _TOOL_FIELDS:
                call.pop(field, None)
        tool_index = _require_tool_index(call.get("index", position))
        call["index"] = tool_index
        function = call.get("function")
        if function is None:
            continue
        if not isinstance(function, dict):
            raise ReconstructionError("function")
        _rewrite_function(f"{choice_index}:tool:{tool_index}", function, redactors)


def _rewrite_function(path: str, function: dict[str, Any], redactors: FieldRedactors) -> None:
    for field in list(function):
        if field not in _FUNCTION_FIELDS:
            function.pop(field, None)
            continue
        value = function[field]
        if not isinstance(value, str):
            function.pop(field, None)
            continue
        released = redactors.feed(f"{path}:{field}", value)
        if released:
            function[field] = released
        else:
            function.pop(field, None)


def _string_paths(chunk: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return found
    for position, choice in enumerate(choices):
        if not isinstance(choice, dict):
            continue
        choice_index = _declared_index(choice, position)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        for field, value in delta.items():
            if field == "tool_calls" and isinstance(value, list):
                for call_position, call in enumerate(value):
                    if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                        continue
                    tool_index = _require_tool_index(call.get("index", call_position))
                    function = call["function"]
                    for name in _FUNCTION_FIELDS:
                        if isinstance(function.get(name), str):
                            found.add(f"{choice_index}:tool:{tool_index}:{name}")
                continue
            if field == "function_call" and isinstance(value, dict):
                for name in _FUNCTION_FIELDS:
                    if isinstance(value.get(name), str):
                        found.add(f"{choice_index}:function_call:{name}")
                continue
            if isinstance(value, str) and field in {"content", "refusal"}:
                found.add(f"{choice_index}:{field}")
    return found


def _chunk_for_key(template: dict[str, Any], key: str, text: str) -> dict[str, Any]:
    parts = key.split(":")
    if not parts or not parts[0].isdigit():
        raise ReconstructionError("field_path")
    choice_index = int(parts[0])
    chunk = _shell(template, choice_index)
    delta = chunk["choices"][0]["delta"]
    if len(parts) == 4 and parts[1] == "tool" and parts[3] in _FUNCTION_FIELDS:
        delta["tool_calls"] = [
            {"index": int(parts[2]), "function": {parts[3]: text}},
        ]
        return chunk
    if len(parts) == 3 and parts[1] == "function_call" and parts[2] in _FUNCTION_FIELDS:
        delta["function_call"] = {parts[2]: text}
        return chunk
    if len(parts) == 2 and parts[1] in {"content", "refusal"}:
        delta[parts[1]] = text
        return chunk
    raise ReconstructionError("field_path")


def _shell(template: dict[str, Any], choice_index: int) -> dict[str, Any]:
    return {
        "id": template.get("id"),
        "object": template.get("object", "chat.completion.chunk"),
        "created": template.get("created"),
        "model": template.get("model"),
        "choices": [{"index": choice_index, "delta": {}, "finish_reason": None}],
    }


def _should_flush(chunk: dict[str, Any]) -> bool:
    if "usage" in chunk:
        return True
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    return any(isinstance(choice, dict) and choice.get("finish_reason") for choice in choices)


def _is_meaningful(chunk: dict[str, Any]) -> bool:
    if "usage" in chunk:
        return True
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        if choice.get("finish_reason"):
            return True
        delta = choice.get("delta")
        if isinstance(delta, dict) and delta:
            return True
    return False


def _append_field(chunk: dict[str, Any], key: str, text: str) -> bool:
    parts = key.split(":")
    if not parts or not parts[0].isdigit():
        return False
    choice = _choice(chunk, int(parts[0]))
    if choice is None:
        return False
    delta = choice.get("delta")
    if not isinstance(delta, dict):
        delta = {}
        choice["delta"] = delta
    if len(parts) >= 4 and parts[1] == "tool":
        calls = delta.get("tool_calls")
        if not isinstance(calls, list):
            calls = []
            delta["tool_calls"] = calls
        tool_index = int(parts[2])
        call = next(
            (
                item
                for item in calls
                if isinstance(item, dict)
                and not isinstance(item.get("index"), bool)
                and item.get("index") == tool_index
            ),
            None,
        )
        if call is None:
            call = {"index": tool_index, "function": {}}
            calls.append(call)
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
            call["function"] = function
        if parts[3] not in _FUNCTION_FIELDS:
            raise ReconstructionError("field_path")
        current = function.get(parts[3], "")
        function[parts[3]] = f"{current}{text}" if isinstance(current, str) else text
        return True
    if len(parts) == 3 and parts[1] == "function_call" and parts[2] in _FUNCTION_FIELDS:
        function = delta.get("function_call")
        if not isinstance(function, dict):
            function = {}
            delta["function_call"] = function
        current = function.get(parts[2], "")
        function[parts[2]] = f"{current}{text}" if isinstance(current, str) else text
        return True
    if len(parts) == 2 and parts[1] in {"content", "refusal"}:
        current = delta.get(parts[1], "")
        delta[parts[1]] = f"{current}{text}" if isinstance(current, str) else text
        return True
    raise ReconstructionError("field_path")


def _declared_index(choice: dict[str, Any], position: int) -> int:
    if "index" not in choice or choice.get("index") is None:
        return position
    return _require_tool_index(choice["index"])


def _choice(chunk: dict[str, Any], choice_index: int) -> dict[str, Any] | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if isinstance(choice, dict) and choice.get("index") == choice_index:
            return choice
    if choice_index < len(choices) and isinstance(choices[choice_index], dict):
        return choices[choice_index]
    return None


def _field_name(key: str) -> str:
    return key.rsplit(":", 1)[-1]
