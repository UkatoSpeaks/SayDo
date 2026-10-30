"""Filesystem storage for uploaded PDFs.

Uploads are streamed to a staging file while the SHA-256 is computed and the
size limit enforced. Once the caller has checked for duplicates, the staged file
is moved to ``{upload_dir}/{company_id}/{sha256}.pdf``.
"""

import contextlib
import hashlib
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

import anyio
from fastapi import UploadFile

from app.core.errors import InvalidInputError, PayloadTooLargeError, UnsupportedMediaTypeError

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES = 1024 * 1024
PDF_MAGIC = b"%PDF-"
# Per the PDF spec, readers must accept the header anywhere in the first 1024 bytes.
PDF_HEADER_SEARCH_BYTES = 1024
_STAGING_DIRNAME = ".staging"


@dataclass(frozen=True)
class StagedFile:
    path: Path
    sha256: str
    size_bytes: int


class PdfStorage:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = root
        self.max_bytes = max_bytes

    @property
    def staging_dir(self) -> Path:
        return self.root / _STAGING_DIRNAME

    def relative_path(self, company_id: uuid.UUID, sha256: str) -> str:
        return f"{company_id}/{sha256}.pdf"

    def resolve(self, relative_path: str) -> Path:
        path = (self.root / relative_path).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"Path escapes upload directory: {relative_path!r}")
        return path

    async def stage(self, upload: UploadFile) -> StagedFile:
        """Stream ``upload`` to a staging file, validating size and PDF magic bytes."""
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        staged_path = self.staging_dir / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        size = 0
        head = b""
        header_checked = False

        try:
            async with await anyio.open_file(staged_path, "wb") as out:
                while chunk := await upload.read(READ_CHUNK_BYTES):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise PayloadTooLargeError(
                            f"File exceeds the maximum upload size of "
                            f"{self.max_bytes // (1024 * 1024)} MB."
                        )
                    if not header_checked:
                        head += chunk[: PDF_HEADER_SEARCH_BYTES - len(head)]
                        if len(head) >= PDF_HEADER_SEARCH_BYTES:
                            _check_pdf_header(head)
                            header_checked = True
                    digest.update(chunk)
                    await out.write(chunk)

            if size == 0:
                raise InvalidInputError("Uploaded file is empty.", code="empty_file")
            if not header_checked:
                _check_pdf_header(head)
        except BaseException:
            staged_path.unlink(missing_ok=True)
            raise

        return StagedFile(path=staged_path, sha256=digest.hexdigest(), size_bytes=size)

    def commit(self, staged: StagedFile, company_id: uuid.UUID) -> str:
        """Move a staged file into its final location; returns the stored relative path."""
        relative = self.relative_path(company_id, staged.sha256)
        final_path = self.resolve(relative)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged.path, final_path)
        return relative

    def discard(self, staged: StagedFile) -> None:
        staged.path.unlink(missing_ok=True)

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to delete stored file %s", path)
            return
        # Tidy up the company directory once it is empty.
        with contextlib.suppress(OSError):
            path.parent.rmdir()


def _check_pdf_header(head: bytes) -> None:
    if PDF_MAGIC not in head:
        raise UnsupportedMediaTypeError("Uploaded file is not a PDF.", code="invalid_file_type")
