"""Local polling and OCR stay independent of public statistics downloads."""
import logging
import queue
import threading
import time

from .capture import CaptureReader
from .client import ClientReader, is_aram_mode, is_tft_mode, is_arena_mode
from .data import RETRY_DELAY, StatsProvider
from .game import poll_active_player
from .matching import detect_offers
from .tft_data import TftProvider
from .arena_data import ArenaProvider


class Worker(threading.Thread):
    ACTIVE_SCAN_SECONDS = 0.35
    IDLE_SCAN_SECONDS = 0.8
    OFFER_GONE_SECONDS = 1.2
    OFFER_GONE_MIN_FRAMES = 2
    SELECT_POLL_SECONDS = 0.25
    DEFAULT_POLL_SECONDS = 0.75

    def __init__(self, output, requests, stop, cache):
        super().__init__(daemon=True, name="mayhem-reader")
        self.output, self.requests, self.stop = output, requests, stop
        self.provider = StatsProvider(cache)
        self.data_pending = {}
        self.data_lock = threading.Lock()
        self.data_ready = threading.Event()
        self.data_thread = None
        self.catalog_results = queue.Queue()
        self.tft_provider = None
        self.cache = cache
        self.tft_requests = queue.Queue()
        self.tft_thread = None
        self.arena_provider = None
        self.arena_pending = {}
        self.arena_lock = threading.Lock()
        self.arena_ready = threading.Event()
        self.arena_thread = None
        self.arena_catalog_results = queue.Queue()
        self.arena_catalog = None
        self.arena_catalog_token = None
        self.scan_mode = "aram"
        self.arena_scan_champion = None
        self.champion_provider = None
        self.champion_retry_at = 0.0
        self.champion_requests = queue.Queue(maxsize=1)
        self.champion_thread = None
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
        self.catalog_results.put(self.provider.load_catalog())

    def publish_catalogs(self):
        # The UI must receive a catalog before offers recognized against it.
        try:
            while True:
                self.catalog = self.catalog_results.get_nowait()
                self.emit("catalog", self.catalog)
        except queue.Empty:
            pass
        try:
            while True:
                token, self.arena_catalog = self.arena_catalog_results.get_nowait()
                self.arena_catalog_token = token
                self.emit("arena", (token, self.arena_catalog))
                if self.scan_mode == "arena":
                    self.force_offer_emit = True
                    self.pending_offers = ()
                    self.empty_since, self.empty_frames = None, 0
        except queue.Empty:
            pass

    def perform_arena_task(self, task, payload):
        try:
            if self.arena_provider is None:
                self.arena_provider = ArenaProvider(self.cache)
            if task == "arena":
                self.arena_catalog_results.put((payload[0], self.arena_provider.load(force=payload[1])))
            else:
                token, champion, force = payload
                self.emit("arena_champion", (token, champion, self.arena_provider.champion_for(champion, force=force)))
        except Exception as exc:
            if task == "arena":
                self.emit("arena_error", (payload[0], str(exc)))
            else:
                self.emit("arena_champion_error", (payload[0], payload[1], str(exc)))

    def run_arena_data(self):
        while not self.stop.is_set():
            if not self.arena_ready.wait(0.2) or self.stop.is_set():
                continue
            with self.arena_lock:
                task = next(iter(self.arena_pending))
                payload = self.arena_pending.pop(task)
                if not self.arena_pending:
                    self.arena_ready.clear()
            self.perform_arena_task(task, payload)

    def queue_arena_task(self, task, payload):
        with self.arena_lock:
            self.arena_pending[task] = payload
            self.arena_ready.set()
        if self.arena_thread is None:
            self.arena_thread = threading.Thread(target=self.run_arena_data, name="mayhem-arena-data", daemon=True)
            self.arena_thread.start()

    def perform_aram_task(self, task, payload):
        try:
            if task == "catalog":
                self.load()
            elif task == "lookup":
                token, champion, ids, stage = payload
                rates = [self.provider.rate_for(champion, ident, stage) if ident else None for ident in ids]
                self.emit("rates", (token, rates))
            elif task == "spells":
                token, champion = payload
                self.emit("spells", (token, self.provider.summoner_spells_for(champion)))
            elif task == "builds":
                token, champion = payload
                self.emit("builds", (token, self.provider.item_builds_for(champion)))
        except Exception as exc:
            if task == "catalog":
                self.emit("error", f"Data unavailable: {exc}. Use Retry data.")
            else:
                self.emit(task + "_error", (payload[0], str(exc)))

    def run_aram_data(self):
        # One provider owns its mutable cache. Keep only the latest waiting
        # request of each type so rapid champion changes cannot build a backlog.
        while not self.stop.is_set():
            if not self.data_ready.wait(0.2) or self.stop.is_set():
                continue
            with self.data_lock:
                task = next(iter(self.data_pending))
                payload = self.data_pending.pop(task)
                if not self.data_pending:
                    self.data_ready.clear()
            self.perform_aram_task(task, payload)

    def queue_aram_task(self, task, payload):
        with self.data_lock:
            self.data_pending[task] = payload
            self.data_ready.set()
        if self.data_thread is None:
            self.data_thread = threading.Thread(target=self.run_aram_data,
                                                name="mayhem-aram-data", daemon=True)
            self.data_thread.start()

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

    def perform_champion_task(self, payload):
        token, champion_ids = payload
        if token is None and time.monotonic() < self.champion_retry_at:
            return
        try:
            # A separate provider owns its mutable in-memory cache. Disk cache
            # writes are atomic and can be shared with the ARAM lookup worker.
            if self.champion_provider is None:
                self.champion_provider = StatsProvider(self.cache)
            data = self.champion_provider.champion_rates_for(champion_ids)
            if token is not None:
                self.champion_retry_at = 0.0
                self.emit("champions", (token, data))
        except Exception as exc:
            if token is not None:
                self.champion_retry_at = time.monotonic() + RETRY_DELAY
                self.emit("champions_error", (token, str(exc)))
            else:
                # A failed speculative warmup must not impose the provider's
                # retry cooldown on the first real selection request.
                self.champion_provider = None

    def run_champion_data(self):
        while not self.stop.is_set():
            try:
                payload = self.champion_requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if not self.stop.is_set():
                    self.perform_champion_task(payload)
            finally:
                self.champion_requests.task_done()

    def queue_champion_task(self, payload):
        # Bench updates can arrive during a slow fetch. Only the newest waiting
        # choice list matters; the UI also rejects replies to obsolete tokens.
        try:
            self.champion_requests.get_nowait()
        except queue.Empty:
            pass
        else:
            self.champion_requests.task_done()
        self.champion_requests.put_nowait(payload)
        self.start_champion_data()

    def prewarm_champions(self):
        try:
            # Load the aggregate snapshot without per-champion downloads.
            # Never displace a pending request for choices already on screen.
            self.champion_requests.put_nowait((None, []))
        except queue.Full:
            return
        self.start_champion_data()

    def start_champion_data(self):
        if self.champion_thread is None:
            self.champion_thread = threading.Thread(target=self.run_champion_data,
                                                    name="mayhem-champion-data", daemon=True)
            self.champion_thread.start()

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
        live_mode = game.get("game_mode")
        if ((selection.get("is_arena") is True and live_mode and not is_arena_mode(live_mode))
                or (is_arena_mode(live_mode) and selection.get("is_arena") is False)):
            # A previous match's live API can linger during loading.
            return selection
        if selection.get("is_arena") is not None or is_arena_mode(live_mode):
            game["is_arena"] = (selection["is_arena"] if selection.get("is_arena") is not None
                                else is_arena_mode(live_mode))
        if game.get("is_arena") is True and "queue_id" in selection:
            game["queue_id"] = selection["queue_id"]
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
        self.queue_aram_task("catalog", None)
        self.prewarm_champions()
        while not self.stop.wait(0.05):
            try:
                while True:
                    task, payload = self.requests.get_nowait()
                    if task == "enabled":
                        self.enabled = payload
                    elif task == "retry":
                        self.queue_aram_task("catalog", None)
                    elif task == "scan":
                        self.next_scan = 0
                        self.pending_offers = ()
                        self.empty_since, self.empty_frames = None, 0
                        # Refresh the UI after confirmation, even if these are
                        # the same IDs it had before manual browsing/rescan.
                        self.force_offer_emit = True
                    elif task in ("lookup", "spells", "builds"):
                        self.queue_aram_task(task, payload)
                    elif task in ("tft", "tft_items"):
                        self.queue_tft_task(task, payload)
                    elif task in ("arena", "arena_champion"):
                        self.queue_arena_task(task, payload)
                    elif task == "champions":
                        self.queue_champion_task(payload)
            except queue.Empty:
                pass
            except Exception as exc:
                logging.exception("Companion task failed")
                self.emit("error", str(exc))
            self.publish_catalogs()
            now = time.monotonic()
            if now >= self.next_poll:
                try:
                    game = self.poll_game()
                    self.emit("game", game)
                    previous_phase = self.last_game.get("phase") if self.last_game else None
                    if (game.get("phase") in ("Matchmaking", "ReadyCheck")
                            and game.get("phase") != previous_phase):
                        self.prewarm_champions()
                    self.last_game = game
                except Exception:
                    logging.exception("Local game polling failed")
                    self.last_game = {"status": "waiting"}
                    self.emit("game", self.last_game)
                selecting = (self.last_game.get("status") == "champ_select"
                             or self.last_game.get("phase") in ("Matchmaking", "ReadyCheck", "ChampSelect"))
                self.next_poll = time.monotonic() + (self.SELECT_POLL_SECONDS if selecting
                                                      else self.DEFAULT_POLL_SECONDS)
            scan_mode = "arena" if self.last_game and self.last_game.get("is_arena") is True else "aram"
            scan_champion = (self.last_game.get("champion_id") or self.last_game.get("raw_champion")
                             or self.last_game.get("champion_name")) if self.last_game else None
            if (scan_mode != self.scan_mode or (scan_mode == "arena" and
                    scan_champion != self.arena_scan_champion)):
                self.emit("arena_offers" if self.scan_mode == "arena" else "offers",
                          (self.arena_catalog_token, []) if self.scan_mode == "arena" else [])
                self.last_offers = self.pending_offers = ()
                self.empty_since, self.empty_frames = None, 0
                self.next_scan = 0
                self.scan_mode = scan_mode
            self.arena_scan_champion = scan_champion if scan_mode == "arena" else None
            scan_catalog = self.arena_catalog if scan_mode == "arena" else self.catalog
            def emit_scan(kind, payload):
                if scan_mode == "arena":
                    self.emit("arena_" + kind, (self.arena_catalog_token, payload)
                              if kind == "offers" else payload)
                else:
                    self.emit(kind, payload)
            if not (self.enabled and scan_catalog and self.last_game
                    and self.last_game.get("status") == "live"
                    and (self.last_game.get("is_aram") is True or self.last_game.get("is_arena") is True)):
                self.empty_since, self.empty_frames = None, 0
                if self.last_offers:
                    self.last_offers = self.pending_offers = ()
                    emit_scan("offers", [])
                continue
            if now < self.next_scan:
                continue
            scan_delay = self.ACTIVE_SCAN_SECONDS
            try:
                result = self.capture.scan(mode="arena") if scan_mode == "arena" else self.capture.scan()
                if result.get("status") != "ok":
                    self.pending_offers = ()
                    self.empty_since, self.empty_frames = None, 0
                    scan_delay = self.IDLE_SCAN_SECONDS
                    if result.get("status") == "waiting":
                        message = "Scan paused · focus League to resume."
                        if self.last_offers:
                            message += " Showing last recognized choices."
                        emit_scan("scan_status", message)
                        continue
                    message = result.get("message", "Game capture unavailable.")
                    if self.last_offers:
                        message += " Showing last recognized choices."
                    emit_scan("scan_status", message)
                    if result.get("status") == "error":
                        scan_delay = 10
                    continue
                offers = detect_offers(result["lines"], scan_catalog["augments"])
                emit_scan("scan_status", f"{len(offers)} titles recognized · left to right" if offers
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
                        emit_scan("offers", offers)
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
                        emit_scan("offers", [])
                    scan_delay = (self.ACTIVE_SCAN_SECONDS if self.last_offers or self.force_offer_emit
                                  else self.IDLE_SCAN_SECONDS)
            except Exception as exc:
                self.pending_offers = ()
                self.empty_since, self.empty_frames = None, 0
                logging.exception("OCR failed")
                emit_scan("scan_status", f"OCR unavailable: {exc}. Manual search still works.")
                scan_delay = 10
            finally:
                # Capture/OCR can take longer than the interval. Always leave
                # a full cooldown after it finishes instead of immediately
                # starting another expensive pass with an overdue deadline.
                self.next_scan = time.monotonic() + scan_delay
