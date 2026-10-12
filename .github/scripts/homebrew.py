#!/usr/bin/env python3
"""Open a narrowly scoped Homebrew PR for the verified release."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
from release import CONFIG, api, metadata

version, _ = metadata()
app = CONFIG['app']
name = app.lower()
tap = 'steipete/homebrew-tap'
branch = f'release/{name}-{version}'
archive = f'{app}-{version}.zip'
cask_digest = hashlib.sha256(Path('release-assets', archive).read_bytes()).hexdigest()
current = api(f'repos/{tap}/git/ref/heads/main')['object']['sha']
updates = []
for path in [f'Casks/{name}.rb'] + ([f'Formula/{name}.rb'] if app == 'Nameplate' else []):
    response = api(f'repos/{tap}/contents/{path}?ref={current}')
    source = base64.b64decode(response['content']).decode()
    if path.startswith('Casks/'):
        source, count = re.subn(r'(?m)^  version "[^"]+"$', f'  version "{version}"', source)
        assert count == 1, 'Unexpected cask version layout'
        digest = cask_digest
    else:
        url = f'https://github.com/{CONFIG["repository"]}/archive/refs/tags/v{version}.tar.gz'
        with urllib.request.urlopen(url, timeout=120) as download:
            digest = hashlib.sha256(download.read()).hexdigest()
        source, count = re.subn(r'(?m)^  url "[^"]+"$', f'  url "{url}"', source)
        assert count == 1, 'Unexpected formula URL layout'
    source, count = re.subn(r'(?m)^  sha256 "[0-9a-f]+"$', f'  sha256 "{digest}"', source)
    assert count == 1, 'Unexpected Homebrew checksum layout'
    updates.append((path, response['sha'], source))
api(f'repos/{tap}/git/refs', ref=f'refs/heads/{branch}', sha=current)
for path, sha, source in updates:
    api(f'repos/{tap}/contents/{path}', method='PUT', message=f'chore: update {name} to {version}', content=base64.b64encode(source.encode()).decode(), sha=sha, branch=branch)
with tempfile.NamedTemporaryFile(mode='w', suffix='.md') as body:
    body.write(f'Updates {app} to [{version}](https://github.com/{CONFIG["repository"]}/releases/tag/v{version}).\n\nThe macOS checksum comes from the signed, notarized release archive verified by the release workflow.\n')
    body.flush()
    subprocess.run(['gh', 'pr', 'create', '--repo', tap, '--base', 'main', '--head', branch, '--title', f'chore: update {name} to {version}', '--body-file', body.name], check=True)
