import os
import re
from contextlib import suppress
from pathlib import Path
from shutil import copyfileobj
from typing import BinaryIO, Protocol


STAGED_KEY = re.compile(r"staging/prj_[0-9a-f]{32}/upl_[0-9a-f]{32}/[0-9a-f]{32}\.[a-z0-9]{2,8}")


class StorageError(Exception):
    pass


class UploadStorage(Protocol):
    def put(self, key: str, file: BinaryIO, *, size: int, content_type: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def path(self, key: str) -> Path: ...


class LocalUploadStorage:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, key: str) -> Path:
        if not STAGED_KEY.fullmatch(key):
            raise StorageError
        return self._plain_path(self.root.joinpath(*key.split("/")))

    @staticmethod
    def _plain_path(path: Path) -> Path:
        try:
            # The storage key addresses only files and directories created by this service.
            if path.resolve() != path:
                raise StorageError
        except (OSError, RuntimeError):
            raise StorageError from None
        return path

    def _partial_path(self, target: Path) -> Path:
        return self._plain_path(target.with_name(target.name + ".partial"))

    def put(self, key: str, file: BinaryIO, *, size: int, content_type: str) -> None:
        target = self.path(key)
        partial = self._partial_path(target)
        try:
            self.root.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            with os.fdopen(os.open(partial, flags, 0o600), "wb") as output:
                copyfileobj(file, output, length=1024 * 1024)
                if output.tell() != size:
                    raise StorageError
                output.flush()
                os.fsync(output.fileno())
            partial.replace(target)
        except OSError:
            raise StorageError from None

    def delete(self, key: str) -> None:
        target = self.path(key)
        partial = self._partial_path(target)
        try:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
        except OSError:
            raise StorageError from None
        with suppress(OSError):
            target.parent.rmdir()
