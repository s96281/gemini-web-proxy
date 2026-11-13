import base64
import binascii
import hashlib
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import ProxyError

DATA_IMAGE = re.compile(r"data:(image/(?:jpeg|png|webp|gif));base64,", re.IGNORECASE)
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


@dataclass(frozen=True)
class ImageAttachment:
    name: str
    mime_type: str
    data: bytes = field(repr=False)


def image_name(url, message_index, part_index):
    match = DATA_IMAGE.match(url)
    if not match:
        raise ProxyError(
            "unsupported_image_url",
            "image_url requires a base64 data:image/jpeg, png, webp or gif URL; "
            "HTTP URLs and local file paths are unsupported.",
            400,
        )
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"image-m{message_index}-p{part_index}-{digest}.{EXTENSIONS[match[1].lower()]}"


def image_parts(messages, start=0):
    for message_index in range(start, len(messages)):
        content = messages[message_index].content
        if isinstance(content, list):
            for part_index, part in enumerate(content):
                if part["type"] == "image_url":
                    yield message_index, part_index, part["image_url"]["url"]


def prepare_images(request, start, settings):
    images = []
    total = 0
    for message_index, part_index, url in image_parts(request.messages, start):
        if len(images) >= settings.max_images:
            raise ProxyError("too_many_images", "Too many new images in this request.", 400)
        name = image_name(url, message_index, part_index)
        match = DATA_IMAGE.match(url)
        encoded = url[match.end() :]
        if len(encoded) > 4 * ((settings.max_image_bytes + 2) // 3):
            raise ProxyError("image_too_large", "Image exceeds the configured byte limit.", 413)
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ProxyError("invalid_image", "Image contains invalid base64 data.", 400) from None
        mime = match[1].lower()
        valid = {
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        }
        if not valid[mime]:
            raise ProxyError("invalid_image", "Image data does not match its MIME type.", 400)
        total += len(data)
        if len(data) > settings.max_image_bytes or total > settings.max_total_image_bytes:
            raise ProxyError("image_too_large", "Images exceed the configured byte limit.", 413)
        images.append(ImageAttachment(name, mime, data))
    return images


@contextmanager
def image_files(images):
    # The pinned SDK assigns random .txt names to bytes/BytesIO; paths preserve image MIME/name.
    if not images:
        yield None
        return
    with TemporaryDirectory(prefix="gemini-proxy-images-") as directory:
        paths = []
        for image in images:
            path = Path(directory) / image.name
            path.write_bytes(image.data)
            paths.append(path)
        yield paths
