"""Minimal protobuf wire-format reader — no ``.proto`` schema required.

protobuf's binary wire format is self-describing: each field is a
``(field_number, wire_type)`` key followed by a value whose shape follows from
the wire type. Without a ``.proto`` schema the field *names* are unknowable, but
the complete structure — and every length-delimited payload, including all text —
can be recovered exactly. That is exactly what this module does.

Motivation: Antigravity (Google's agentic IDE) stores each conversation as a
SQLite database whose ``steps.step_payload`` column holds a binary protobuf
``Step`` message. No ``.proto`` ships with the store, which previously forced
adapters to keep the column *preserved by reference* with
``content_availability = unavailable``. The wire format is in fact fully
decodable, so the transcript is recoverable rather than lost.

This module is deliberately dependency-free (stdlib only) and tolerant: a
truncated or non-protobuf buffer raises :class:`WireFormatError` in strict mode
and can be read leniently to recover the leading fields of a buffer that is only
protobuf for part of its length.
"""

from __future__ import annotations

from typing import Iterator

__all__ = [
    "WireFormatError",
    "PbMessage",
    "read_varint",
    "encode_varint",
    "encode_field",
    "as_text",
]


class WireFormatError(ValueError):
    """Raised when a buffer cannot be read as protobuf wire format."""


def read_varint(buf: bytes, index: int) -> tuple[int, int]:
    """Decode one base-128 varint. Returns ``(value, next_index)``."""
    shift = 0
    value = 0
    i = index
    n = len(buf)
    while i < n:
        byte = buf[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, i
        shift += 7
        if shift >= 64:
            raise WireFormatError("varint longer than 64 bits")
    raise WireFormatError("truncated varint")


def encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a base-128 varint (test/builder helper)."""
    if value < 0:
        raise ValueError("negative varints are not supported")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def encode_field(number: int, wire_type: int, payload: bytes | int) -> bytes:
    """Encode one field (builder helper used by tests and fixtures)."""
    if number <= 0:
        raise ValueError("field numbers start at 1")
    key = encode_varint((number << 3) | wire_type)
    if wire_type == 0:
        return key + encode_varint(int(payload))
    if wire_type == 2:
        body = bytes(payload)
        return key + encode_varint(len(body)) + body
    raise ValueError(f"unsupported wire type {wire_type}")


def _printable_ratio(text: str) -> float:
    """Share of characters that are printable (newlines/tabs allowed)."""
    ok = sum(1 for ch in text if ch.isprintable() or ch in "\n\r\t")
    return ok / len(text)


def _decode_utf8(chunk: bytes) -> str | None:
    try:
        return chunk.decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def _text_candidates(chunk: bytes) -> Iterator[str]:
    """Yield progressively more aggressive decodings of ``chunk``, best first.

    The second candidate exists because Windows tooling (PowerShell, ``wsl.exe``)
    emits UTF-16LE text, which arrives here as ASCII/Latin bytes padded with NUL
    at every other offset. A protobuf ``string`` field never legitimately
    contains a NUL, so dropping the padding bytes recovers the payload — and
    also rescues the mixed case where a UTF-8 prefix was concatenated in front
    of a UTF-16 tail. This runs only as a fallback, after plain UTF-8 has failed
    to be fully printable, so ordinary text is never rewritten.
    """
    text = _decode_utf8(chunk)
    if text is not None:
        yield text
    if b"\x00" in chunk:
        stripped = chunk.replace(b"\x00", b"")
        if stripped and stripped != chunk:
            recovered = _decode_utf8(stripped)
            if recovered is not None:
                yield recovered


def as_text(chunk: bytes, *, lenient: bool = False) -> str | None:
    """Return ``chunk`` decoded as text, or ``None`` if it is binary.

    A chunk counts as text when it is valid UTF-8 and every character is
    printable (newlines/tabs allowed). ``lenient`` additionally accepts mostly
    printable buffers, which is useful for recovering a text field that was
    concatenated with a small binary tail. When plain UTF-8 is not fully
    printable the NUL-padded UTF-16 form is tried as a fallback.
    """
    if not chunk:
        return None
    best: str | None = None
    best_ratio = -1.0
    for text in _text_candidates(chunk):
        ratio = _printable_ratio(text)
        if ratio == 1.0:
            return text
        if ratio > best_ratio:
            best, best_ratio = text, ratio
    if best is None:
        return None
    if lenient and best_ratio > 0.9:
        return best
    return None


class PbMessage:
    """A decoded protobuf message: a flat list of ``(field, wire_type, value)``.

    ``value`` is ``int`` for varint fields, ``bytes`` for fixed-width and
    length-delimited fields. ``strict=False`` reads as far as the buffer is
    well-formed instead of raising, which is how a payload that starts as
    protobuf and ends in an opaque blob is partially recovered.
    """

    __slots__ = ("raw", "fields", "complete")

    def __init__(self, buf: bytes, *, strict: bool = True) -> None:
        self.raw = buf
        self.fields: list[tuple[int, int, object]] = []
        self.complete = True

        i = 0
        n = len(buf)
        while i < n:
            try:
                key, i = read_varint(buf, i)
            except WireFormatError:
                self._incomplete(strict)
                break
            number = key >> 3
            wire_type = key & 7
            if number == 0:
                self._incomplete(strict)
                break
            if wire_type == 0:
                try:
                    value, i = read_varint(buf, i)
                except WireFormatError:
                    self._incomplete(strict)
                    break
                self.fields.append((number, 0, value))
            elif wire_type == 1:
                if i + 8 > n:
                    self._incomplete(strict)
                    break
                self.fields.append((number, 1, buf[i : i + 8]))
                i += 8
            elif wire_type == 2:
                try:
                    length, i = read_varint(buf, i)
                except WireFormatError:
                    self._incomplete(strict)
                    break
                if i + length > n:
                    self._incomplete(strict)
                    break
                self.fields.append((number, 2, buf[i : i + length]))
                i += length
            elif wire_type == 5:
                if i + 4 > n:
                    self._incomplete(strict)
                    break
                self.fields.append((number, 5, buf[i : i + 4]))
                i += 4
            else:  # 3/4 are deprecated group markers
                self._incomplete(strict)
                break

    def _incomplete(self, strict: bool) -> None:
        self.complete = False
        if strict:
            raise WireFormatError("buffer is not well-formed protobuf")

    # ---------------------------------------------------------------- queries

    def raw_fields(self, number: int) -> list[object]:
        """All values for ``number`` regardless of wire type."""
        return [v for f, _wt, v in self.fields if f == number]

    def blobs(self, number: int) -> list[bytes]:
        """All length-delimited values for ``number``."""
        return [v for f, wt, v in self.fields if f == number and wt == 2]

    def blob(self, number: int) -> bytes | None:
        """First length-delimited value for ``number``."""
        for f, wt, v in self.fields:
            if f == number and wt == 2:
                return v
        return None

    def integer(self, number: int) -> int | None:
        """First varint value for ``number``."""
        for f, wt, v in self.fields:
            if f == number and wt == 0:
                return v
        return None

    def text(self, number: int, *, lenient: bool = False) -> str | None:
        """First length-delimited value for ``number`` that decodes as text."""
        for f, wt, v in self.fields:
            if f == number and wt == 2:
                decoded = as_text(v, lenient=lenient)
                if decoded is not None:
                    return decoded
        return None

    def sub(self, number: int, *, strict: bool = True) -> list["PbMessage"]:
        """All length-delimited values for ``number`` parsed as sub-messages."""
        out: list[PbMessage] = []
        for f, wt, v in self.fields:
            if f == number and wt == 2:
                out.append(PbMessage(v, strict=strict))
        return out

    def sub1(self, number: int, *, strict: bool = True) -> "PbMessage | None":
        """First length-delimited value for ``number`` parsed as a sub-message."""
        blob = self.blob(number)
        if blob is None:
            return None
        return PbMessage(blob, strict=strict)

    def iter_text(self, *, min_length: int = 1, _prefix: str = "") -> Iterator[tuple[str, str]]:
        """Yield ``(field_path, text)`` for every recoverable text leaf."""
        for f, wt, v in self.fields:
            if wt != 2:
                continue
            path = f"{_prefix}/f{f}" if _prefix else f"f{f}"
            decoded = as_text(v)
            if decoded is not None:
                if len(decoded) >= min_length:
                    yield path, decoded
                continue
            try:
                child = PbMessage(v, strict=False)
            except WireFormatError:
                continue
            yield from child.iter_text(min_length=min_length, _prefix=path)
