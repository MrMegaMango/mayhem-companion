"""Build the Windows bundle; only an isolated build environment is needed."""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def collect_licenses(destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    packages = ('pillow', 'pystray', 'six', 'typing_extensions', 'pyinstaller',
                'winrt-runtime', 'winrt-Windows.Foundation',
                'winrt-Windows.Foundation.Collections', 'winrt-Windows.Globalization',
                'winrt-Windows.Graphics.Imaging', 'winrt-Windows.Media.Ocr',
                'winrt-Windows.Storage.Streams')
    manifest = []
    for package in packages:
        dist = metadata.distribution(package)
        target = destination / package
        target.mkdir(exist_ok=True)
        for entry in dist.files or ():
            name = Path(entry).name.lower()
            if ('.dist-info/' in str(entry).replace('\\', '/')
                    and (name.startswith(('license', 'copying', 'notice')))):
                shutil.copy2(dist.locate_file(entry), target / Path(entry).name)
        manifest.append({'name': dist.metadata['Name'], 'version': dist.version,
                         'project_urls': dist.metadata.get_all('Project-URL') or []})
    for license_file in (ROOT / 'packaging' / 'licenses').glob('*'):
        shutil.copy2(license_file, destination / license_file.name)
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if not python_license.is_file():
        raise RuntimeError('The Python runtime LICENSE.txt must accompany the bundle')
    shutil.copy2(python_license, destination / 'Python-LICENSE.txt')
    for directory, label in (('tcl8.6', 'Tcl'), ('tk8.6', 'Tk')):
        license_file = Path(sys.base_prefix) / 'tcl' / directory / 'license.terms'
        if license_file.is_file():
            shutil.copy2(license_file, destination / f'{label}-license.terms')
        elif not (destination / f'{label}-license.terms').is_file():
            raise RuntimeError(f'{label} license must accompany the bundle')
    import ssl
    import tkinter
    import zlib
    from xml.parsers import expat
    interpreter = tkinter.Tk()
    interpreter.withdraw()
    manifest.extend([
        {'name': 'Python', 'version': sys.version.split()[0]},
        {'name': 'Tcl', 'version': interpreter.call('info', 'patchlevel')},
        {'name': 'Tk', 'version': interpreter.call('package', 'provide', 'Tk')},
        {'name': 'OpenSSL', 'version': ssl.OPENSSL_VERSION},
        {'name': 'zlib', 'version': zlib.ZLIB_RUNTIME_VERSION},
        {'name': 'Expat', 'version': expat.EXPAT_VERSION},
    ])
    interpreter.eval('destroy .')
    (destination / 'versions.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--work-dir', type=Path, default=ROOT / 'build' / 'windows')
    parser.add_argument('--bundle-only', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or sys.maxsize < 2**32:
        parser.error('Build with 64-bit Python on Windows')
    work = args.work_dir.resolve()
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(ROOT))
    from mayhem import __version__
    collect_licenses(work / 'licenses')
    version = tuple(int(part) for part in __version__.split('.')) + (0,)
    (work / 'version.txt').write_text(
        'VSVersionInfo(ffi=FixedFileInfo(filevers=' + repr(version) + ', prodvers=' + repr(version)
        + ', mask=0x3f, flags=0, OS=0x40004, fileType=0x1, subtype=0, date=(0, 0)), '
        + 'kids=[StringFileInfo([StringTable("040904B0", ['
        + 'StringStruct("FileDescription", "Mayhem Companion"), '
        + 'StringStruct("ProductName", "Mayhem Companion"), '
        + f'StringStruct("FileVersion", "{__version__}"), '
        + f'StringStruct("ProductVersion", "{__version__}")])]), '
        + 'VarFileInfo([VarStruct("Translation", [1033, 1200])])])\n', encoding='utf-8')
    env = dict(os.environ, MAYHEM_BUILD_DIR=str(work))
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
                    '--distpath', str(work / 'dist'), '--workpath', str(work / 'pyinstaller'),
                    str(ROOT / 'packaging' / 'MayhemCompanion.spec')], env=env, check=True)
    bundle = work / 'dist' / 'MayhemCompanion'
    shutil.copy2(ROOT / 'packaging' / 'THIRD-PARTY-NOTICES.txt', bundle / 'THIRD-PARTY-NOTICES.txt')
    if args.bundle_only:
        print(f'Bundle: {bundle}')
        return
    compiler = shutil.which('ISCC.exe')
    if not compiler:
        candidates = [Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)'))
                      / 'Inno Setup 6' / 'ISCC.exe']
        compiler = next((str(path) for path in candidates if path.is_file()), None)
    if not compiler:
        raise RuntimeError('Inno Setup is required to build the installer; use the Windows release workflow')
    output = work / 'installer'
    output.mkdir(exist_ok=True)
    subprocess.run([compiler, f'/DAppVersion={__version__}', f'/DBundleDir={bundle}',
                    f'/DInstallerOutputDir={output}', str(ROOT / 'packaging' / 'installer.iss')], check=True)
    installer = output / 'MayhemCompanionSetup.exe'
    digest = hashlib.file_digest(installer.open('rb'), 'sha256').hexdigest()
    (output / 'SHA256SUMS.txt').write_text(digest + '  MayhemCompanionSetup.exe\n', encoding='ascii')
    print(f'Installer: {installer} ({installer.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
