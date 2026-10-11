import hashlib
import io
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import boto3
import httpx
import pytest

from app.object_storage import ObjectStorage, StorageConfig, new_object_key


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs):
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]])}

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"])


def test_stockage_prive_empreinte_et_cles_opaques():
    fake = FakeS3()
    config = StorageConfig("https://s3.example.org", "mimic-sources", "us-east-1", "ref", "secret")
    storage = ObjectStorage(config, client=fake)
    key = new_object_key(str(uuid4()), "sources")
    digest = storage.put_bytes(key, b"Document source", "text/markdown")
    assert digest == hashlib.sha256(b"Document source").hexdigest()
    assert storage.verify(key, digest)["valid"]
    assert not storage.verify(key, "0" * 64)["valid"]
    assert "Document source" not in key
    with pytest.raises(ValueError):
        storage.put_bytes("projects/../../fuite", b"x", "text/plain")
    storage.delete(key)
    assert not fake.objects


def test_configuration_s3_exige_https_sauf_choix_local(monkeypatch):
    monkeypatch.setenv("MIMIC_S3_ENDPOINT_URL", "http://127.0.0.1:55000")
    monkeypatch.setenv("MIMIC_S3_BUCKET", "mimic-sources")
    monkeypatch.setenv("MIMIC_S3_ACCESS_KEY", "acces-test")
    monkeypatch.setenv("MIMIC_S3_SECRET_KEY", "secret-test")
    monkeypatch.delenv("MIMIC_S3_ALLOW_HTTP", raising=False)
    with pytest.raises(ValueError):
        StorageConfig.from_env()
    monkeypatch.setenv("MIMIC_S3_ALLOW_HTTP", "true")
    assert StorageConfig.from_env().bucket == "mimic-sources"
    monkeypatch.setenv("MIMIC_S3_ENDPOINT_URL", "http://storage.example.org:9000")
    with pytest.raises(ValueError):
        StorageConfig.from_env()


def test_job_redis_et_s3_de_bout_en_bout(monkeypatch):
    endpoint = os.environ.get("MIMIC_TEST_S3_ENDPOINT_URL")
    redis_url = os.environ.get("MIMIC_TEST_REDIS_URL")
    if not endpoint or not redis_url:
        pytest.skip("MIMIC_TEST_S3_ENDPOINT_URL ou MIMIC_TEST_REDIS_URL non définie")
    bucket = f"mimic-sources-{uuid4().hex[:12]}"
    access_key = os.environ.get("MIMIC_S3_ACCESS_KEY")
    secret_key = os.environ.get("MIMIC_S3_SECRET_KEY")
    if not access_key or not secret_key:
        pytest.skip("Identifiants MinIO de test non définis")
    env = {**os.environ, "MIMIC_S3_ENDPOINT_URL": endpoint,
           "MIMIC_S3_BUCKET": bucket, "MIMIC_S3_REGION": "us-east-1",
           "MIMIC_S3_ACCESS_KEY": access_key, "MIMIC_S3_SECRET_KEY": secret_key,
           "MIMIC_S3_ALLOW_HTTP": "true", "MIMIC_REDIS_URL": redis_url}
    for name in ("MIMIC_S3_ENDPOINT_URL", "MIMIC_S3_BUCKET", "MIMIC_S3_REGION",
                 "MIMIC_S3_ACCESS_KEY", "MIMIC_S3_SECRET_KEY", "MIMIC_S3_ALLOW_HTTP",
                 "MIMIC_REDIS_URL"):
        monkeypatch.setenv(name, env[name])
    s3 = boto3.client("s3", endpoint_url=endpoint, region_name="us-east-1",
                      aws_access_key_id=access_key, aws_secret_access_key=secret_key)
    s3.create_bucket(Bucket=bucket)

    from app.worker import celery_app

    celery_app.conf.broker_url = redis_url
    celery_app.conf.result_backend = redis_url
    storage = ObjectStorage.from_env()
    key = new_object_key(str(uuid4()), "sources")
    digest = storage.put_bytes(key, b"Proposition non validee", "text/markdown")
    anonymous = httpx.get(f"{endpoint}/{bucket}/{key}", timeout=5)
    assert anonymous.status_code == 403
    command = [sys.executable, "-m", "celery", "-A", "app.worker:celery_app", "worker",
               "--pool=solo", "--concurrency=1", "--loglevel=WARNING", "--hostname=mimic-test@%h"]
    worker = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(20):
            if celery_app.control.ping(timeout=1):
                break
            time.sleep(0.25)
        else:
            pytest.fail("Worker Celery indisponible")
        result = celery_app.send_task("mimic.storage.verify", args=[key, digest])
        assert result.get(timeout=20, propagate=True) == {
            "sha256": digest, "size_bytes": len(b"Proposition non validee"), "valid": True}
        repeated = celery_app.send_task("mimic.storage.verify", args=[key, digest])
        assert repeated.get(timeout=20, propagate=True)["valid"]
    finally:
        worker.terminate()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait(timeout=5)
        storage.delete(key)
        s3.delete_bucket(Bucket=bucket)
