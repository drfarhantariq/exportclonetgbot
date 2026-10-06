"""Large Google Drive range requests with streaming progress and bounded retries."""
from __future__ import annotations

import re
import time
from pathlib import Path

import requests

DOWNLOAD_CHUNK_SIZE = 100 * 1024 * 1024  # Same request size as WZML.
STREAM_BLOCK_SIZE = 1024 * 1024


def _retryable(response):
    if response.status_code in {429, 500, 502, 503, 504}:
        return True
    if response.status_code == 403:
        try:
            errors = response.json().get("error", {}).get("errors", [])
            return any(item.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded", "backendError"}
                       for item in errors)
        except (ValueError, AttributeError, TypeError):
            pass
    return False


def download(session, url, output_path: Path, *, total_size=None, chunk_size=DOWNLOAD_CHUNK_SIZE,
             progress_callback=None, max_retries=5):
    if chunk_size <= 0:
        raise ValueError("Download chunk size must be positive.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".part")
    done = 0
    retries = 0
    etag = None
    with temp_path.open("wb") as handle:
        while total_size is None or done < total_size:
            start = done
            end = start + chunk_size - 1
            if total_size is not None:
                end = min(end, total_size - 1)
            headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
            if etag:
                headers["If-Range"] = etag
            response = None
            try:
                response = session.get(url, headers=headers, stream=True, timeout=(30, 120))
                if _retryable(response):
                    raise requests.ConnectionError(f"Google Drive temporarily unavailable (HTTP {response.status_code}).")
                response.raise_for_status()
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", response.headers.get("Content-Range", ""))
                    if not match or int(match[1]) != start or int(match[2]) > end:
                        raise RuntimeError("Google Drive returned an unexpected download range.")
                    response_end = int(match[2]) + 1
                    if response_end <= start:
                        raise RuntimeError("Google Drive returned an empty download range.")
                    remote_size = int(match[3]) if match[3] != "*" else None
                elif response.status_code == 200 and start == 0:
                    remote_size = int(response.headers["Content-Length"]) if response.headers.get("Content-Length") else None
                    response_end = remote_size
                else:
                    raise RuntimeError("Google Drive did not honor the requested download range.")
                if remote_size is not None:
                    if total_size is not None and remote_size != total_size:
                        raise RuntimeError("Google Drive file size changed during download.")
                    total_size = remote_size
                remote_etag = response.headers.get("ETag")
                if etag and remote_etag and remote_etag != etag:
                    raise RuntimeError("Google Drive file changed during download.")
                etag = etag or remote_etag
                for block in response.iter_content(chunk_size=STREAM_BLOCK_SIZE):
                    if not block:
                        continue
                    if (response_end is not None and done + len(block) > response_end) or (
                            total_size is not None and done + len(block) > total_size):
                        raise RuntimeError("Google Drive sent more bytes than requested.")
                    handle.write(block)
                    done += len(block)
                    if progress_callback:
                        progress_callback(done, total_size)
                if response_end is not None and done != response_end:
                    raise requests.ConnectionError("Google Drive download response was incomplete.")
                if response.status_code == 200:
                    if total_size is None:
                        total_size = done
                    if done != total_size:
                        raise requests.ConnectionError("Google Drive download was incomplete.")
                elif done == start:
                    raise requests.ConnectionError("Google Drive download made no progress.")
                retries = 0
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError):
                retries += 1
                if retries > max_retries:
                    raise
                # Release sockets before backing off; retry from bytes already written.
                if response is not None:
                    response.close()
                    response = None
                time.sleep(min(6, 2 ** retries))
            finally:
                if response is not None:
                    response.close()
        if total_size is not None and done != total_size:
            raise RuntimeError("Google Drive downloaded size does not match the source.")
    temp_path.replace(output_path)
    if progress_callback:
        progress_callback(done, total_size)
    return output_path
