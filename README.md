# Mayhem Companion

A personal Windows prototype that reads your current League champion and the visible ARAM Mayhem augment titles, then shows that champion's augment and summoner-spell-pair win rates from ARAMKit.

## Start

Run `Setup.ps1` with PowerShell on Windows (Python 3.12+ required). If Windows treats a WSL/network path as an unsigned remote script, copy the project to a local Windows folder before running setup. Open **Mayhem Companion** from the desktop shortcut. Installed files live in `%USERPROFILE%\MayhemCompanion`; the development source can remain in WSL.

- Use **borderless or windowed** League. Exclusive fullscreen is not supported.
- Keep **Auto scan** checked and **Auto** champion selection active. The companion waits until you enter a match; the lobby alone does not expose a live champion.
- Move the panel by dragging **MAYHEM**. Keep it away from the three augment cards.
- **Ctrl+Alt+M** shows/hides the panel. **Ctrl+Alt+L** toggles click-through. **Ctrl+Alt+S** resumes/rescans.
- If OCR misses a choice, type its name in a dropdown and press Enter. Choose a champion manually to browse outside a match. **Auto** returns to live detection.
- Open **Spells** to compare that champion's summoner-spell pairs by win rate, game count, or pick rate. Your current pair is highlighted when League exposes it. The statistics describe pairs (for example, Flash + Mark), not individual spells. Before a match, select a champion manually to compare pairs; the companion does not change your spells.
- **All picks** shows the champion's overall augment statistics. Pick 1–4 filters are manual because level alone cannot reliably identify pending selections or extra-augment mechanics. Additional picks use All picks.
- English augment titles are supported. Native Windows English OCR must be installed. OCR never chooses an augment or presses an in-game button.

## Data

Data source: [ARAMKit](https://aramkit.com/en-US), using its public version manifest, champion/augment catalogs and the currently selected champion's stats. The app downloads only catalog data and requested champions, caches immutable snapshots, and checks the manifest at most daily. No key or account is needed.

Rates are **champion + augment** or **champion + spell pair**, never global rates substituted for missing champion data. Missing data is blank; unavailable pairs are not invented. Samples below 200 games are marked (an asterisk in the Spells table). Cached fallback data retains its original patch/date and is labeled. The rates reflect ARAMKit's sample, not all regions; collection/provenance claims belong to that provider. Source attribution is clickable in the panel. Respect [ARAMKit's terms](https://aramkit.com/en-US/terms); this is for personal, noncommercial use, not bulk redistribution of its data.

Riot's [developer policy](https://developer.riotgames.com/docs/lol#game-policy) lists augment win-rate displays in websites, applications and overlays as an unapproved use case. The wording refers to augments broadly without specifically naming Mayhem. This personal prototype is not Riot-approved; applying that restriction to Mayhem is a reasonable reading of the text, not a separate Mayhem-specific ruling verified by this project. No claim is made about enforcement, account bans, or guaranteed account safety.

## Local operation

Champion detection uses Riot's documented read-only Live Client Data API on Windows `127.0.0.1:2999`. TLS relaxation is scoped only to that fixed loopback API, and redirects are refused. Public stats use normal HTTPS verification. The app does not read game memory, inject code, modify item sets, install drivers, automate gameplay, or access account credentials.

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

Checks cover source validation/caching, champion matching, wrong-player rejection, spell-pair validation/current-pair matching, stale-response handling, OCR title ambiguity and layout, and failure states. Native English OCR was also tested using synthetic title images; spell detection was verified against a live game's local API. **A real Mayhem augment selection has not yet been tested on this installation.** OCR may miss stylized/wrapped titles or unusual display layouts; use the manual dropdown fallback.

Mayhem Companion is not endorsed by Riot Games and does not reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
