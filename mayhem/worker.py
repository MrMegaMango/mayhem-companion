"""Serialized background HTTP, local API and OCR work."""
import logging
import queue
import threading
import time

from .capture import CaptureReader
from .client import ClientReader, is_aram_mode, is_tft_mode
from .data import StatsProvider
from .game import poll_active_player
from .matching import detect_offers
from .tft_data import TftProvider


class Worker(threading.Thread):
    ACTIVE_SCAN_SECONDS = 0.35
    IDLE_SCAN_SECONDS = 0.8
    OFFER_GONE_SECONDS = 1.2
    OFFER_GONE_MIN_FRAMES = 2

    def __init__(self, output, requests, stop, cache):
        super().__init__(daemon=True, name="mayhem-reader")
        self.output, self.requests, self.stop = output, requests, stop
        self.provider = StatsProvider(cache)
        self.tft_provider = None
        self.cache = cache
        self.tft_requests = queue.Queue()
        self.tft_thread = None
        self.catalog = None
        self.capture = CaptureReader()
        self.client = ClientReader()
        self.enabled = True
        self.last_game = None
        self.next_poll = self.next_scan = 0.0
        self.last_offers = self.pending_offers = ()
        self.last_seen = 0.0
        self.empty_since = None
        self.empty_frames = 0
        self.force_offer_emit = False

    def emit(self, kind, value):
        self.output.put((kind, value))

    def load(self):
        self.catalog = self.provider.load_catalog()
        self.emit("catalog", self.catalog)

    def perform_tft_task(self, task, payload):
        token = payload[0]
        try:
            if self.tft_provider is None:
                self.tft_provider = TftProvider(self.cache)
            if task == "tft":
                self.emit("tft", (token, self.tft_provider.load(force=payload[1])))
            else:
                unit = payload[1]
                force = payload[2] if len(payload) > 2 else False
                self.emit("tft_items", (token, unit, self.tft_provider.items_for(unit, force=force)))
        except Exception as exc:
            if task == "tft":
                self.emit("tft_error", (token, str(exc)))
            else:
                self.emit("tft_items_error", (token, payload[1], str(exc)))

    def run_tft_data(self):
        # TFT catalogs/comps can be larger than an ARAM lookup. Keep those
        # requests serialized on their own thread so local polling/OCR continue.
        while not self.stop.is_set():
            try:
                task, payload = self.tft_requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if not self.stop.is_set():
                    self.perform_tft_task(task, payload)
            finally:
                self.tft_requests.task_done()

    def queue_tft_task(self, task, payload):
        self.tft_requests.put((task, payload))
        if self.tft_thread is None:
            self.tft_thread = threading.Thread(target=self.run_tft_data, name="mayhem-tft-data", daemon=True)
            self.tft_thread.start()

    def poll_game(self):
        # The lobby exposes the champion while spells can still be changed.
        # Keep the in-game API as the source once champion select has ended.
        selection = self.client.poll()
        if selection.get("is_tft") is True or selection.get("status") == "tft":
            return selection
        if selection.get("status") == "champ_select":
            return selection
        phase = selection.get("phase")
        if phase is not None and phase not in ("GameStart", "InProgress", "Reconnect"):
            # The game API can linger after a match. A known lobby/end phase
            # takes precedence so it cannot reopen the panel on the desktop.
            return selection
        game = poll_active_player(timeout=0.5)
        if game.get("status") != "live" and phase is not None:
            # Keep ARAM context while the game loads or reconnects.
            return selection
        game["phase"] = phase
        if is_tft_mode(game.get("game_mode")):
            if selection.get("is_tft") is False:
                # A previous game's local API can linger during a new game's
                # load. Do not combine its TFT player payload with ARAM context.
                return selection
            # This is only a mode fallback; TFT boards/items are not League
            # champion context and must never enter the ARAM lookup path.
            game.update(status="tft", is_tft=True, is_aram=False,
                        champion_id=None, champion_name=None, raw_champion=None,
                        summoner_spells=[], item_ids=None)
            return game
        known_mode = selection.get("is_aram")
        game["is_aram"] = (known_mode if known_mode is not None else
                           is_aram_mode(game.get("game_mode")) if game.get("game_mode") else None)
        return game

    def run(self):
        try:
            self.load()
        except Exception as exc:
            self.emit("error", f"Data unavailable: {exc}. Use Retry data.")
        while not self.stop.wait(0.05):
            try:
                while True:
                    task, payload = self.requests.get_nowait()
                    if task == "enabled":
                        self.enabled = payload
                    elif task == "retry":
                        self.load()
                    elif task == "scan":
                        self.next_scan = 0
                        self.pending_offers = ()
                        self.empty_since, self.empty_frames = None, 0
                        # Refresh the UI after confirmation, even if these are
                        # the same IDs it had before manual browsing/rescan.
                        self.force_offer_emit = True
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
                    elif task == "builds":
                        token, champion = payload
                        try:
                            self.emit("builds", (token, self.provider.item_builds_for(champion)))
                        except Exception as exc:
                            self.emit("builds_error", (token, str(exc)))
                    elif task in ("tft", "tft_items"):
                        self.queue_tft_task(task, payload)
            except queue.Empty:
                pass
            except Exception as exc:
                logging.exception("Companion task failed")
                self.emit("error", str(exc))
            now = time.monotonic()
            if now >= self.next_poll:
                self.next_poll = now + 0.75
                try:
                    game = self.poll_game()
                    self.emit("game", game)
                    self.last_game = game
                except Exception:
                    logging.exception("Local game polling failed")
                    self.last_game = {"status": "waiting"}
                    self.emit("game", self.last_game)
            if not (self.enabled and self.catalog and self.last_game
                    and self.last_game.get("status") == "live"
                    and self.last_game.get("is_aram") is True):
                self.empty_since, self.empty_frames = None, 0
                if self.last_offers:
                    self.last_offers = self.pending_offers = ()
                    self.emit("offers", [])
                continue
            if now < self.next_scan:
                continue
            self.next_scan = now + self.ACTIVE_SCAN_SECONDS
            try:
                result = self.capture.scan()
                if result.get("status") != "ok":
                    self.pending_offers = ()
                    self.empty_since, self.empty_frames = None, 0
                    self.next_scan = now + self.IDLE_SCAN_SECONDS
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
                    self.empty_since, self.empty_frames = None, 0
                    # Wait for two readings: animations/rerolls can show partial titles.
                    if (signature == self.pending_offers
                            and (signature != self.last_offers or self.force_offer_emit)):
                        self.last_offers = signature
                        self.force_offer_emit = False
                        self.emit("offers", offers)
                    self.pending_offers = signature
                else:
                    self.pending_offers = ()
                    # Only a continuous run of successful empty scans proves
                    # the cards disappeared. Pauses/failures cannot age out a
                    # choice, and one empty reroll frame cannot clear it.
                    if self.empty_since is None:
                        self.empty_since = now
                    self.empty_frames += 1
                    if ((self.last_offers or self.force_offer_emit)
                            and self.empty_frames >= self.OFFER_GONE_MIN_FRAMES
                            and now - self.empty_since >= self.OFFER_GONE_SECONDS):
                        self.last_offers = ()
                        self.force_offer_emit = False
                        self.emit("offers", [])
                    self.next_scan = now + (self.ACTIVE_SCAN_SECONDS if self.last_offers or self.force_offer_emit
                                            else self.IDLE_SCAN_SECONDS)
            except Exception as exc:
                self.pending_offers = ()
                self.empty_since, self.empty_frames = None, 0
                logging.exception("OCR failed")
                self.emit("scan_status", f"OCR unavailable: {exc}. Manual search still works.")
                self.next_scan = now + 10
