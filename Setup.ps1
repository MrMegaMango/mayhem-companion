param(
    [string]$InstallPath = (Join-Path $env:USERPROFILE 'MayhemCompanion'),
    [switch]$StartWithLeague
)
$ErrorActionPreference = 'Stop'
$sourceRoot = $PSScriptRoot
$pythonCandidates = @(
    (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
    (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')
)
$python = $pythonCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $python) {
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
}
if (-not $python) { throw 'Python 3.12 or newer is needed to run the companion.' }
& $python -c "import sys,tkinter; assert sys.version_info >= (3,12)"
if ($LASTEXITCODE -ne 0) { throw 'A working Python 3.12+ with Tk is required.' }
New-Item -ItemType Directory -Force -Path $InstallPath | Out-Null
$venvPython = Join-Path $InstallPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $python -m venv (Join-Path $InstallPath '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Creating the Python environment failed.' }
}
& $venvPython -m pip install -r (Join-Path $sourceRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Installing the companion dependencies failed.' }
$appPath = Join-Path $InstallPath 'app'
New-Item -ItemType Directory -Force -Path $appPath | Out-Null
Copy-Item -LiteralPath (Join-Path $sourceRoot 'mayhem') -Destination $appPath -Recurse -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'README.md') -Destination $InstallPath -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'Launch.vbs') -Destination $InstallPath -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'Configure-Autostart.vbs') -Destination $InstallPath -Force
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut((Join-Path $desktop 'Mayhem Companion.lnk'))
$shortcut.TargetPath = Join-Path $env:WINDIR 'System32\wscript.exe'
$shortcut.Arguments = '"' + (Join-Path $InstallPath 'Launch.vbs') + '"'
$shortcut.WorkingDirectory = $InstallPath
$shortcut.Description = 'Local ARAM Mayhem champion and augment companion'
$shortcut.IconLocation = (Join-Path $InstallPath '.venv\Scripts\pythonw.exe') + ',0'
$shortcut.Save()
if ($StartWithLeague) {
    & (Join-Path $env:WINDIR 'System32\cscript.exe') //NoLogo (Join-Path $InstallPath 'Configure-Autostart.vbs') $InstallPath
    if ($LASTEXITCODE -ne 0) { throw 'Configuring automatic launch failed.' }
}
Write-Output "Installed to $InstallPath. Open Mayhem Companion on your desktop."
