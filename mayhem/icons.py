"""Optional item artwork on a dedicated thread; no Tk or game-worker work."""
from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
from urllib.request import HTTPRedirectHandler, Request, build_opener

from PIL import Image


MAX_DOWNLOAD_BYTES = 512 * 1024
MAX_IMAGE_PIXELS = 1024 * 1024
MAX_IMAGE_EDGE = 1024
DOWNLOAD_TIMEOUT = 3
FAILURE_COOLDOWN = 60
MAX_PENDING = 128
_ICON_URL = re.compile(
    r"(?:https://data\.aramkit\.com/assets/item/[A-Za-z0-9][A-Za-z0-9_-]*-[0-9a-f]{12}\.webp"
    r"|https://ap\.tft\.tools/img/(?:gg[0-9]{1,2}/face|items_s[0-9]{1,2}|augments)/"
    r"[A-Za-z0-9][A-Za-z0-9_-]*\.(?:png|jpg|webp)\?w=64)\Z"
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _decode_image(payload: bytes) -> Image.Image:
    if not payload or len(payload) > MAX_DOWNLOAD_BYTES:
        raise ValueError("Invalid item image size")
    with Image.open(BytesIO(payload)) as source:
        width, height = source.size
        if (source.format not in ("WEBP", "PNG", "JPEG")
                or width < 1 or height < 1
                or max(width, height) > MAX_IMAGE_EDGE
                or width * height > MAX_IMAGE_PIXELS
                or getattr(source, "n_frames", 1) != 1):
            raise ValueError("Invalid item image dimensions or format")
        source.load()
        # Detached pixels can safely cross the queue; ImageTk creation belongs
        # to the main UI thread. Neither the decoder nor loader imports Tk.
        return source.convert("RGBA")


class IconLoader(threading.Thread):
    """Queue optional icons without doing network, disk, or decode work in request.

    Call start() once and request(url) from the UI. Success emits
    ('item_icon', (url, PIL.Image.Image)); failed requests remain silent. The UI
    should retain successful images by URL, since each URL is emitted once.
    """

    def __init__(self, output, stop, cache_dir):
        super().__init__(daemon=True, name="mayhem-item-icons")
        self.output, self.stop = output, stop
        self.cache_dir = Path(cache_dir)
        self._requests = queue.Queue(maxsize=MAX_PENDING)
        self._lock = threading.Lock()
        self._pending = set()
        self._complete = set()
        self._retry_after = {}

    def request(self, url) -> bool:
        """Nonblocking enqueue; false means invalid, duplicate, or cooling down."""
        if self.stop.is_set() or not isinstance(url, str) or not _ICON_URL.fullmatch(url):
            return False
        with self._lock:
            if (url in self._pending or url in self._complete
                    or time.monotonic() < self._retry_after.get(url, 0)):
                return False
            try:
                self._requests.put_nowait(url)
            except queue.Full:
                return False
            self._pending.add(url)
        return True

    def _cache_path(self, url):
        return self.cache_dir / (hashlib.sha256(url.encode("utf-8")).hexdigest() + ".img")

    def _download(self, url):
        # Recheck at the network boundary as well as at enqueue. No redirects
        # are followed, so responses cannot move requests off the asset host.
        if not isinstance(url, str) or not _ICON_URL.fullmatch(url):
            raise ValueError("Unsupported item image URL")
        request = Request(url, headers={"User-Agent": "MayhemCompanion/0.1", "Accept": "image/webp"})
        with build_opener(_NoRedirect()).open(request, timeout=DOWNLOAD_TIMEOUT) as response:
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(payload) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Item image exceeds the size limit")
        return payload

    def _write_cache(self, path, payload):
        temporary = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.cache_dir,
                                             prefix=".icon-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            os.replace(temporary, path)
        except OSError:
            # A read-only/full cache must not prevent an otherwise valid icon.
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _load(self, url):
        path = self._cache_path(url)
        try:
            with path.open("rb") as stream:
                cached = stream.read(MAX_DOWNLOAD_BYTES + 1)
            return _decode_image(cached)
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
            pass
        payload = self._download(url)
        image = _decode_image(payload)
        self._write_cache(path, payload)
        return image

    def run(self):
        while not self.stop.is_set():
            try:
                url = self._requests.get(timeout=0.2)
            except queue.Empty:
                continue
            success = False
            image = None
            try:
                if not self.stop.is_set():
                    image = self._load(url)
                    if not self.stop.is_set():
                        self.output.put_nowait(("item_icon", (url, image)))
                        success = True
            except Exception:
                # Textual item builds remain fully usable. Do not emit errors
                # or repeatedly retry a failed icon on every UI repaint.
                pass
            finally:
                if image is not None and not success:
                    image.close()
                with self._lock:
                    self._pending.discard(url)
                    if success:
                        self._complete.add(url)
                        self._retry_after.pop(url, None)
                    else:
                        self._retry_after[url] = time.monotonic() + FAILURE_COOLDOWN
                self._requests.task_done()
