"""Chromaprint fingerprint decoding and similarity, in pure Python (no libchromaprint needed).

Compressed format (as produced by fpcalc): urlsafe base64 of
  1 byte algorithm, 3 bytes item count (big-endian),
  "normal" bit-deltas packed as 3-bit values (LSB first), 0 ends an item, 7 means "exceptional",
  then the exceptional remainders packed as 5-bit values.
Each item is the XOR of consecutive 32-bit sub-fingerprints.
"""

import base64
from functools import lru_cache


def _unpack(data: bytes, bits: int, count: int | None = None):
    """Yield `bits`-wide values packed LSB-first."""
    acc = nacc = 0
    got = 0
    mask = (1 << bits) - 1
    for byte in data:
        acc |= byte << nacc
        nacc += 8
        while nacc >= bits:
            yield acc & mask
            acc >>= bits
            nacc -= bits
            got += 1
            if count is not None and got == count:
                return


@lru_cache(maxsize=4096)
def decode(fp: str) -> tuple[int, ...]:
    raw = base64.urlsafe_b64decode(fp + "=" * (-len(fp) % 4))
    n = int.from_bytes(raw[1:4], "big")
    body = raw[4:]
    # Pass 1: 3-bit normal values until n items are complete.
    normal, items_seen, used_bits = [], 0, 0
    for v in _unpack(body, 3):
        normal.append(v)
        used_bits += 3
        if v == 0:
            items_seen += 1
            if items_seen == n:
                break
    exceptional_start = (used_bits + 7) // 8
    need = sum(1 for v in normal if v == 7)
    exc = iter(list(_unpack(body[exceptional_start:], 5, need)))
    out, value, last_bit, prev = [], 0, 0, 0
    for v in normal:
        if v == 0:
            prev ^= value
            out.append(prev)
            value = last_bit = 0
            continue
        if v == 7:
            v += next(exc)
        last_bit += v
        value |= 1 << (last_bit - 1)
    return tuple(out)


def similarity(a: str, b: str, max_offset: int = 10) -> float:
    """1 - bit error rate over the best alignment (within +/- max_offset items)."""
    x, y = decode(a), decode(b)
    best = 0.0
    for off in range(-max_offset, max_offset + 1):
        pairs = [(x[i], y[i + off]) for i in range(len(x)) if 0 <= i + off < len(y)]
        if len(pairs) < 50:
            continue
        err = sum(bin((p ^ q) & 0xFFFFFFFF).count("1") for p, q in pairs) / (32 * len(pairs))
        best = max(best, 1 - err)
    return round(best, 3)
