"""Prepare Telegram uploads for the harness's document and image inputs."""

from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile

from pypdf import PdfReader

MAX_BYTES = 8 * 1024 * 1024
IMAGE_TYPES = {".png": "image/png", ".jpeg": "image/jpeg", ".jpg": "image/jpeg", ".webp": "image/webp"}


class AttachmentError(ValueError):
    pass


def prepare(path):
    suffix = path.suffix.lower()
    if suffix in IMAGE_TYPES:
        return path
    try:
        if suffix == ".pdf":
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        elif suffix == ".docx":
            with ZipFile(path) as archive:
                if archive.getinfo("word/document.xml").file_size > MAX_BYTES:
                    raise AttachmentError("The expanded Word document exceeds 8 MiB.")
                root = ElementTree.fromstring(archive.read("word/document.xml"))
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            text = "\n".join("".join(node.text or "" for node in para.findall(".//w:t", ns))
                             for para in root.findall(".//w:p", ns))
        else:
            text = path.read_text(encoding="utf-8-sig")
        if not text.strip() or "\x00" in text:
            raise AttachmentError("No readable text found. For scanned pages, send an image.")
        if len(text.encode()) > 65536:
            raise AttachmentError("The extracted text exceeds 64 KiB. Send a shorter excerpt.")
    except AttachmentError:
        raise
    except Exception as exc:
        raise AttachmentError("Could not read this file. Send UTF-8 text, PDF, DOCX, JPEG, PNG or WebP.") from exc
    output = path.with_name(path.name + ".txt")
    output.write_text(text, encoding="utf-8")
    return output


def original_upload(prepared: Path) -> Path:
    """Return the original Telegram file when `prepare` produced extracted text."""
    if prepared.suffix == ".txt":
        original = prepared.with_name(prepared.name.removesuffix(".txt"))
        if original.is_file():
            return original
    return prepared


async def download(client, api, token, item, directory):
    if item.get("file_size", 0) > MAX_BYTES:
        raise AttachmentError("Each attachment must be 8 MiB or smaller.")
    info = await api("getFile", file_id=item["file_id"])
    remote = info["file_path"]
    name = Path(item.get("file_name") or remote).name
    path = directory / (name if name not in {"", ".", ".."} else "attachment")
    directory.mkdir()
    async with client.stream("GET", f"https://api.telegram.org/file/bot{token}/{remote}") as response:
        if not response.is_success:
            raise AttachmentError("Telegram could not download this attachment. Please resend it.")
        with path.open("wb") as stream:
            async for chunk in response.aiter_bytes():
                if stream.tell() + len(chunk) > MAX_BYTES:
                    raise AttachmentError("Each attachment must be 8 MiB or smaller.")
                stream.write(chunk)
    return prepare(path)
