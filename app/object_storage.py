"""Accès privé au stockage objet S3 compatible, partagé entre API et workers."""

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID, uuid4


BUCKET_PATTERN = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
OBJECT_KEY_PATTERN = re.compile(
    r"projects/[0-9a-f-]{36}/(?:sources|snapshots|exports)/[0-9a-f-]{36}\Z"
)
MAX_OBJECT_BYTES = 25 * 1024 * 1024


class StorageUnavailable(Exception):
    """Erreur minimisée ; aucun détail boto3 ou secret ne doit remonter aux jobs."""


@dataclass(frozen=True)
class StorageConfig:
    endpoint_url: str
    bucket: str
    region: str
    access_key: str
    secret_key: str

    @classmethod
    def from_env(cls) -> "StorageConfig":
        endpoint = os.environ.get("MIMIC_S3_ENDPOINT_URL", "").rstrip("/")
        bucket = os.environ.get("MIMIC_S3_BUCKET", "")
        region = os.environ.get("MIMIC_S3_REGION", "us-east-1")
        access_key = os.environ.get("MIMIC_S3_ACCESS_KEY", "")
        secret_key = os.environ.get("MIMIC_S3_SECRET_KEY", "")
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.query or parsed.fragment or
            any(character.isspace() for character in endpoint) or "\\" in endpoint):
            raise ValueError("Adresse de stockage S3 invalide.")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Port de stockage S3 invalide.") from exc
        if parsed.scheme == "http" and (os.environ.get("MIMIC_S3_ALLOW_HTTP") != "true" or
                                        parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("HTTP n'est permis que pour un émulateur S3 local explicite.")
        if not BUCKET_PATTERN.fullmatch(bucket) or not region or not access_key or not secret_key:
            raise ValueError("Configuration S3 incomplète.")
        return cls(endpoint, bucket, region, access_key, secret_key)


def new_object_key(project_id: str, category: str) -> str:
    """La clé est opaque et ne contient jamais le nom du fichier utilisateur."""
    project_uuid = UUID(project_id)
    if category not in {"sources", "snapshots", "exports"}:
        raise ValueError("Catégorie de stockage inconnue.")
    return f"projects/{project_uuid}/{category}/{uuid4()}"


def valid_object_key(key: str) -> bool:
    if not OBJECT_KEY_PATTERN.fullmatch(key):
        return False
    _, project_id, _, object_id = key.split("/")
    try:
        UUID(project_id)
        UUID(object_id)
    except ValueError:
        return False
    return True


class ObjectStorage:
    def __init__(self, config: StorageConfig, client=None):
        self.config = config
        if client is None:
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3", endpoint_url=config.endpoint_url, region_name=config.region,
                aws_access_key_id=config.access_key, aws_secret_access_key=config.secret_key,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                              connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
            )
        self.client = client

    @classmethod
    def from_env(cls) -> "ObjectStorage":
        return cls(StorageConfig.from_env())

    def check_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.config.bucket)
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None

    def put_bytes(self, key: str, content: bytes, mime_type: str) -> str:
        if not valid_object_key(key) or not isinstance(content, bytes) or not 0 < len(content) <= MAX_OBJECT_BYTES:
            raise ValueError("Objet invalide ou trop volumineux.")
        if not mime_type or len(mime_type) > 120 or any(c in mime_type for c in "\r\n"):
            raise ValueError("Type MIME invalide.")
        try:
            self.client.put_object(Bucket=self.config.bucket, Key=key, Body=content,
                                   ContentType=mime_type, ContentLength=len(content))
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None
        return hashlib.sha256(content).hexdigest()

    def verify(self, key: str, expected_sha256: str) -> dict:
        if not valid_object_key(key) or not SHA256_PATTERN.fullmatch(expected_sha256):
            raise ValueError("Référence d'objet invalide.")
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            body = response["Body"]
            digest = hashlib.sha256()
            size = 0
            try:
                while chunk := body.read(64 * 1024):
                    size += len(chunk)
                    if size > MAX_OBJECT_BYTES:
                        raise ValueError("Objet trop volumineux.")
                    digest.update(chunk)
            finally:
                body.close()
        except ValueError:
            raise
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None
        actual = digest.hexdigest()
        return {"sha256": actual, "size_bytes": size,
                "valid": hmac.compare_digest(actual, expected_sha256)}

    def get_bytes(self, key: str) -> bytes:
        if not valid_object_key(key):
            raise ValueError("Référence d'objet invalide.")
        try:
            response = self.client.get_object(Bucket=self.config.bucket, Key=key)
            body = response["Body"]
            try:
                content = body.read(MAX_OBJECT_BYTES + 1)
            finally:
                body.close()
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None
        if len(content) > MAX_OBJECT_BYTES:
            raise ValueError("Objet trop volumineux.")
        return content

    def delete(self, key: str) -> None:
        if not valid_object_key(key):
            raise ValueError("Référence d'objet invalide.")
        try:
            self.client.delete_object(Bucket=self.config.bucket, Key=key)
        except Exception:
            raise StorageUnavailable("storage_unavailable") from None
