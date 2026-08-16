"""Minimal, dependency-free Ogg/Opus (and Ogg/Vorbis) metadata reader.

Only what an audiobook scanner needs: the Vorbis comment tags, the stream
duration, and any embedded cover picture. Everything is parsed from the raw
container, so there is nothing to pip install and no ffprobe dependency.

Container reference: an Ogg file is a sequence of pages. Each page carries a
27-byte header (capture pattern "OggS", flags, granule position, stream serial,
sequence, CRC, segment count) followed by a segment table and the segment
bodies. Logical packets are the concatenation of segments; a segment of exactly
255 bytes means "the packet continues", so a packet can span pages.

For Opus the first packet is OpusHead and the second is OpusTags. The granule
position counts 48 kHz samples, so the duration is the final page's granule
minus the encoder pre-skip, divided by 48000.
"""

from __future__ import annotations

import base64
import struct

_OGG_MAGIC = b"OggS"
_HEADER_LEN = 27
_TAIL_WINDOW = 1 << 16  # how far back to look for the final page

# Vorbis comment keys we care about, lowercased.
_WANTED_TAGS = {
    "title",
    "album",
    "artist",
    "albumartist",
    "album artist",
    "composer",
    "tracknumber",
    "track",
    "discnumber",
    "date",
    "year",
    "genre",
    "description",
    "comment",
    "narrator",
    "performer",
    "series",
    "part",
}


class OggFormatError(Exception):
    """Raised when a file is not readable as an Ogg stream."""


def _read_page(fh):
    """Read one Ogg page from the current position, or None at EOF."""
    header = fh.read(_HEADER_LEN)
    if len(header) < _HEADER_LEN or not header.startswith(_OGG_MAGIC):
        return None
    granule = struct.unpack("<q", header[6:14])[0]
    serial = struct.unpack("<I", header[14:18])[0]
    seg_count = header[26]
    seg_table = fh.read(seg_count)
    if len(seg_table) < seg_count:
        return None
    body = fh.read(sum(seg_table))
    return {
        "flags": header[5],
        "granule": granule,
        "serial": serial,
        "segments": seg_table,
        "body": body,
    }


def _leading_packets(fh, max_packets=2, max_pages=64):
    """Assemble the first few logical packets of the first logical stream."""
    packets = []
    pending = bytearray()
    serial = None
    for _ in range(max_pages):
        page = _read_page(fh)
        if page is None:
            break
        if serial is None:
            serial = page["serial"]
        elif page["serial"] != serial:
            continue  # a second multiplexed stream; audiobooks never have one
        offset = 0
        for seg_len in page["segments"]:
            pending += page["body"][offset : offset + seg_len]
            offset += seg_len
            if seg_len < 255:  # segment < 255 terminates the packet
                packets.append(bytes(pending))
                pending.clear()
                if len(packets) >= max_packets:
                    return packets
    return packets


def _parse_vorbis_comments(blob, start):
    """Parse a Vorbis comment block, returning (tags, raw_picture_values)."""
    tags = {}
    pictures = []
    pos = start
    if pos + 4 > len(blob):
        return tags, pictures
    vendor_len = struct.unpack_from("<I", blob, pos)[0]
    pos += 4 + vendor_len
    if pos + 4 > len(blob):
        return tags, pictures
    count = struct.unpack_from("<I", blob, pos)[0]
    pos += 4
    for _ in range(min(count, 512)):  # guard against a corrupt length field
        if pos + 4 > len(blob):
            break
        length = struct.unpack_from("<I", blob, pos)[0]
        pos += 4
        entry = blob[pos : pos + length]
        pos += length
        sep = entry.find(b"=")
        if sep <= 0:
            continue
        key = entry[:sep].decode("utf-8", "replace").strip().lower()
        value = entry[sep + 1 :].decode("utf-8", "replace").strip()
        if key == "metadata_block_picture":
            pictures.append(value)
        elif key in _WANTED_TAGS and value and key not in tags:
            tags[key] = value
    return tags, pictures


def _stream_duration(fh, file_size, pre_skip, sample_rate):
    """Duration in seconds, from the granule position of the last page."""
    window = min(_TAIL_WINDOW, file_size)
    fh.seek(file_size - window)
    tail = fh.read(window)
    idx = tail.rfind(_OGG_MAGIC)
    while idx != -1:
        if idx + _HEADER_LEN <= len(tail):
            granule = struct.unpack_from("<q", tail, idx + 6)[0]
            if granule > 0:
                return max(0.0, (granule - pre_skip) / sample_rate)
        idx = tail.rfind(_OGG_MAGIC, 0, idx)
    return 0.0


def read(path):
    """Read one Ogg/Opus or Ogg/Vorbis file.

    Returns a dict with ``tags``, ``duration`` (seconds), ``channels`` and
    ``has_picture``. Raises OggFormatError if the file is not Ogg at all.
    """
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        file_size = fh.tell()
        if file_size < _HEADER_LEN:
            raise OggFormatError("file too small to be Ogg")
        fh.seek(0)
        packets = _leading_packets(fh)
        if not packets:
            raise OggFormatError("no Ogg pages found")

        head, comment_packet = packets[0], (packets[1] if len(packets) > 1 else b"")
        pre_skip, channels, sample_rate = 0, 2, 48000.0
        tags, pictures = {}, []

        if head.startswith(b"OpusHead") and len(head) >= 12:
            channels = head[9]
            pre_skip = struct.unpack_from("<H", head, 10)[0]
            if comment_packet.startswith(b"OpusTags"):
                tags, pictures = _parse_vorbis_comments(comment_packet, 8)
        elif head.startswith(b"\x01vorbis") and len(head) >= 16:
            channels = head[11]
            sample_rate = float(struct.unpack_from("<I", head, 12)[0]) or 48000.0
            if comment_packet.startswith(b"\x03vorbis"):
                tags, pictures = _parse_vorbis_comments(comment_packet, 7)
        else:
            raise OggFormatError("unrecognised Ogg codec")

        duration = _stream_duration(fh, file_size, pre_skip, sample_rate)

    return {
        "tags": tags,
        "duration": duration,
        "channels": channels,
        "has_picture": bool(pictures),
    }


def read_picture(path):
    """Return (mime_type, image_bytes) for an embedded cover, or None.

    METADATA_BLOCK_PICTURE holds a base64-encoded FLAC picture block:
    big-endian type, MIME length + MIME, description length + description,
    width, height, depth, colour count, then the image length and data.
    """
    try:
        with open(path, "rb") as fh:
            packets = _leading_packets(fh)
    except OSError:
        return None
    if len(packets) < 2:
        return None

    comment = packets[1]
    if comment.startswith(b"OpusTags"):
        _, pictures = _parse_vorbis_comments(comment, 8)
    elif comment.startswith(b"\x03vorbis"):
        _, pictures = _parse_vorbis_comments(comment, 7)
    else:
        return None

    for encoded in pictures:
        try:
            blob = base64.b64decode(encoded, validate=False)
            pos = 4  # skip picture type
            mime_len = struct.unpack_from(">I", blob, pos)[0]
            pos += 4
            mime = blob[pos : pos + mime_len].decode("ascii", "replace")
            pos += mime_len
            desc_len = struct.unpack_from(">I", blob, pos)[0]
            pos += 4 + desc_len
            pos += 16  # width, height, depth, colour count
            data_len = struct.unpack_from(">I", blob, pos)[0]
            pos += 4
            data = blob[pos : pos + data_len]
            if data:
                return mime or "image/jpeg", data
        except (struct.error, ValueError, IndexError):
            continue
    return None
