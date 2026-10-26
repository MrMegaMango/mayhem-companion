from io import BytesIO
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import URLError

from PIL import Image
from mayhem import icons

URL = "https://data.aramkit.com/assets/item/6653-b9135a38a6d5.webp"
OTHER = "https://data.aramkit.com/assets/item/3031-abcdef123456.webp"


def picture(size=(24, 24), format="PNG"):
    stream = BytesIO()
    with Image.new("RGB", size, (30, 80, 120)) as image:
        image.save(stream, format=format)
    return stream.getvalue()


class IconLoaderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "icons"

    def loader(self, download=None, start=True):
        output, stop = queue.Queue(), threading.Event()
        loader = icons.IconLoader(output, stop, self.cache)
        loader._download = Mock(return_value=picture()) if download is None else Mock(side_effect=download)
        def cleanup():
            stop.set()
            if loader.ident is not None:
                loader.join(4)
                self.assertFalse(loader.is_alive())
        self.addCleanup(cleanup)
        if start:
            loader.start()
        return loader, output, stop

    def finished(self, loader):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with loader._lock:
                pending = bool(loader._pending)
            if not pending:
                return
            time.sleep(0.005)
        self.fail("Icon work did not finish")

    def test_request_is_deduplicated_without_disk_or_network_work(self):
        loader, output, _ = self.loader(start=False)
        self.assertTrue(loader.request(URL))
        self.assertFalse(loader.request(URL))
        self.assertFalse(self.cache.exists())
        loader._download.assert_not_called()
        loader.start()
        kind, (url, image) = output.get(timeout=2)
        self.assertEqual((kind, url, image.mode, image.size), ("item_icon", URL, "RGBA", (24, 24)))
        self.assertEqual(image.getpixel((0, 0)), (30, 80, 120, 255))
        self.finished(loader)
        self.assertFalse(loader.request(URL))
        loader._download.assert_called_once_with(URL)
        self.assertTrue(output.empty())

    def test_slow_network_never_blocks_other_requests(self):
        entered, release = threading.Event(), threading.Event()
        def download(url):
            entered.set()
            self.assertTrue(release.wait(2))
            return picture()
        loader, output, _ = self.loader(download)
        loader.request(URL)
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        try:
            self.assertTrue(loader.request(OTHER))
            self.assertFalse(loader.request(URL))
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
        self.assertEqual({output.get(timeout=2)[1][0], output.get(timeout=2)[1][0]}, {URL, OTHER})

    def test_cache_is_atomic_validated_and_reused_offline(self):
        loader, output, stop = self.loader()
        loader.request(URL)
        output.get(timeout=2)
        self.finished(loader)
        self.assertEqual(loader._cache_path(URL).read_bytes(), picture())
        self.assertEqual(list(self.cache.glob(".icon-*.tmp")), [])
        stop.set()
        loader.join(2)
        def offline(url):
            raise URLError("offline")
        reloaded, output, _ = self.loader(offline)
        reloaded.request(URL)
        self.assertEqual(output.get(timeout=2)[1][1].size, (24, 24))
        reloaded._download.assert_not_called()

    def test_corrupt_cache_is_replaced_with_valid_download(self):
        loader, output, _ = self.loader()
        self.cache.mkdir()
        path = loader._cache_path(URL)
        path.write_bytes(b"not an image")
        loader.request(URL)
        output.get(timeout=2)
        self.finished(loader)
        self.assertEqual(path.read_bytes(), picture())
        loader._download.assert_called_once()

    def test_bad_image_and_network_failure_are_silent_and_cooled_down(self):
        for failure in (b"not an image", URLError("offline")):
            with self.subTest(failure=type(failure).__name__):
                def download(url):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                loader, output, stop = self.loader(download)
                self.assertTrue(loader.request(URL))
                self.finished(loader)
                self.assertTrue(output.empty())
                self.assertFalse(loader._cache_path(URL).exists())
                self.assertFalse(loader.request(URL))
                loader._download.assert_called_once()
                self.assertTrue(loader.is_alive())
                stop.set()
                loader.join(2)

    def test_failed_icon_can_retry_after_cooldown(self):
        loader, output, _ = self.loader(lambda url: b"bad image")
        loader.request(URL)
        self.finished(loader)
        self.assertFalse(loader.request(URL))
        with loader._lock:
            loader._retry_after[URL] = 0
        loader._download.side_effect = None
        loader._download.return_value = picture()
        self.assertTrue(loader.request(URL))
        self.assertEqual(output.get(timeout=2)[1][0], URL)
        self.assertEqual(loader._download.call_count, 2)

    def test_cache_write_failure_still_emits_valid_icon(self):
        loader, output, _ = self.loader()
        self.cache.write_text("File occupies cache directory")
        loader.request(URL)
        self.assertEqual(output.get(timeout=2)[1][0], URL)

    def test_invalid_urls_and_stopped_loader_do_not_enqueue(self):
        loader, _, stop = self.loader(start=False)
        for url in (None, "", URL.replace("https:", "http:"), URL + "?x=1", URL + "#x",
                    URL.replace(".com/", ".com:443/"), URL.replace("data.aramkit.com", "example.com"),
                    URL.replace("/assets/item/", "/assets/../item/"), URL.replace(".webp", ".exe")):
            self.assertFalse(loader.request(url))
        stop.set()
        self.assertFalse(loader.request(URL))
        self.assertTrue(loader._requests.empty())

    def test_tft_artwork_uses_the_same_nonblocking_cache_with_exact_public_asset_paths(self):
        loader, output, _ = self.loader()
        urls = ["https://ap.tft.tools/img/gg17/face/da_18_akali_ad.jpg?w=64",
                "https://ap.tft.tools/img/items_s14/DA_InfinityEdge.png?w=64",
                "https://ap.tft.tools/img/augments/DA_AugmentedPower1.png?w=64"]
        for url in urls:
            self.assertTrue(loader.request(url))
        self.assertEqual({output.get(timeout=2)[1][0] for _ in urls}, set(urls))
        for url in urls:
            self.assertTrue(loader._cache_path(url).is_file())

    def test_tft_asset_allowlist_rejects_other_hosts_paths_and_queries(self):
        loader, _, _ = self.loader(start=False)
        url = "https://ap.tft.tools/img/items_s14/DA_InfinityEdge.png?w=64"
        for invalid in (url.replace("ap.tft.tools", "example.com"), url.replace("https:", "http:"),
                        url.replace("/img/", "/private/"), url + "&redirect=example.com",
                        url.replace("?w=64", "?w=99999"), url.replace("DA_InfinityEdge", "../secret")):
            self.assertFalse(loader.request(invalid))
        loader._download.assert_not_called()

    def test_decoder_rejects_excessive_bytes_dimensions_and_unsupported_format(self):
        for payload in (b"x" * (icons.MAX_DOWNLOAD_BYTES + 1),
                        picture((icons.MAX_IMAGE_EDGE + 1, 1)), picture(format="GIF")):
            with self.assertRaises(ValueError):
                icons._decode_image(payload)

    def test_download_is_bounded_and_redirects_are_rejected(self):
        loader = icons.IconLoader(queue.Queue(), threading.Event(), self.cache)
        opener = MagicMock()
        response = opener.open.return_value.__enter__.return_value
        response.read.return_value = picture()
        with patch.object(icons, "build_opener", return_value=opener) as build:
            self.assertEqual(loader._download(URL), picture())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, URL)
        self.assertEqual(request.get_header("User-agent"), "MayhemCompanion/0.1")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        response.read.assert_called_once_with(icons.MAX_DOWNLOAD_BYTES + 1)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, "", {}, "https://example.com/"))
        response.read.return_value = b"x" * (icons.MAX_DOWNLOAD_BYTES + 1)
        with patch.object(icons, "build_opener", return_value=opener), self.assertRaises(ValueError):
            loader._download(URL)
        with self.assertRaises(ValueError):
            loader._download("https://example.com/icon.webp")


if __name__ == "__main__":
    unittest.main()
