"""protobuf wire-format reader contract.

The reader must recover structure and text without any ``.proto`` schema, and
must refuse to guess when a buffer is not well-formed protobuf.
"""

from __future__ import annotations

import pytest

from personal_knowledge.adapters.conversation_sources.protobuf_wire import (
    PbMessage,
    WireFormatError,
    as_text,
    encode_field,
    encode_varint,
    read_varint,
)


class TestVarint:
    def test_round_trip(self):
        for value in (0, 1, 127, 128, 300, 1788678250, 2**40):
            encoded = encode_varint(value)
            decoded, index = read_varint(encoded, 0)
            assert decoded == value
            assert index == len(encoded)

    def test_truncated_raises(self):
        with pytest.raises(WireFormatError):
            read_varint(b"\x80\x80", 0)

    def test_overlong_raises(self):
        with pytest.raises(WireFormatError):
            read_varint(b"\xff" * 12, 0)


class TestParse:
    def test_reads_mixed_wire_types(self):
        fixed32 = bytes([0x1D]) + b"\x01\x02\x03\x04"  # field 3, wire type 5
        buf = encode_field(1, 0, 14) + encode_field(2, 2, b"hi") + fixed32
        msg = PbMessage(buf)
        assert msg.complete is True
        assert msg.integer(1) == 14
        assert msg.text(2) == "hi"
        # raw_fields exposes a value whatever its wire type...
        assert msg.raw_fields(3) == [b"\x01\x02\x03\x04"]
        # ...while blob() is deliberately length-delimited (wire type 2) only.
        assert msg.blob(3) is None

    def test_truncated_length_raises_in_strict_mode(self):
        with pytest.raises(WireFormatError):
            PbMessage(encode_field(1, 2, b"abcdef")[:-2])

    def test_lenient_mode_keeps_leading_fields(self):
        good = encode_field(1, 0, 7)
        buf = good + encode_field(2, 2, b"abcdef")[:-2]
        msg = PbMessage(buf, strict=False)
        assert msg.complete is False
        assert msg.integer(1) == 7

    def test_repeated_fields_are_listed(self):
        buf = encode_field(1, 2, b"a") + encode_field(1, 2, b"b")
        assert PbMessage(buf).blobs(1) == [b"a", b"b"]

    def test_field_number_zero_is_rejected(self):
        with pytest.raises(WireFormatError):
            PbMessage(bytes([0x00, 0x01]))


class TestTextDetection:
    def test_printable_utf8_is_text(self):
        assert as_text("中文 ok".encode()) == "中文 ok"

    def test_newlines_and_tabs_are_allowed(self):
        assert as_text(b"a\nb\tc") == "a\nb\tc"

    def test_binary_is_not_text(self):
        assert as_text(bytes([0x00, 0x01, 0x02])) is None

    def test_strict_rejects_mostly_printable_with_binary_tail(self):
        assert as_text(b"abc\x01\x02") is None

    def test_lenient_accepts_mostly_printable(self):
        # one stray control byte in eleven: > 0.9 printable
        assert as_text(b"abcdefghij" + b"\x01", lenient=True) == "abcdefghij\x01"

    def test_lenient_still_rejects_heavily_binary_buffer(self):
        assert as_text(b"abc" + b"\x01" * 7, lenient=True) is None

    def test_empty_is_not_text(self):
        assert as_text(b"") is None


class TestWindowsUtf16Recovery:
    """Windows tooling (PowerShell, wsl.exe) emits UTF-16 text as NUL-padded
    ASCII. protobuf string fields never contain a NUL, so dropping the padding
    is a safe fallback once plain UTF-8 has failed to be printable."""

    def test_nul_padded_utf16le_ascii_is_recovered(self):
        text = "wsl: wsl2.sparseVhd:C:\\Users\\li\\.wslconfig"
        chunk = text.encode("utf-16-le")
        assert b"\x00" in chunk
        assert as_text(chunk) == text

    def test_utf8_prefix_followed_by_utf16_tail_is_recovered(self):
        # Real artifact: a plain-UTF-8 status header concatenated with UTF-16LE
        # command output. Decoding the whole buffer as UTF-16 would turn the
        # ASCII prefix into CJK gibberish; dropping the NULs is the correct fix.
        prefix = "Task: d84f1ba0/task-323\nStatus: RUNNING\n"
        tail = " NAME    STATE\n wsl      RUNNING"
        chunk = prefix.encode("utf-8") + tail.encode("utf-16-le")
        assert as_text(chunk) == prefix + tail

    def test_nul_padded_binary_is_still_not_text(self):
        assert as_text(bytes([0x00, 0xFF, 0x00, 0xFE])) is None

    def test_ordinary_text_is_never_rewritten(self):
        # no NUL present -> the fallback cannot fire
        assert as_text("中文 ok".encode()) == "中文 ok"


class TestNestedAccess:
    def test_sub_and_sub1(self):
        inner = encode_field(1, 2, b"deep")
        msg = PbMessage(encode_field(5, 2, inner))
        assert msg.sub1(5).text(1) == "deep"
        assert len(msg.sub(5)) == 1

    def test_missing_field_returns_none(self):
        msg = PbMessage(encode_field(1, 0, 1))
        assert msg.blob(9) is None
        assert msg.sub1(9) is None
        assert msg.integer(9) is None
        assert msg.text(9) is None

    def test_iter_text_walks_nested_messages(self):
        inner = encode_field(2, 2, b"leaf")
        paths = dict(PbMessage(encode_field(5, 2, inner)).iter_text())
        assert paths.get("f5/f2") == "leaf"

    def test_iter_text_respects_min_length(self):
        buf = encode_field(1, 2, b"ab") + encode_field(1, 2, b"a-longer-leaf")
        texts = [t for _p, t in PbMessage(buf).iter_text(min_length=5)]
        assert texts == ["a-longer-leaf"]
