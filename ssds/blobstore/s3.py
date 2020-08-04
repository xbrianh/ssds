import io
import requests
from math import ceil
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor, as_completed, Future
from typing import Set, List, Dict, Union, Generator

import botocore.exceptions

from ssds import aws
from ssds.blobstore import BlobStore, AsyncPartIterator, Part, MultipartWriter, get_s3_multipart_chunk_size

class S3BlobStore(BlobStore):
    schema = "s3://"

    def put_tags(self, bucket_name: str, key: str, tags: Dict[str, str]):
        aws_tags = [dict(Key=key, Value=val)
                    for key, val in tags.items()]
        aws.client("s3").put_object_tagging(Bucket=bucket_name, Key=key, Tagging=dict(TagSet=aws_tags))

    def get_tags(self, bucket_name: str, key: str) -> Dict[str, str]:
        tagset = aws.client("s3").get_object_tagging(Bucket=bucket_name, Key=key)
        return {tag['Key']: tag['Value']
                for tag in tagset['TagSet']}

    def list(self, bucket: str, prefix="") -> Generator[str, None, None]:
        for item in aws.resource("s3").Bucket(bucket).objects.filter(Prefix=prefix):
            yield item.key

    def get(self, bucket_name: str, key: str) -> bytes:
        with closing(aws.resource("s3").Bucket(bucket_name).Object(key).get()['Body']) as fh:
            return fh.read()

    def put(self, bucket_name: str, key: str, data: bytes):
        blob = aws.resource("s3").Bucket(bucket_name).Object(key)
        blob.upload_fileobj(io.BytesIO(data))

    def exists(self, bucket_name: str, key: str) -> bool:
        try:
            self.size(bucket_name, key)
            return True
        except botocore.exceptions.ClientError as e:
            if str(e.response['Error']['Code']) == str(requests.codes.not_found):
                return False
            else:
                raise

    def size(self, bucket_name: str, key: str) -> int:
        blob = aws.resource("s3").Bucket(bucket_name).Object(key)
        return blob.content_length

    def cloud_native_checksum(self, bucket_name: str, key: str) -> str:
        blob = aws.resource("s3").Bucket(bucket_name).Object(key)
        return blob.e_tag.strip("\"")

    def parts(self, bucket_name: str, key: str, executor: ThreadPoolExecutor=None) -> "S3AsyncPartIterator":
        return S3AsyncPartIterator(bucket_name, key, executor)

    def multipart_writer(self, bucket_name: str, key: str, executor: ThreadPoolExecutor=None) -> "MultipartWriter":
        return S3MultipartWriter(bucket_name, key, executor)

class S3AsyncPartIterator(AsyncPartIterator):
    parts_to_buffer = 2

    def __init__(self, bucket_name, key, executor: ThreadPoolExecutor=None):
        self._blob = aws.resource("s3").Bucket(bucket_name).Object(key)
        self.size = self._blob.content_length
        self.chunk_size = get_s3_multipart_chunk_size(self.size)
        self._number_of_parts = ceil(self.size / self.chunk_size)
        self._executor = executor or ThreadPoolExecutor(max_workers=4)

    def __iter__(self) -> Generator[Part, None, None]:
        if 1 == self._number_of_parts:
            yield self._get_part(0)
        else:
            futures: Set[Future] = set()
            part_numbers = [part_number for part_number in range(self._number_of_parts)]
            while part_numbers or futures:
                if len(futures) < self.parts_to_buffer:
                    number_of_parts_to_fetch = self.parts_to_buffer - len(futures)
                    for part_number in part_numbers[:number_of_parts_to_fetch]:
                        futures.add(self._executor.submit(self._get_part, part_number))
                    part_numbers = part_numbers[number_of_parts_to_fetch:]
                for f in as_completed(futures):
                    part = f.result()
                    futures.remove(f)
                    yield part
                    break  # Break out of inner loop to avoid waiting for `as_completed` to provide next future

    def _get_part(self, part_number: int) -> Part:
        offset = part_number * self.chunk_size
        byte_range = f"bytes={offset}-{offset + self.chunk_size - 1}"
        data = self._blob.get(Range=byte_range)['Body'].read()
        return Part(part_number, data)

class S3MultipartWriter(MultipartWriter):
    concurrent_uploads = 4

    def __init__(self, bucket_name: str, key: str, executor: ThreadPoolExecutor=None):
        self.bucket_name = bucket_name
        self.key = key
        self.mpu = aws.client("s3").create_multipart_upload(Bucket=bucket_name, Key=key)['UploadId']
        self.parts: List[Dict[str, Union[str, int]]] = list()
        self._closed = False
        self._executor = executor
        self._futures: Set[Future] = set()

    def _put_part(self, part: Part) -> Dict[str, Union[str, int]]:
        aws_part_number = part.number + 1
        resp = aws.client("s3").upload_part(
            Body=part.data,
            Bucket=self.bucket_name,
            Key=self.key,
            PartNumber=aws_part_number,
            UploadId=self.mpu,
        )
        return dict(ETag=resp['ETag'], PartNumber=aws_part_number)

    def put_part(self, part: Part):
        if self._executor:
            if self.concurrent_uploads <= len(self._futures):
                for _ in as_completed(self._futures):
                    break
            self._collect_parts()
            f = self._executor.submit(self._put_part, part)
            self._futures.add(f)
        else:
            self.parts.append(self._put_part(part))

    def _wait(self):
        """
        Wait for current part uploads to finish.
        """
        if self._executor:
            if self._futures:
                for f in as_completed(self._futures):
                    f.result()  # raises if future errored

    def _collect_parts(self):
        if self._executor:
            for f in self._futures.copy():
                if f.done():
                    self.parts.append(f.result())
                    self._futures.remove(f)

    def close(self):
        if not self._closed:
            self._closed = True
            if self._executor:
                self._wait()
                self._collect_parts()
            self.parts.sort(key=lambda item: item['PartNumber'])
            aws.client("s3").complete_multipart_upload(Bucket=self.bucket_name,
                                                       Key=self.key,
                                                       MultipartUpload=dict(Parts=self.parts),
                                                       UploadId=self.mpu)
