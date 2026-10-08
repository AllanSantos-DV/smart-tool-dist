"""Downloads a file in HTTP Range requests of CHUNK_BYTES, each on a new connection, resuming from the last byte
received when a connection drops or stalls. Some networks (proxies, TLS inspection) cut every connection after a few
hundred MB, so a single long transfer can never finish there. It gives up only after MAX_IDLE_ATTEMPTS attempts in a
row without a single new byte. Standard library only: on Windows urllib trusts the system certificate store, including
a corporate inspection CA, and honors HTTPS_PROXY."""
import http.client
import time
import urllib.error
import urllib.request

CHUNK_BYTES = 32 * 1024 * 1024
READ_BYTES = 256 * 1024
TIMEOUT_S = 60
MAX_IDLE_ATTEMPTS = 6
MAX_FULL_RESTARTS = 3
RETRYABLE_HTTP = {408, 429}


class DownloadError(RuntimeError):
    pass


def _open(url, start, end, timeout):
    request = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "SmartTool-installer"})
    return urllib.request.urlopen(request, timeout=timeout)


def download(url, out, progress=None, chunk_bytes=CHUNK_BYTES, timeout=TIMEOUT_S, attempts=MAX_IDLE_ATTEMPTS,
             sleep=time.sleep):
    """Writes url into the seekable binary file out and returns its size; progress(done, total) after every read."""
    done, total, idle, restarts = 0, None, 0, 0
    while total is None or done < total:
        end = done + chunk_bytes - 1 if total is None else min(done + chunk_bytes, total) - 1
        before = done
        try:
            with _open(url, done, end, timeout) as response:
                if response.status == 200:
                    if done:
                        restarts += 1
                        if restarts > MAX_FULL_RESTARTS:
                            raise DownloadError(f"{url} ignores Range requests and broke {restarts} times; giving up.")
                        out.seek(0)
                        out.truncate()
                        done = before = 0
                    length = response.headers.get("Content-Length")
                    total = int(length) if length else None
                elif response.status == 206:
                    total = int(response.headers["Content-Range"].rsplit("/", 1)[1])
                else:
                    raise DownloadError(f"{url} answered HTTP {response.status} to a Range request.")
                out.seek(done)
                while data := response.read1(READ_BYTES):
                    out.write(data)
                    done += len(data)
                    if progress:
                        progress(done, total or 0)
                if response.status == 200 and total is None:
                    total = done
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code < 500 and exc.code not in RETRYABLE_HTTP:
                raise DownloadError(f"{url} answered HTTP {exc.code}.") from exc
            error = exc
        except (OSError, http.client.HTTPException) as exc:
            error = exc
        else:
            error = None
        if done > before:
            idle = 0
        elif error is not None or done < (total or 0):
            idle += 1
            if idle >= attempts:
                raise DownloadError(f"Download stopped at {done // 1048576} of {(total or 0) // 1048576} MB after "
                                    f"{attempts} attempts without progress: {error or 'empty response'}")
            sleep(min(2 ** idle, 30))
    out.flush()
    out.seek(0)
    return done
