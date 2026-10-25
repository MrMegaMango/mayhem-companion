"""Serialized background HTTP, local API and OCR work."""
import logging
import queue
import threading
import time

from .capture import CaptureReader
from .data import StatsProvider
from .game import poll_active_player
from .matching import detect_offers


class Worker(threading.Thread):
    def __init__(self, output, requests, stop, cache):
        super().__init__(daemon=True, name="mayhem-reader")
        self.output, self.requests, self.stop = output, requests, stop
        self.provider = StatsProvider(cache)
        self.catalog = None
        self.capture = CaptureReader()
        self.enabled = True
        self.last_game = None
        self.next_poll = self.next_scan = 0.0
        self.last_offers = self.pending_offers = ()
        self.last_seen = 0.0

    def emit(self, kind, value):
        self.output.put((kind, value))

    def load(self):
        self.catalog = self.provider.load_catalog()
        self.emit("catalog", self.catalog)

    def run(self):
        try:
            self.load()
        except Exception as exc:
            self.emit("error", f"Data unavailable: {exc}. Use Retry data.")
        while not self.stop.wait(0.1):
            try:
                while True:
                    task, payload = self.requests.get_nowait()
                    if task == "enabled":
                        self.enabled = payload
                    elif task == "retry":
                        self.load()
                    elif task == "scan":
                        self.next_scan = 0
                        self.last_offers = self.pending_offers = ()
                        self.emit("offers", [])
                    elif task == "lookup":
                        token, champion, ids, stage = payload
                        try:
                            rates = [self.provider.rate_for(champion, ident, stage)
                                     if ident else None for ident in ids]
                            self.emit("rates", (token, rates))
                        except Exception as exc:
                            self.emit("lookup_error", (token, str(exc)))
                    elif task == "spells":
                        token, champion = payload
                        try:
                            self.emit("spells", (token, self.provider.summoner_spells_for(champion)))
                        except Exception as exc:
                            self.emit("spells_error", (token, str(exc)))
            except queue.Empty:
                pass
            except Exception as exc:
                logging.exception("Companion task failed")
                self.emit("error", str(exc))
            now = time.monotonic()
            if now >= self.next_poll:
                self.next_poll = now + 2
                try:
                    game = poll_active_player()
                    self.emit("game", game)
                    self.last_game = game
                except Exception:
                    logging.exception("Local game polling failed")
                    self.last_game = {"status": "waiting"}
                    self.emit("game", self.last_game)
            if not (self.enabled and self.catalog and self.last_game
                    and self.last_game.get("status") == "live"):
                if self.last_offers:
                    self.last_offers = self.pending_offers = ()
                    self.emit("offers", [])
                continue
            if now < self.next_scan:
                continue
            self.next_scan = now + 1.3
            try:
                result = self.capture.scan()
                if result.get("status") != "ok":
                    self.pending_offers = ()
                    if result.get("status") == "waiting":
                        message = "Scan paused · focus League to resume."
                        if self.last_offers:
                            message += " Showing last recognized choices."
                        self.emit("scan_status", message)
                        continue
                    message = result.get("message", "Game capture unavailable.")
                    if self.last_offers:
                        message += " Showing last recognized choices."
                    self.emit("scan_status", message)
                    if result.get("status") == "error":
                        self.next_scan = now + 10
                    continue
                offers = detect_offers(result["lines"], self.catalog["augments"])
                self.emit("scan_status", f"{len(offers)} titles recognized · left to right" if offers
                          else "No augment choices recognized. Keep the three cards visible.")
                signature = tuple(str(x["id"]) for x in offers)
                if signature:
                    self.last_seen = now
                    # Wait for two readings: animations/rerolls can show partial titles.
                    if signature == self.pending_offers and signature != self.last_offers:
                        self.last_offers = signature
                        self.emit("offers", offers)
                    self.pending_offers = signature
                else:
                    self.pending_offers = ()
                    if self.last_offers and now - self.last_seen > 4:
                        self.last_offers = ()
                        self.emit("offers", [])
            except Exception as exc:
                logging.exception("OCR failed")
                self.emit("scan_status", f"OCR unavailable: {exc}. Manual search still works.")
                self.next_scan = now + 10
