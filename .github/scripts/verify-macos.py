#!/usr/bin/env python3
"""Verify the release bundle, deployment targets, and launch on the build runner."""
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from release import deployment_targets

config = json.loads(Path('.github/release.json').read_text())
app = Path(sys.argv[1]).resolve()
plist = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
version = dict(re.findall(r'^(\w+)=(.+)$', Path('version.env').read_text(), re.M))
assert plist['CFBundleIdentifier'] == config['bundle_id']
assert plist['CFBundleShortVersionString'] == version['MARKETING_VERSION']
assert plist['CFBundleVersion'] == version['BUILD_NUMBER']
assert plist['LSMinimumSystemVersion'] == config['minimum_macos']
assert plist['SUPublicEDKey'] == config['sparkle_public_key']
assert plist['SUFeedURL'] == f'https://raw.githubusercontent.com/{config["repository"]}/main/appcast.xml'
main = app / 'Contents/MacOS' / config['app']
required = set(config['architectures'])
assert set(subprocess.check_output(['lipo', '-archs', str(main)], text=True).split()) == required
limit = tuple(map(int, config['minimum_macos'].split('.')))
seen = set()
for path in app.rglob('*'):
    if not path.is_file() or path.resolve() in seen:
        continue
    seen.add(path.resolve())
    if 'Mach-O' not in subprocess.check_output(['file', '-b', str(path)], text=True):
        continue
    architectures = set(subprocess.check_output(['lipo', '-archs', str(path)], text=True).split())
    assert required <= architectures, f'{path}: missing architecture'
    for arch in required:
        load = subprocess.check_output(['otool', '-arch', arch, '-l', str(path)], text=True)
        minima = deployment_targets(load)
        assert minima, f'{path}: no deployment target for {arch}'
        assert all(tuple(map(int, m.split('.')[:2])) <= limit for m in minima), f'{path}: deployment target exceeds {limit}: {minima}'
subprocess.run(['codesign', '--verify', '--deep', '--strict', '-R', f'=anchor apple generic and certificate leaf[subject.OU] = "Y5PE65HELJ" and identifier "{config["bundle_id"]}"', str(app)], check=True)
subprocess.run(['spctl', '--assess', '--type', 'execute', '--verbose=2', str(app)], check=True)
subprocess.run(['xcrun', 'stapler', 'validate', str(app)], check=True)
if config['app'] == 'Nameplate':
    help_result = subprocess.run([str(app / 'Contents/Helpers/nameplate'), '--help'], text=True, capture_output=True)
    # The existing CLI's usage() exits 2 for help as well as invalid arguments.
    assert help_result.returncode == 2 and 'nameplate attention' in (help_result.stdout + help_result.stderr), 'CLI help smoke failed'
with tempfile.TemporaryDirectory() as home:
    environment = dict(os.environ, HOME=home, CFFIXED_USER_HOME=home)
    for name in [
        'SPARKLE_PRIVATE_KEY',
        'GH_TOKEN',
        'GITHUB_TOKEN',
        'MACOS_SIGNING_P12',
        'MACOS_SIGNING_P12_PASSWORD',
        'APP_STORE_CONNECT_API_KEY_P8',
    ]:
        environment.pop(name, None)
    process = subprocess.Popen([str(main)], env=environment)
    try:
        time.sleep(8)
        assert process.poll() is None, 'App exited during launch smoke test'
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
print('Verified metadata, architectures, all Mach-O minimum OS versions, signing, notarization, and launch')
