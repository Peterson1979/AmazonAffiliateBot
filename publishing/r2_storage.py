"""Cloudflare R2 storage integration for public creative assets.

Reuses the existing LifeMode R2 bucket ('lifemode-assets'), credentials,
endpoint configuration, and public HTTPS base URL.
Creatives are stored under the object-key prefix:
    amazon-affiliate-bot/creatives/<ASIN>/<filename>
"""
from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

DEFAULT_KEY_PREFIX = "amazon-affiliate-bot/creatives"
DEFAULT_VIDEO_KEY_PREFIX = "amazon-affiliate-bot/videos"
DEFAULT_BUCKET_NAME = "lifemode-assets"
ALLOWED_VIDEO_EXTENSIONS = (".mp4", ".mov")
DEFAULT_VIDEO_UPLOAD_TIMEOUT = 300
DEFAULT_MAX_VIDEO_SIZE_BYTES = 100 * 1024 * 1024  # 100 MB


class R2Error(RuntimeError):
    """Base exception for Cloudflare R2 operations."""
    pass


class R2ConfigError(R2Error):
    """Raised when required R2 configuration is missing or invalid."""
    pass


class R2UploadError(R2Error):
    """Raised when an object upload to R2 fails."""
    pass


@dataclass(frozen=True)
class R2Config:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket_name: str
    public_base_url: str
    endpoint: str = ""

    @classmethod
    def from_env(cls) -> "R2Config":
        """Load R2 configuration from environment variables with fallback to LifeMode .env."""
        account_id = os.getenv("R2_ACCOUNT_ID", "").strip()
        access_key = os.getenv("R2_ACCESS_KEY_ID", "").strip()
        secret_key = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
        bucket_name = os.getenv("R2_BUCKET_NAME", "").strip() or DEFAULT_BUCKET_NAME
        public_base_url = os.getenv("R2_PUBLIC_BASE_URL", "").strip()
        endpoint = os.getenv("R2_ENDPOINT", "").strip()

        # If any essential value is missing from environment, attempt safe fallback to LifeMode .env
        if not (account_id and access_key and secret_key and public_base_url):
            lifemode_env_path = Path(r"C:\Users\opeti\LifeMode\lifemode\.env")
            if lifemode_env_path.is_file():
                try:
                    from dotenv import dotenv_values
                    lm_vals = dotenv_values(lifemode_env_path)
                    account_id = account_id or lm_vals.get("R2_ACCOUNT_ID", "").strip()
                    access_key = access_key or lm_vals.get("R2_ACCESS_KEY_ID", "").strip()
                    secret_key = secret_key or lm_vals.get("R2_SECRET_ACCESS_KEY", "").strip()
                    bucket_name = bucket_name or lm_vals.get("R2_BUCKET_NAME", "").strip() or DEFAULT_BUCKET_NAME
                    public_base_url = public_base_url or lm_vals.get("R2_PUBLIC_BASE_URL", "").strip()
                    endpoint = endpoint or lm_vals.get("R2_ENDPOINT", "").strip()
                except Exception:
                    pass

        if not endpoint and account_id:
            endpoint = f"https://{account_id}.r2.cloudflarestorage.com"

        return cls(
            account_id=account_id,
            access_key_id=access_key,
            secret_access_key=secret_key,
            bucket_name=bucket_name,
            public_base_url=public_base_url,
            endpoint=endpoint,
        )

    def validate(self) -> None:
        """Validate that all required credentials and URLs are present."""
        missing = []
        if not self.account_id:
            missing.append("R2_ACCOUNT_ID")
        if not self.access_key_id:
            missing.append("R2_ACCESS_KEY_ID")
        if not self.secret_access_key:
            missing.append("R2_SECRET_ACCESS_KEY")
        if not self.bucket_name:
            missing.append("R2_BUCKET_NAME")
        if not self.public_base_url:
            missing.append("R2_PUBLIC_BASE_URL")

        if missing:
            raise R2ConfigError(f"Missing required Cloudflare R2 configuration: {', '.join(missing)}")

        if not (self.public_base_url.startswith("https://") or self.public_base_url.startswith("http://")):
            raise R2ConfigError(f"R2_PUBLIC_BASE_URL must be a valid HTTP(S) URL: '{self.public_base_url}'")


def get_creative_object_key(
    asin: str,
    filename_or_path: Union[str, Path],
    prefix: str = DEFAULT_KEY_PREFIX,
) -> str:
    """Generate object key following: amazon-affiliate-bot/creatives/<ASIN>/<filename>."""
    clean_asin = str(asin or "UNKNOWN").strip().upper()
    filename = Path(filename_or_path).name
    return f"{prefix.strip('/')}/{clean_asin}/{filename}"


def get_public_creative_url(public_base_url: str, object_key: str) -> str:
    """Resolve full public HTTPS URL from public base URL and object key."""
    base = public_base_url.rstrip("/")
    key = object_key.lstrip("/")
    return f"{base}/{key}"


def get_image_content_type(filename_or_path: Union[str, Path]) -> str:
    """Determine MIME content-type based on image file extension."""
    suffix = Path(filename_or_path).suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    if suffix == ".webp":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(str(filename_or_path))
    return guessed or "application/octet-stream"


def sanitize_video_filename(filename_or_path: Union[str, Path]) -> str:
    """Return a clean, URL-safe video filename preserving its original extension."""
    raw_name = Path(str(filename_or_path or "")).name.strip()
    if not raw_name:
        return "video.mp4"
    stem = Path(raw_name).stem
    suffix = Path(raw_name).suffix.lower()
    clean_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or "video"
    return f"{clean_stem}{suffix}"


def get_video_object_key(
    asin: str,
    filename_or_path: Union[str, Path],
    prefix: str = DEFAULT_VIDEO_KEY_PREFIX,
) -> str:
    """Generate object key following: amazon-affiliate-bot/videos/<ASIN>/<filename>."""
    safe_name = sanitize_video_filename(filename_or_path)
    return get_creative_object_key(asin=asin, filename_or_path=safe_name, prefix=prefix)


def get_video_content_type(filename_or_path: Union[str, Path]) -> str:
    """Determine MIME content-type for supported video extensions (.mp4, .mov)."""
    suffix = Path(filename_or_path).suffix.lower()
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".mov":
        return "video/quicktime"
    guessed, _ = mimetypes.guess_type(str(filename_or_path))
    return guessed or "application/octet-stream"


def get_max_video_size_bytes(override: Optional[int] = None) -> int:
    """Resolve configurable maximum video file size in bytes (defaults to 100 MB)."""
    if override is not None:
        val = int(override)
        if val <= 0:
            raise ValueError("max_size_bytes must be a positive integer.")
        return val
    env_bytes = os.getenv("R2_MAX_VIDEO_SIZE_BYTES", "").strip()
    if env_bytes:
        try:
            parsed = int(env_bytes)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    env_mb = os.getenv("R2_MAX_VIDEO_SIZE_MB", "").strip()
    if env_mb:
        try:
            parsed_mb = float(env_mb)
            if parsed_mb > 0:
                return int(parsed_mb * 1024 * 1024)
        except ValueError:
            pass
    return DEFAULT_MAX_VIDEO_SIZE_BYTES


def get_video_upload_timeout(override: Optional[int] = None) -> int:
    """Resolve configurable video upload timeout in seconds (defaults to 300s)."""
    if override is not None:
        val = int(override)
        if val <= 0:
            raise ValueError("timeout must be a positive integer.")
        return val
    env_timeout = os.getenv("R2_VIDEO_UPLOAD_TIMEOUT", "").strip()
    if env_timeout:
        try:
            parsed = int(env_timeout)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return DEFAULT_VIDEO_UPLOAD_TIMEOUT


def validate_video_file(
    filename_or_path: Union[str, Path],
    size_bytes: int,
    max_size_bytes: Optional[int] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate video filename extension (.mp4, .mov) and non-zero bounded file size."""
    raw_name = Path(str(filename_or_path or "")).name.strip()
    if not raw_name:
        return False, "Video filename is missing."
    suffix = Path(raw_name).suffix.lower()
    if suffix not in ALLOWED_VIDEO_EXTENSIONS:
        allowed_str = ", ".join(ext.upper().lstrip(".") for ext in ALLOWED_VIDEO_EXTENSIONS)
        return False, f"Unsupported video format '{suffix or raw_name}'. Allowed formats: {allowed_str}."
    if size_bytes <= 0:
        return False, "Uploaded video file is empty (0 bytes)."
    limit_bytes = get_max_video_size_bytes(max_size_bytes)
    if size_bytes > limit_bytes:
        size_mb = size_bytes / (1024 * 1024)
        limit_mb = limit_bytes / (1024 * 1024)
        return (
            False,
            f"Video file size ({size_mb:.1f} MB) exceeds the configured maximum limit of {limit_mb:.1f} MB.",
        )
    return True, None


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


class R2Client:
    """Minimal S3-compatible client for Cloudflare R2 using standard library SigV4."""
    service = "s3"
    region = "auto"

    def __init__(
        self,
        config: R2Config,
        opener: Optional[Callable[..., Any]] = None,
    ):
        config.validate()
        self.config = config
        self.opener = opener or urllib.request.urlopen
        self.host = urllib.parse.urlparse(config.endpoint).netloc

    def _signing_key(self, day: str) -> bytes:
        date_key = _hmac(("AWS4" + self.config.secret_access_key).encode("utf-8"), day)
        return _hmac(_hmac(_hmac(date_key, self.region), self.service), "aws4_request")

    def _canonical_path(self, key: str) -> str:
        return "/" + urllib.parse.quote(self.config.bucket_name + "/" + key.lstrip("/"), safe="/-_.~")

    def _signed_request(
        self,
        method: str,
        key: str,
        body: bytes = b"",
        headers: Optional[Dict[str, str]] = None,
        query: Optional[Dict[str, str]] = None,
        timeout: int = 60,
    ) -> Tuple[int, bytes]:
        req_headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
        payload_hash = hashlib.sha256(body).hexdigest()
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        day = now.strftime("%Y%m%d")

        req_headers.update({
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        })

        canonical_headers = "".join(f"{k}:{' '.join(v.split())}\n" for k, v in sorted(req_headers.items()))
        signed_headers = ";".join(sorted(req_headers.keys()))
        canonical_query = urllib.parse.urlencode(sorted((query or {}).items()))
        canonical_uri = self._canonical_path(key)
        canonical_request = "\n".join((
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ))

        scope = f"{day}/{self.region}/{self.service}/aws4_request"
        string_to_sign = "\n".join((
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ))

        signature = hmac.new(self._signing_key(day), string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        req_headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.config.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        url = self.config.endpoint.rstrip("/") + canonical_uri
        if canonical_query:
            url += "?" + canonical_query

        request = urllib.request.Request(url, data=body or None, method=method, headers=req_headers)
        try:
            with self.opener(request, timeout=timeout) as response:
                status = getattr(response, "status", getattr(response, "code", 200))
                return status, response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return 404, b""
            err_body = exc.read().decode("utf-8", errors="replace")[:200]
            raise R2UploadError(f"R2 {method} operation failed (HTTP {exc.code}): {err_body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise R2UploadError(f"R2 {method} network failure: {exc}") from exc

    def head_object(self, key: str) -> int:
        """Check if an object exists on R2."""
        return self._signed_request("HEAD", key)[0]

    def put_object(
        self,
        key: str,
        body: bytes,
        content_type: str = "application/octet-stream",
        timeout: int = 60,
    ) -> int:
        """Upload raw bytes to R2."""
        status, _ = self._signed_request(
            "PUT",
            key,
            body=body,
            headers={"content-type": content_type},
            timeout=timeout,
        )
        if status not in (200, 201):
            raise R2UploadError(f"R2 PUT request returned unexpected status {status}")
        return status


def upload_creative_to_r2(
    asin: str,
    local_image_path: Union[str, Path],
    dry_run: bool = False,
    config: Optional[R2Config] = None,
    client: Optional[R2Client] = None,
) -> Dict[str, Any]:
    """
    Upload an existing local creative file to R2 without altering its bytes.

    Returns:
        Dict with keys: success, object_key, public_url, and optional error.
    """
    path = Path(local_image_path)
    if not path.is_file():
        return {
            "success": False,
            "error": f"Local creative file does not exist on disk: '{local_image_path}'",
        }

    try:
        cfg = config or R2Config.from_env()
        cfg.validate()
    except Exception as exc:
        return {
            "success": False,
            "error": f"Cloudflare R2 configuration invalid: {exc}",
        }

    object_key = get_creative_object_key(asin, path)
    public_url = get_public_creative_url(cfg.public_base_url, object_key)

    if dry_run:
        return {
            "success": True,
            "object_key": object_key,
            "public_url": public_url,
            "dry_run": True,
            "uploaded": False,
        }

    try:
        with open(path, "rb") as handle:
            file_bytes = handle.read()

        content_type = get_image_content_type(path)
        r2_client = client or R2Client(cfg)
        r2_client.put_object(object_key, file_bytes, content_type=content_type)

        return {
            "success": True,
            "object_key": object_key,
            "public_url": public_url,
            "size_bytes": len(file_bytes),
            "content_type": content_type,
            "dry_run": False,
            "uploaded": True,
        }
    except Exception as exc:
        return {
            "success": False,
            "object_key": object_key,
            "error": f"R2 upload failed: {exc}",
        }


def upload_video_to_r2(
    asin: str,
    local_video_path: Union[str, Path],
    filename_override: Optional[str] = None,
    dry_run: bool = False,
    max_size_bytes: Optional[int] = None,
    timeout: Optional[int] = None,
    config: Optional[R2Config] = None,
    client: Optional[R2Client] = None,
) -> Dict[str, Any]:
    """
    Validate and upload a local MP4/MOV video file unchanged to Cloudflare R2
    under amazon-affiliate-bot/videos/<ASIN>/<filename>.
    """
    path = Path(local_video_path)
    if not path.is_file():
        return {
            "success": False,
            "error": f"Local video file does not exist on disk: '{local_video_path}'",
        }

    target_filename = filename_override or path.name
    try:
        file_size = path.stat().st_size
        is_valid, validation_error = validate_video_file(
            target_filename,
            size_bytes=file_size,
            max_size_bytes=max_size_bytes,
        )
    except Exception as exc:
        return {
            "success": False,
            "error": f"Video validation failed: {exc}",
        }

    if not is_valid:
        return {
            "success": False,
            "error": validation_error or "Invalid video file.",
        }

    try:
        cfg = config or R2Config.from_env()
        cfg.validate()
        effective_timeout = get_video_upload_timeout(timeout)
    except Exception as exc:
        return {
            "success": False,
            "error": f"Cloudflare R2 configuration invalid: {exc}",
        }

    object_key = get_video_object_key(asin, target_filename)
    public_url = get_public_creative_url(cfg.public_base_url, object_key)
    content_type = get_video_content_type(target_filename)

    if dry_run:
        return {
            "success": True,
            "object_key": object_key,
            "public_url": public_url,
            "size_bytes": file_size,
            "content_type": content_type,
            "dry_run": True,
            "uploaded": False,
        }

    try:
        with open(path, "rb") as handle:
            file_bytes = handle.read()

        r2_client = client or R2Client(cfg)
        r2_client.put_object(
            object_key,
            file_bytes,
            content_type=content_type,
            timeout=effective_timeout,
        )

        return {
            "success": True,
            "object_key": object_key,
            "public_url": public_url,
            "size_bytes": len(file_bytes),
            "content_type": content_type,
            "dry_run": False,
            "uploaded": True,
        }
    except Exception as exc:
        return {
            "success": False,
            "object_key": object_key,
            "error": f"R2 video upload failed: {exc}",
        }


