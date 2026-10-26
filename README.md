# Mayhem Companion

A personal Windows prototype that reads your current League champion and the visible ARAM Mayhem augment titles, then shows that champion's augment, core-item-build and summoner-spell-pair win rates from ARAMKit.

## Start

Run `Setup.ps1` with PowerShell on Windows (Python 3.12+ required). If Windows treats a WSL/network path as an unsigned remote script, copy the project to a local Windows folder before running setup. Open **Mayhem Companion** from the desktop shortcut. Installed files live in `%USERPROFILE%\MayhemCompanion`; the development source can remain in WSL.

The companion starts hidden and waits in the background. It appears automatically when you enter **ARAM or ARAM Mayhem champion select**, stays available through loading and the match, and hides when the match ends or you leave champion select. Other queues and the ordinary lobby do not open it. Custom games are recognized by their actual ARAM/Mayhem mode. **Ctrl+Alt+M** opens or hides it manually; hiding it during a match lasts until that match ends. Start with `--browse` to keep manual browsing available outside matches. Closing with × exits the background companion.

The **M icon in the Windows notification area** remains available while the panel is hidden. Click it to show the panel, or right-click for **Show companion**, **Hide companion**, and **Exit**. Showing from the tray also unlocks click-through controls. Windows may place the icon in its hidden-icons menu. Exit removes the icon and stops the background companion.

Turn on **Start with League** in the tray menu to launch automatically when the League client opens. This installs one shortcut in your own Windows Startup folder: a small watcher starts at sign-in, checks League process names every two seconds, and launches the companion once per League session. Exit stays effective for that session; closing and reopening League starts it again. The watcher also counts an active game so a client closing during a match does not trigger another launch. Turn the tray setting off to remove the startup shortcut and stop the watcher. Setup also accepts `-StartWithLeague`. Outside the app, run `cscript //NoLogo Configure-Autostart.vbs "%USERPROFILE%\MayhemCompanion" disable` from the installed folder to disable it.

- Use **borderless or windowed** League. Exclusive fullscreen is not supported.
- Keep **Auto scan** checked. **Auto** champion selection follows your champion during champion select and in game, including champion swaps. Manually choosing a champion also works with automatic augment scanning.
- Keep League focused with the three cards visible while scanning. Switching away pauses the reader and retains the last recognized choices with a paused label. Returning to League resumes scanning; choices clear after the selection disappears or the game ends.
- Scanning reads the card-title band first at up to 4K resolution; the wider game area is a fallback for other layouts. An unreadable capture retains the last results with an explicit retry message.
- Move the panel by dragging **MAYHEM**. Keep it away from the three augment cards.
- Placement uses the chosen monitor's work area, including displays left of or above the main screen. The saved display preference is preserved across resizing and restarts, with a visible fallback if that monitor is disconnected. The visible panel checks its actual position after opening and periodically afterward, returning to the preferred monitor if Windows relocates it or the monitor reconnects. Temporary monitor-query failures retain the last known display; dragging within the preferred screen remains available.
- **Ctrl+Alt+M** shows/hides the panel. **Ctrl+Alt+L** toggles click-through. **Ctrl+Alt+S** resumes/rescans.
- If OCR misses a choice, type its name in a dropdown and press Enter. Choose a champion manually to browse outside a match. **Auto** returns to automatic detection.
- **Spells** opens automatically during champion select so you can compare spell-pair win rates before the match. It follows your champion and highlights your current pair as you change spells. A hovered champion is labeled as a preview. The statistics describe pairs (for example, Flash + Mark), not individual spells. The companion does not change your spells.
- **Builds** opens when the game starts and between augment selections. It shows up to five core item sets for the selected build type, with win rates, game counts and pick rates within that type. The default sort is **Games**; choose **Win rate** or **Pick rate** to compare other sets. Sets use ARAMKit's filtered build data and do not imply a purchase order. **Augments** opens when choices are recognized and **Builds** returns after repeated successful scans confirm they disappeared. Rerolls, focus loss and capture errors do not bounce between tabs. You can switch tabs manually; that choice stays until the next selection or game phase. Manual champion browsing keeps its selected tab.
- **All picks** shows the champion's overall augment statistics. Pick 1–4 filters are manual because level alone cannot reliably identify pending selections or extra-augment mechanics. Additional picks use All picks.
- English augment titles are supported. Native Windows English OCR must be installed. OCR never chooses an augment or presses an in-game button.

## Data

Data source: [ARAMKit](https://aramkit.com/en-US), using its public version manifest, champion/augment catalogs and the currently selected champion's stats. The app downloads catalog data, requested champions and icons for displayed items, caches immutable snapshots, and checks the manifest at most daily. No key or account is needed.

Build cards show each item's icon beside its name. Icons come from the matching ARAMKit item catalog, download on a separate background thread, and are cached locally. Missing or unavailable icons leave the item names and statistics usable and do not delay augment scanning.

Rates are **champion + augment**, **champion + core item set**, or **champion + spell pair**, never global rates substituted for missing champion data. Missing data is blank; unavailable builds and pairs are not invented. Samples below 200 games are marked (an asterisk in the Spells table). Cached fallback data retains its original patch/date and is labeled. The rates reflect ARAMKit's sample, not all regions; collection/provenance claims belong to that provider. Source attribution is clickable in the panel. Respect [ARAMKit's terms](https://aramkit.com/en-US/terms); this is for personal, noncommercial use, not bulk redistribution of its data.

Riot's [developer policy](https://developer.riotgames.com/docs/lol#game-policy) lists augment win-rate displays in websites, applications and overlays as an unapproved use case. The wording refers to augments broadly without specifically naming Mayhem. This personal prototype is not Riot-approved; applying that restriction to Mayhem is a reasonable reading of the text, not a separate Mayhem-specific ruling verified by this project. No claim is made about enforcement, account bans, or guaranteed account safety.

## Local operation

Champion select is read from the local League Client API, using the current client's lockfile for its temporary local authentication token and port. The reader only issues GET requests for gameflow phase, gameflow session (queue/mode), and champion-select session, matches your local player slot, and polls every 0.75 seconds. The token stays in memory and is never logged, cached, or uploaded. Riot does not guarantee compatibility of League Client API endpoints; manual champion browsing remains available if the client changes.

In-game champion detection uses Riot's documented read-only Live Client Data API on Windows `127.0.0.1:2999`. TLS relaxation is scoped to these loopback services, and redirects and proxies are refused. Public stats use normal HTTPS verification. The app does not read game memory, inject code, modify item sets, install drivers, automate gameplay, or request your account password.

Screen capture runs only when the League game executable is the visible foreground window. Screenshots remain in memory and OCR runs locally. No screenshots, player names, Riot IDs or game histories are uploaded. HTTP requests to ARAMKit identify the public champion ID being looked up, as a normal page lookup would. Cache, window position and error logs are under `%USERPROFILE%\MayhemCompanion`.

The app registers modified hotkeys without keyboard hooks. Automatic launch is a per-user Startup shortcut and needs no administrator access. Close with ×. To uninstall, disable **Start with League**, close the companion, and delete its installed folder and desktop shortcut.

## Development and verification

From the repository, using Windows Python:

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m mayhem --browse
```

Python under WSL cannot use the Windows OCR API or reach League's Windows loopback API by default. Use Windows Python for the app; pure unit tests can run in either environment.

Checks cover source validation/caching, champion matching, wrong-player rejection, spell-pair validation/current-pair matching, stale-response handling, OCR title ambiguity and layout, focus-pause retention, and failure states. Native English OCR was tested using synthetic title images and a live custom Mayhem game at 3840×2160: all three choices populated their champion-specific rates, including a hovered tooltip repeating a card title. Keeping those results while switching focus was also verified. Spell detection was verified against a live game's local API. OCR may still miss stylized/wrapped titles or unusual display layouts; use the manual dropdown fallback.

Champion-select fields were checked against the running client's own API schema. Automated checks cover local-player matching, champion previews/swaps, spell changes, stale result rejection, and phase transitions. A real Tk panel was exercised with simulated champion-select states and real cached spell statistics; the local client connection was also verified. A live champion-select screen has not yet been used to verify this version end to end.

Build statistics were checked against live ARAMKit champion data. The native Tk panel was exercised with real core sets and simulated champion-select, live-play, augment-open and augment-dismissed events. Tests cover stale build responses, missing data, coherent snapshot fallback, manual tab ownership and reroll/rescan stability. The new automatic Builds handoff has not yet been verified during a live match.

Mayhem Companion is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
