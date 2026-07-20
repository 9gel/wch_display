"""Decode/encode the vendor editor's ``.ui`` theme files.

The editor stores themes as RC4-encrypted XML. RC4 is symmetric, so the same
operation decrypts and encrypts. Key recovered by static analysis of the editor
binary (see ``PROTOCOL_NOTES.md``).
"""

RC4_KEY = b"This product is designed by OuJianbo,zhe ge chan pin shi gzbkey she ji de"


def rc4(key: bytes, data: bytes) -> bytes:
    S = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + S[i] + key[i % klen]) & 0xFF
        S[i], S[j] = S[j], S[i]
    out = bytearray(len(data))
    i = j = 0
    for n in range(len(data)):
        i = (i + 1) & 0xFF
        j = (j + S[i]) & 0xFF
        S[i], S[j] = S[j], S[i]
        out[n] = data[n] ^ S[(S[i] + S[j]) & 0xFF]
    return bytes(out)


def decode(data: bytes) -> bytes:
    """Encrypted ``.ui`` bytes -> plaintext XML bytes."""
    return rc4(RC4_KEY, data)


def encode(xml: bytes) -> bytes:
    """Plaintext XML bytes -> encrypted ``.ui`` bytes (loadable by the editor)."""
    return rc4(RC4_KEY, xml)


def decode_file(path: str) -> bytes:
    with open(path, "rb") as f:
        return decode(f.read())
