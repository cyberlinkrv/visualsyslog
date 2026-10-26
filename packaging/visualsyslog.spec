# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
root = Path(SPECPATH).parent
block_cipher = None
a = Analysis([str(root / 'src/visualsyslog/__main__.py')], pathex=[str(root / 'src')],
             binaries=[], datas=[(str(root / 'src/visualsyslog/static'), 'visualsyslog/static')],
             hiddenimports=['uvicorn.logging','uvicorn.loops.auto','uvicorn.protocols.http.auto'],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [], name='VisualSyslog',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=True,
          console=True, disable_windowed_traceback=False, argv_emulation=False,
          target_arch=None, codesign_identity=None, entitlements_file=None)
