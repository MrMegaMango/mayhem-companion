from pathlib import Path
import os
from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).parent
generated = Path(os.environ['MAYHEM_BUILD_DIR'])
winrt_data, winrt_binaries, winrt_imports = collect_all('winrt')
a = Analysis(
    [str(root / 'packaging' / 'launcher.py')],
    pathex=[str(root)],
    binaries=winrt_binaries,
    datas=winrt_data + [
        (str(root / 'mayhem' / 'arena_augments.json'), 'mayhem'),
        (str(root / 'mayhem' / 'analytics_config.json'), 'mayhem'),
        (str(generated / 'licenses'), 'licenses'),
        (str(root / 'packaging' / 'THIRD-PARTY-NOTICES.txt'), '.'),
    ],
    hiddenimports=winrt_imports + ['pystray', 'pystray._win32',
                                  'pystray._util.win32', 'mayhem.autostart'],
    excludes=['pytest', 'unittest', 'pip', 'setuptools'],
    module_collection_mode={'pystray': 'py'},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name='MayhemCompanion', debug=False, strip=False, upx=False,
    console=False, icon=str(root / 'packaging' / 'app.ico'),
    version=str(generated / 'version.txt'),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='MayhemCompanion')
