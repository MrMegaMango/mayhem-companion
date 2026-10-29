# Windows installer builds

The **Windows installer** GitHub Actions workflow uses standard Python to build an x64 Windows app and
tests installation, the installed runtime, and removal on a clean Windows runner.
Run it from Actions, then publish the reviewed `MayhemCompanionSetup.exe` and
`SHA256SUMS.txt` files as a GitHub release. The website's download link follows the
latest release. Publish the release before deploying a website that links to it.

For a local bundle, create a separate Windows Python 3.12 virtual environment and
install `packaging/requirements-build.txt`.

Use an official Python distribution. Customized embedded Python runtimes may
depend on extra vendor files and should not be used for release builds. Run:

```powershell
python packaging/build.py --bundle-only
```

Omit `--bundle-only` when Inno Setup 6 is installed to also create the installer.
Builds never include user settings, logs, artwork caches, or credentials. The two
bundled JSON resources are the Arena name reference and the public analytics
endpoint. The installed app keeps user data outside its installation directory.

Run an offline check of the frozen runtime without starting the app:

```powershell
MayhemCompanion.exe --self-test --report C:\Temp\mayhem-smoke.json
```

Exit 0 means all checks passed. Exit 1 means a package/runtime failure. Exit 2 with
`english_ocr_not_installed` means the host lacks the optional Windows English OCR
capability; manual lookup still works. The check creates no settings, usage events,
tray icon, game connections, or network requests. Inspect the complete report.

The installer is currently unsigned. Do not describe it as signed or instruct
users to disable security software. Release checksums verify transfer integrity,
not publisher identity. Signing can be added when a certificate is available.

Third-party licenses are included in the installed `_internal/licenses` folder.
Pystray is distributed as replaceable Python source under `_internal/pystray`.
