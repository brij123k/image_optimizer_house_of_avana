"""Image compression logic — shrink file size, optionally converting to WebP."""
import base64
import io

from PIL import Image


def compress_image(raw_bytes, quality=75, max_width=None, force_webp=False):
    """Returns (compressed_bytes, base64_str, out_format).

    - force_webp: everything becomes WebP (transparency preserved).
    - JPEG: re-encoded with the given quality + optimize flag.
    - PNG without transparency: converted to JPEG for a much bigger win.
    - PNG with transparency: re-saved with optimize=True (lossless).
    - Other formats: best-effort re-save.
    """
    img = Image.open(io.BytesIO(raw_bytes))
    original_format = (img.format or "JPEG").upper()

    if max_width and img.width > max_width:
        ratio = max_width / float(img.width)
        img = img.resize((max_width, int(img.height * ratio)), Image.LANCZOS)

    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )

    out = io.BytesIO()

    if force_webp:
        # method=6 is the slowest/smallest encoder setting.
        img.convert("RGBA" if has_alpha else "RGB").save(
            out, format="WEBP", quality=quality, method=6
        )
        out_format = "WEBP"
    elif original_format == "PNG" and not has_alpha:
        img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
        out_format = "JPEG"
    elif original_format in ("JPEG", "JPG"):
        img.convert("RGB").save(out, format="JPEG", quality=quality, optimize=True)
        out_format = "JPEG"
    elif original_format == "PNG":
        img.save(out, format="PNG", optimize=True)
        out_format = "PNG"
    else:
        try:
            img.save(out, format=original_format, quality=quality, optimize=True)
        except (TypeError, ValueError):
            img.save(out, format=original_format)
        out_format = original_format

    compressed_bytes = out.getvalue()
    b64 = base64.b64encode(compressed_bytes).decode("utf-8")
    return compressed_bytes, b64, out_format