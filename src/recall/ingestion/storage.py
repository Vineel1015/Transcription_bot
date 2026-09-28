"""Media storage for uploaded recordings: local disk (default), Azure Blob (production) or S3-compatible."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from pathlib import Path
from typing import Protocol

from recall.config import get_settings


class MediaStore(Protocol):
    async def put(self, name: str, data: bytes) -> str: ...
    async def get(self, path: str) -> bytes: ...
    async def delete(self, path: str) -> None: ...


class LocalStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    async def put(self, name: str, data: bytes) -> str:
        path = self.root / f"{uuid.uuid4().hex}-{Path(name).name}"

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(write)
        return str(path)

    async def get(self, path: str) -> bytes:
        return await asyncio.to_thread(Path(path).read_bytes)

    async def delete(self, path: str) -> None:
        await asyncio.to_thread(Path(path).unlink, missing_ok=True)


class AzureBlobStore:
    """Uses DefaultAzureCredential (managed identity in Azure). A lifecycle rule on the container also deletes
    blobs after the media retention period."""

    def __init__(self, account_url: str, container: str) -> None:
        from azure.identity.aio import DefaultAzureCredential
        from azure.storage.blob.aio import BlobServiceClient

        self._service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self._container = container

    async def put(self, name: str, data: bytes) -> str:
        blob = f"{dt.date.today():%Y/%m/%d}/{uuid.uuid4().hex}-{Path(name).name}"
        await self._service.get_blob_client(self._container, blob).upload_blob(data)
        return blob

    async def get(self, path: str) -> bytes:
        stream = await self._service.get_blob_client(self._container, path).download_blob()
        data: bytes = await stream.readall()
        return data

    async def delete(self, path: str) -> None:
        await self._service.get_blob_client(self._container, path).delete_blob(delete_snapshots="include")


class S3Store:
    def __init__(self, bucket: str, endpoint_url: str | None, region: str | None) -> None:
        import boto3

        self._client = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region or None)
        self._bucket = bucket

    async def put(self, name: str, data: bytes) -> str:
        key = f"{uuid.uuid4().hex}-{Path(name).name}"
        await asyncio.to_thread(self._client.put_object, Bucket=self._bucket, Key=key, Body=data)
        return key

    async def get(self, path: str) -> bytes:
        obj = await asyncio.to_thread(self._client.get_object, Bucket=self._bucket, Key=path)
        body: bytes = obj["Body"].read()
        return body

    async def delete(self, path: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=self._bucket, Key=path)


_store: MediaStore | None = None


def get_store() -> MediaStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.media_storage == "azure_blob":
            _store = AzureBlobStore(s.azure_storage_account_url, s.azure_storage_container)
        elif s.media_storage == "s3":
            _store = S3Store(s.s3_bucket, s.s3_endpoint_url, s.s3_region)
        else:
            _store = LocalStore(s.media_local_dir)
    return _store
