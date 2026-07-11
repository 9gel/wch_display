"""Build theme packages (the panel's upload format) from host images.

The panel is a JPEG framebuffer. A theme package is:
    [u32 LE magic=918]
    [descriptor @0x40: resolution, frame count, data offset, theme id]
    [optional widget table @0x80 ...]   (we emit none)
    [records @0x1000: repeated (u32 BE jpeg_size)(jpeg bytes)]
For a static image we emit a single record with frame_count=1; the panel plays
records 0..N-1 as a background animation, so N identical/looping frames animate.
"""
import struct

from PIL import Image

MAGIC = 918
DATA_OFFSET = 0x1000


def encode_jpeg(img: Image.Image, quality: int = 80) -> bytes:
    from io import BytesIO
    if img.size != (800, 480):
        img = img.resize((800, 480))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def build_theme(jpegs, width=800, height=480) -> bytes:
    """Assemble a theme blob from a list of JPEG byte strings (each 800x480)."""
    records = bytearray()
    for j in jpegs:
        records += struct.pack(">I", len(j)) + j
    content_len = DATA_OFFSET + len(records)

    hdr = bytearray(DATA_OFFSET)
    struct.pack_into("<I", hdr, 0, MAGIC)
    hdr[0x40] = 0x81
    struct.pack_into(">H", hdr, 0x47, width)         # width  (BE16)
    struct.pack_into(">H", hdr, 0x49, height)        # height (BE16)
    struct.pack_into(">H", hdr, 0x4c, 0xf79e)        # constant seen in all themes
    struct.pack_into(">H", hdr, 0x50, DATA_OFFSET)   # data offset
    struct.pack_into(">H", hdr, 0x52, len(jpegs))    # frame count
    hdr[0x57] = 0x01                                 # has image frames
    hdr[0x59:0x5c] = content_len.to_bytes(3, "big")  # content length (BE24)

    return bytes(hdr) + bytes(records)


def theme_from_image(img: Image.Image, quality: int = 80) -> bytes:
    """Single static full-screen image -> theme blob."""
    return build_theme([encode_jpeg(img, quality)])


def theme_from_images(imgs, quality: int = 80) -> bytes:
    """Animation from a list of PIL images -> theme blob."""
    return build_theme([encode_jpeg(i, quality) for i in imgs])
