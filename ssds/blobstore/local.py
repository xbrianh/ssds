import os
from math import ceil
from functools import wraps
from typing import Generator, Optional

from ssds.blobstore import (BlobStore, Blob, AsyncPartIterator, Part, MultipartWriter, get_s3_multipart_chunk_size,
                            BlobNotFoundError, BlobStoreUnknownError)


def catch_blob_not_found(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except FileNotFoundError as ex:
            raise BlobNotFoundError(f"Could not find {self.key}") from ex
    return wrapper

class LocalBlobStore(BlobStore):
    def __init__(self, basepath: str):
        self.bucket_name = basepath

    def list(self, prefix: str="") -> Generator["LocalBlob", None, None]:
        for (dirpath, dirnames, filenames) in os.walk(os.path.join(self.bucket_name, prefix)):
            for filename in filenames:
                relpath = os.path.relpath(os.path.join(dirpath, filename), self.bucket_name)
                yield LocalBlob(self.bucket_name, relpath)

    def blob(self, key: str) -> "LocalBlob":
        return LocalBlob(self.bucket_name, key)

class LocalBlob(Blob):
    def __init__(self, basepath: str, relpath: str):
        assert basepath == os.path.abspath(basepath)
        self.bucket_name = basepath
        self.key = relpath
        self._path = os.path.join(basepath, relpath)

    @catch_blob_not_found
    def get(self) -> bytes:
        with open(self._path, "rb") as fh:
            return fh.read()

    @catch_blob_not_found
    def size(self) -> int:
        return os.path.getsize(self._path)

    def parts(self, threads: Optional[int]=None):
        return LocalAsyncPartIterator(self._path)

class LocalAsyncPartIterator:
    def __init__(self, path: str):
        try:
            self.size = os.path.getsize(path)
        except FileNotFoundError:
            raise BlobNotFoundError(f"Could not find {path}")
        self.chunk_size = get_s3_multipart_chunk_size(self.size)
        self._number_of_parts = ceil(self.size / self.chunk_size) if 0 < self.size else 1
        self.handle = open(path, "rb")

    def __len__(self):
        return self._number_of_parts

    def __iter__(self) -> Generator[Part, None, None]:
        for part_number in range(self._number_of_parts):
            yield self._get_part(part_number)

    def _get_part(self, part_number: int) -> Part:
        self.handle.seek(part_number * self.chunk_size)
        return Part(part_number, self.handle.read(self.chunk_size))

    def close(self):
        if hasattr(self, "handle"):
            self.handle.close()

    def __del__(self):
        self.close()
