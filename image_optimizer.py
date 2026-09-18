"""Image compression logic — shrink file size, optionally converting to WebP."""
import base64
import io

from PIL import Image, ImageEnhance, ImageFilter


def center_crop_to_ratio(img, ratio):
    """Center-crops img to match the given (width, height) aspect ratio,
    trimming the minimum needed off whichever axis is relatively too long.
    A no-op if the image is already (near) that ratio. Never enlarges."""
    target_w, target_h = ratio
    if not target_w or not target_h:
        return img
    target = target_w / float(target_h)
    current = img.width / float(img.height)

    if abs(current - target) < 1e-6:
        return img

    if current > target:
        # relatively too wide — trim the sides
        new_width = max(1, round(img.height * target))
        left = (img.width - new_width) // 2
        box = (left, 0, left + new_width, img.height)
    else:
        # relatively too tall — trim top and bottom
        new_height = max(1, round(img.width / target))
        top = (img.height - new_height) // 2
        box = (0, top, img.width, top + new_height)
    return img.crop(box)


def compress_image(raw_bytes, quality=75, max_width=None, max_height=None, force_webp=False,
                    crop_ratio=None):
    """Returns (compressed_bytes, base64_str, out_format).

    - crop_ratio: optional (width, height) tuple, e.g. (16, 9). The image is
      center-cropped to that aspect ratio first, before any resizing below.
      None (or (0, 0)) leaves the image's original framing untouched.
    - force_webp: everything becomes WebP (transparency preserved).
    - JPEG: re-encoded with the given quality + optimize flag.
    - PNG without transparency: converted to JPEG for a much bigger win.
    - PNG with transparency: re-saved with optimize=True (lossless).
    - Other formats: best-effort re-save.
    - max_width / max_height: the image is shrunk (never enlarged) to fit
      within whichever bound(s) are given, preserving aspect ratio.
    """
    img = Image.open(io.BytesIO(raw_bytes))
    original_format = (img.format or "JPEG").upper()

    if crop_ratio:
        img = center_crop_to_ratio(img, crop_ratio)

    if max_width or max_height:
        ratio = min(
            [r for r in (
                max_width / float(img.width) if max_width else None,
                max_height / float(img.height) if max_height else None,
            ) if r is not None]
        )
        if ratio < 1:
            img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))), Image.LANCZOS)

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


def apply_enhancements(img, values):
    """Applies the Enhance tool's slider values (each roughly -50..50, noise
    0..100) to img and returns a new PIL Image. Alpha (if any) is preserved
    untouched — only the color channels are adjusted."""
    has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
    alpha = None
    if has_alpha:
        img = img.convert("RGBA")
        alpha = img.getchannel("A")
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")

    brightness = 1 + (values.get("brightness", 0) + values.get("exposure", 0)) / 100
    contrast = 1 + values.get("contrast", 0) / 100
    saturation = 1 + values.get("saturation", 0) / 100

    if abs(brightness - 1) > 1e-6:
        img = ImageEnhance.Brightness(img).enhance(max(0.1, brightness))
    if abs(contrast - 1) > 1e-6:
        img = ImageEnhance.Contrast(img).enhance(max(0.1, contrast))
    if abs(saturation - 1) > 1e-6:
        img = ImageEnhance.Color(img).enhance(max(0.0, saturation))

    # Sharpness and Clarity both read as "more/less local contrast" — one
    # unsharp-mask pass driven by their combined value covers both.
    sharpness_amt = values.get("sharpness", 0) + values.get("clarity", 0)
    if sharpness_amt > 0:
        img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(min(300, sharpness_amt * 3)), threshold=2))
    elif sharpness_amt < 0:
        img = img.filter(ImageFilter.GaussianBlur(radius=min(4, abs(sharpness_amt) / 15)))

    noise = values.get("noise", 0)
    if noise > 0:
        blurred = img.filter(ImageFilter.MedianFilter(size=3))
        img = Image.blend(img, blurred, min(1.0, noise / 100))

    if alpha is not None:
        img = img.convert("RGBA")
        img.putalpha(alpha)
    return img


def composite_over_color(cutout, color_hex, feather=0):
    """cutout: an RGBA image (subject with a transparent background, e.g. from
    background removal). Returns an RGB image with color_hex filled in behind
    it. feather (0-12px) softens the cutout edge before compositing."""
    cutout = cutout.convert("RGBA")
    if feather:
        alpha = cutout.getchannel("A").filter(ImageFilter.GaussianBlur(radius=feather / 3))
        cutout.putalpha(alpha)
    color_hex = color_hex.lstrip("#")
    rgb = tuple(int(color_hex[i:i + 2], 16) for i in (0, 2, 4))
    background = Image.new("RGB", cutout.size, rgb)
    background.paste(cutout, (0, 0), cutout)
    return background