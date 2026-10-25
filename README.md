# Mayhem Companion

A personal Windows prototype that reads your current League champion and the visible ARAM Mayhem augment titles, then shows that champion's augment and summoner-spell-pair win rates from ARAMKit.

## Start

Run `Setup.ps1` with PowerShell on Windows (Python 3.12+ required). If Windows treats a WSL/network path as an unsigned remote script, copy the project to a local Windows folder before running setup. Open **Mayhem Companion** from the desktop shortcut. Installed files live in `%USERPROFILE%\MayhemCompanion`; the development source can remain in WSL.

The companion starts hidden and waits in the background. It appears automatically when you enter **ARAM or ARAM Mayhem champion select**, stays available through loading and the match, and hides when the match ends or you leave champion select. Other queues and the ordinary lobby do not open it. Custom games are recognized by their actual ARAM/Mayhem mode. **Ctrl+Alt+M** opens or hides it manually; hiding it during a match lasts until that match ends. Start with `--browse` to keep manual browsing available outside matches. Closing with × exits the background companion.

The **M icon in the Windows notification area** remains available while the panel is hidden. Click it to show the panel, or right-click for **Show companion**, **Hide companion**, and **Exit**. Showing from the tray also unlocks click-through controls. Windows may place the icon in its hidden-icons menu. Exit removes the icon and stops the background companion.

- Use **borderless or windowed** League. Exclusive fullscreen is not supported.
- Keep **Auto scan** checked. **Auto** champion selection follows your champion during champion select and in game, including champion swaps. Manually choosing a champion also works with automatic augment scanning.
- Keep League focused with the three cards visible while scanning. Switching away pauses the reader and retains the last recognized choices with a paused label. Returning to League resumes scanning; choices clear after the selection disappears or the game ends.
- Scanning reads the card-title band first at up to 4K resolution; the wider game area is a fallback for other layouts. An unreadable capture retains the last results with an explicit retry message.
- Move the panel by dragging **MAYHEM**. Keep it away from the three augment cards.
- Placement uses the chosen monitor's work area, including displays left of or above the main screen. The saved display preference is preserved across resizing and restarts, with a visible fallback if that monitor is disconnected.
- **Ctrl+Alt+M** shows/hides the panel. **Ctrl+Alt+L** toggles click-through. **Ctrl+Alt+S** resumes/rescans.
- If OCR misses a choice, type its name in a dropdown and press Enter. Choose a champion manually to browse outside a match. **Auto** returns to automatic detection.
- **Spells** opens automatically during champion select so you can compare spell-pair win rates before the match. It follows your champion and highlights your current pair as you change spells. A hovered champion is labeled as a preview. **Augments** opens when the game starts; you can switch tabs yourself at any time. The statistics describe pairs (for example, Flash + Mark), not individual spells. The companion does not change your spells.
- **All picks** shows the champion's overall augment statistics. Pick 1–4 filters are manual because level alone cannot reliably identify pending selections or extra-augment mechanics. Additional picks use All picks.
- English augment titles are supported. Native Windows English OCR must be installed. OCR never chooses an augment or presses an in-game button.

## Data

Data source: [ARAMKit](https://aramkit.com/en-US), using its public version manifest, champion/augment catalogs and the currently selected champion's stats. The app downloads only catalog data and requested champions, caches immutable snapshots, and checks the manifest at most daily. No key or account is needed.

Rates are **champion + augment** or **champion + spell pair**, never global rates substituted for missing champion data. Missing data is blank; unavailable pairs are not invented. Samples below 200 games are marked (an asterisk in the Spells table). Cached fallback data retains its original patch/date and is labeled. The rates reflect ARAMKit's sample, not all regions; collection/provenance claims belong to that provider. Source attribution is clickable in the panel. Respect [ARAMKit's terms](https://aramkit.com/en-US/terms); this is for personal, noncommercial use, not bulk redistribution of its data.

Riot's [developer policy](https://developer.riotgames.com/docs/lol#game-policy) lists augment win-rate displays in websites, applications and overlays as an unapproved use case. The wording refers to augments broadly without specifically naming Mayhem. This personal prototype is not Riot-approved; applying that restriction to Mayhem is a reasonable reading of the text, not a separate Mayhem-specific ruling verified by this project. No claim is made about enforcement, account bans, or guaranteed account safety.

## Local operation

Champion select is read from the local League Client API, using the current client's lockfile for its temporary local authentication token and port. The reader only issues GET requests for gameflow phase, gameflow session (queue/mode), and champion-select session, matches your local player slot, and polls every 0.75 seconds. The token stays in memory and is never logged, cached, or uploaded. Riot does not guarantee compatibility of League Client API endpoints; manual champion browsing remains available if the client changes.

In-game champion detection uses Riot's documented read-only Live Client Data API on Windows `127.0.0.1:2999`. TLS relaxation is scoped to these loopback services, and redirects and proxies are refused. Public stats use normal HTTPS verification. The app does not read game memory, inject code, modify item sets, install drivers, automate gameplay, or request your account password.

Screen capture runs only when the League game executable is the visible foreground window. Screenshots remain in memory and OCR runs locally. No screenshots, player names, Riot IDs or game histories are uploaded. HTTP requests to ARAMKit identify the public champion ID being looked up, as a normal page lookup would. Cache, window position and error logs are under `%USERPROFILE%\MayhemCompanion`.

The app registers modified hotkeys without keyboard hooks. Nothing is added to Windows startup. Close with ×. To uninstall, close it and delete its installed folder and desktop shortcut.

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

Mayhem Companion is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
