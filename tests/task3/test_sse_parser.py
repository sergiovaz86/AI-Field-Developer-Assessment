"""SSE parser boundaries."""

from __future__ import annotations

import json

from gateway_service.task3_stream_guardrail.sse_parser import (
    SseByteParser,
    SseData,
    SseDone,
    SseProtocolError,
)


def _feed(parser: SseByteParser, pieces: list[bytes]) -> list[object]:
    events: list[object] = []
    for piece in pieces:
        events.extend(parser.feed(piece))
    events.extend(parser.finish())
    return events


def test_lf_and_crlf_frames_parse_to_the_same_payload() -> None:
    payload = '{"content":"café"}'
    lf = _feed(SseByteParser(), [f"data: {payload}\n\n".encode()])
    crlf = _feed(SseByteParser(), [f"data: {payload}\r\n\r\n".encode()])
    assert lf == crlf == [SseData(payload)]


def test_split_crlf_and_split_utf8_reassemble() -> None:
    raw = "data: café\r\n\r\n".encode()
    events = _feed(SseByteParser(), [raw[:6], raw[6:7], raw[7:]])
    assert events == [SseData("café")]


def test_one_event_across_bytes_and_many_events_in_one_chunk() -> None:
    frame = 'data: {"n":1}\n\n'
    pieces = [frame.encode()[index : index + 1] for index in range(len(frame))]
    assert _feed(SseByteParser(), pieces) == [SseData('{"n":1}')]
    combined = 'data: {"n":1}\n\ndata: {"n":2}\n\ndata: [DONE]\n\n'
    events = _feed(SseByteParser(), [combined.encode()])
    assert events == [SseData('{"n":1}'), SseData('{"n":2}'), SseDone()]


def test_done_is_not_parsed_as_json() -> None:
    events = _feed(SseByteParser(), [b"data: [DONE]\n\n"])
    assert events == [SseDone()]
    assert json.dumps({"ok": True})


def test_oversized_and_truncated_frames_fail_closed() -> None:
    oversized = _feed(SseByteParser(max_event_bytes=8), [b"data: abcdefghij"])
    assert oversized == [SseProtocolError("oversized")]
    truncated = _feed(SseByteParser(), [b'data: {"n":1}'])
    assert truncated == [SseProtocolError("truncated")]


def test_invalid_utf8_is_rejected() -> None:
    events = _feed(SseByteParser(), [b"data: \xff\n\n"])
    assert events == [SseProtocolError("utf8")]


def test_many_small_events_in_one_read_stay_bounded() -> None:
    frame = b'data: {"n":1}\n\n' * 8
    parser = SseByteParser(max_event_bytes=40)
    assert len(frame) > 40
    assert _feed(parser, [frame]) == [SseData('{"n":1}')] * 8
    assert parser.buffered_bytes == 0
    pieces = [frame[index : index + 1] for index in range(len(frame))]
    split = _feed(SseByteParser(max_event_bytes=40), pieces)
    assert split == [SseData('{"n":1}')] * 8


def test_unterminated_frame_is_discarded_at_the_cap() -> None:
    parser = SseByteParser(max_event_bytes=24)
    assert parser.feed(b"data: ") == []
    assert 0 < parser.buffered_bytes <= 24
    assert parser.feed(b"abcdefghijklmnopqrstuvwxyz") == [SseProtocolError("oversized")]
    assert parser.buffered_bytes == 0
    assert parser.feed(b"data: ok\n\n") == []
