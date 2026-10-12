#!/usr/bin/env python3
"""Release policy and publication; builds use the repository's packaging scripts."""
import argparse
import base64
import datetime as dt
import hashlib
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / '.github/release.json').read_text())
SPARKLE = 'http://www.andymatuschak.org/xml-namespaces/sparkle'
ET.register_namespace('sparkle', SPARKLE)


def run(*args, **kwargs):
    return subprocess.check_output(args, text=True, **kwargs).strip()


def api(path, method=None, **fields):
    args = ['gh', 'api', path]
    if method:
        args += ['--method', method]
    for key, value in fields.items():
        args += ['-f', f'{key}={value}']
    return json.loads(run(*args))


def metadata():
    text = (ROOT / 'version.env').read_text()
    values = dict(re.findall(r'^(MARKETING_VERSION|BUILD_NUMBER)=([0-9.]+)$', text, re.M))
    version = values.get('MARKETING_VERSION', '')
    assert re.fullmatch(r'\d+\.\d+\.\d+', version), 'Expected X.Y.Z in version.env'
    assert re.fullmatch(r'[1-9]\d*', values.get('BUILD_NUMBER', '')), 'Invalid BUILD_NUMBER'
    if CONFIG['app'] == 'Nameplate':
        for path in ['linux/nameplate/Cargo.toml', 'linux/nameplate-core/Cargo.toml']:
            assert re.search(r'^version\s*=\s*"' + re.escape(version) + '"$', (ROOT / path).read_text(), re.M), f'{path}: version mismatch'
        assert f'<Version>{version}</Version>' in (ROOT / 'windows/Directory.Build.props').read_text(), 'Windows version mismatch'
    return version, values['BUILD_NUMBER']


def notes(version):
    text = (ROOT / 'CHANGELOG.md').read_text()
    pattern = r'^## ' + re.escape(version) + r' - (\d{4}-\d{2}-\d{2})\n(.*?)(?=^## |\Z)'
    matches = list(re.finditer(pattern, text, re.M | re.S))
    assert len(matches) == 1, 'Expected one dated CHANGELOG section for this version'
    date = dt.date.fromisoformat(matches[0][1])
    assert date <= dt.datetime.now(dt.timezone.utc).date(), 'Future changelog date'
    body = matches[0][2].strip()
    assert body, 'Release notes are empty'
    return body + '\n'


def require_ci(sha):
    response = api(f'repos/{CONFIG["repository"]}/actions/workflows/ci.yml/runs?event=push&head_sha={sha}&per_page=50')
    runs = [r for r in response['workflow_runs'] if r['head_sha'] == sha and r['event'] == 'push' and r['head_branch'] == 'main']
    assert runs, 'No push CI run on main for the exact release commit'
    latest = max(runs, key=lambda r: (r['run_number'], r['run_attempt']))
    assert latest['status'] == 'completed' and latest['conclusion'] == 'success', 'Exact-commit push CI is not green'
    return latest['id']


def require_absent(tag):
    # The tag lookup omits drafts. List authenticated releases so a failed draft
    # remains a durable version reservation. Errors and excessive history fail closed.
    for page in range(1, 21):
        releases = api(f'repos/{CONFIG["repository"]}/releases?per_page=100&page={page}')
        assert not any(item['tag_name'] == tag for item in releases), 'This version already has a GitHub Release (including drafts)'
        if len(releases) < 100:
            return
    raise RuntimeError('Could not establish release absence within 20 pages')


def validate():
    dry = os.environ['DRY_RUN'] == 'true'
    version, build = metadata()
    body = notes(version)
    sha = run('git', 'rev-parse', 'HEAD')
    subprocess.run(['git', 'merge-base', '--is-ancestor', sha, 'origin/main'], check=True)
    tag = f'v{version}'
    if not dry:
        assert os.environ['RELEASE_TAG'] == tag, 'Tag/version mismatch'
        assert run('git', 'cat-file', '-t', f'refs/tags/{tag}') == 'tag', 'Release tag must be annotated'
        assert run('git', 'rev-parse', f'{tag}^{{commit}}') == sha, 'Tag commit mismatch'
        subprocess.run(['git', '-c', 'gpg.format=ssh', '-c', 'gpg.ssh.allowedSignersFile=.github/release-allowed-signers', 'verify-tag', tag], check=True)
        feed = ET.parse(ROOT / 'appcast.xml')
        builds = [int(e.text) for e in feed.findall(f'.//{{{SPARKLE}}}version')]
        assert not builds or int(build) > max(builds), 'BUILD_NUMBER must increase beyond the published feed'
    ci = require_ci(sha)
    Path('release-notes.md').write_text(body)
    data = dict(version=version, build=build, tag=tag, sha=sha, dry_run=str(dry).lower(), ci_run_id=str(ci))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        for key, value in data.items():
            output.write(f'{key}={value}\n')
    print(f'Validated {tag} at {sha}, push CI {ci}, dry_run={dry}')


def availability():
    assert os.environ["DRY_RUN"] == "false", "Availability job is publication-only"
    version, _ = metadata()
    require_absent(f"v{version}")


def asset_names(version):
    return sorted(name.format(version=version) for name in CONFIG['assets'])


def checksums(directory):
    return ''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in sorted(directory.iterdir()) if p.is_file() and p.name != 'SHA256SUMS')


def inventory():
    directory = Path('release-assets')
    version, _ = metadata()
    names = asset_names(version) + ['appcast.xml', 'RELEASE-NOTES.md']
    assert sorted(p.name for p in directory.iterdir()) == sorted(names), 'Release asset inventory mismatch'
    assert all(p.is_file() and p.stat().st_size > 0 for p in directory.iterdir()), 'Empty or non-file asset'
    assert (directory / 'RELEASE-NOTES.md').read_text() == notes(version)
    (directory / 'SHA256SUMS').write_text(checksums(directory))
    print('Verified exact asset inventory and wrote SHA256SUMS')


def appcast():
    """Sign the archive and verify against the embedded public key before publication."""
    import plistlib
    version, build = metadata()
    directory = Path('release-assets')
    archive = directory / f'{CONFIG["app"]}-{version}.zip'
    key_bytes = base64.b64decode(os.environ['SPARKLE_PRIVATE_KEY'].strip(), validate=True)
    assert len(key_bytes) in (32, 96), 'Unexpected Sparkle key format'
    # Legacy Sparkle keys contain a 64-byte expanded secret followed by the public key.
    swift = '''import Foundation
import CryptoKit
let data = Data(base64Encoded: ProcessInfo.processInfo.environment["SPARKLE_PRIVATE_KEY"]!.trimmingCharacters(in: .whitespacesAndNewlines))!
if data.count == 32 {
  let key = try Curve25519.Signing.PrivateKey(rawRepresentation: data)
  print(key.publicKey.rawRepresentation.base64EncodedString())
} else {
  print(data.suffix(32).base64EncodedString())
}
'''
    public_key = run('swift', '-', input=swift)
    plist = plistlib.loads(Path(os.environ['APP_BUNDLE'], 'Contents/Info.plist').read_bytes())
    assert public_key == plist['SUPublicEDKey'] == CONFIG['sparkle_public_key'], 'Sparkle signing key does not match the shipped app'
    with tempfile.TemporaryDirectory() as temp:
        key = Path(temp, 'sparkle.key')
        key.write_text(os.environ['SPARKLE_PRIVATE_KEY'].strip() + '\n')
        key.chmod(0o600)
        signer = os.environ['SPARKLE_SIGN_UPDATE']
        signed = run(signer, '--ed-key-file', str(key), str(archive))
        signature = re.search(r'sparkle:edSignature="([^"]+)"', signed)[1]
        subprocess.run([signer, '--ed-key-file', str(key), '--verify', str(archive), signature], check=True)
    tree = ET.parse(ROOT / 'appcast.xml')
    channel = tree.getroot().find('channel')
    for item in list(channel.findall('item')):
        if item.findtext(f'{{{SPARKLE}}}version') == build:
            channel.remove(item)
    item = ET.Element('item')
    ET.SubElement(item, 'title').text = version
    ET.SubElement(item, 'pubDate').text = dt.datetime.now(dt.timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')
    ET.SubElement(item, f'{{{SPARKLE}}}version').text = build
    ET.SubElement(item, f'{{{SPARKLE}}}shortVersionString').text = version
    ET.SubElement(item, f'{{{SPARKLE}}}minimumSystemVersion').text = CONFIG['minimum_macos']
    if CONFIG['architectures'] == ['arm64']:
        ET.SubElement(item, f'{{{SPARKLE}}}hardwareRequirements').text = 'arm64'
    ET.SubElement(item, 'description').text = '<pre>' + html.escape(notes(version)) + '</pre>'
    ET.SubElement(item, 'enclosure', {'url': f'https://github.com/{CONFIG["repository"]}/releases/download/v{version}/{archive.name}', 'length': str(archive.stat().st_size), 'type': 'application/octet-stream', f'{{{SPARKLE}}}edSignature': signature})
    channel.insert(0, item)
    ET.indent(tree)
    tree.write(directory / 'appcast.xml', encoding='utf-8', xml_declaration=True)


def publish():
    assert os.environ['DRY_RUN'] == 'false', 'Publication is disabled for dry runs'
    version, _ = metadata()
    tag = f'v{version}'
    sha = os.environ['RELEASE_SHA']
    require_ci(sha)
    require_absent(tag)
    remote_tag = api(f'repos/{CONFIG["repository"]}/git/ref/tags/{tag}')
    assert remote_tag['object']['type'] == 'tag' and remote_tag['object']['sha'] == run('git', 'rev-parse', f'refs/tags/{tag}'), 'Remote tag changed'
    current_feed = api(f'repos/{CONFIG["repository"]}/contents/appcast.xml?ref=main')
    assert base64.b64decode(current_feed['content']) == (ROOT / 'appcast.xml').read_bytes(), 'Feed advanced since this release commit'
    assert run('git', 'rev-parse', f'{tag}^{{commit}}') == sha
    subprocess.run(['git', '-c', 'gpg.format=ssh', '-c', 'gpg.ssh.allowedSignersFile=.github/release-allowed-signers', 'verify-tag', tag], check=True)
    directory = Path('release-assets')
    assert sorted(p.name for p in directory.iterdir()) == sorted(asset_names(version) + ['appcast.xml', 'RELEASE-NOTES.md', 'SHA256SUMS'])
    assert (directory / 'SHA256SUMS').read_text() == checksums(directory), 'Artifact checksum mismatch'
    assert (directory / 'RELEASE-NOTES.md').read_text() == notes(version)
    subprocess.run(['gh', 'release', 'create', tag, '--repo', CONFIG['repository'], '--draft', '--verify-tag', '--title', tag, '--notes-file', str(directory / 'RELEASE-NOTES.md'), *map(str, sorted(directory.iterdir()))], check=True)
    # Verify the uploaded bytes, not just the local artifacts, before making the draft public.
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(['gh', 'release', 'download', tag, '--repo', CONFIG['repository'], '--dir', temp], check=True)
        downloaded = Path(temp)
        assert sorted(p.name for p in downloaded.iterdir()) == sorted(p.name for p in directory.iterdir())
        assert all((downloaded / p.name).read_bytes() == p.read_bytes() for p in directory.iterdir())
    subprocess.run(['gh', 'release', 'edit', tag, '--repo', CONFIG['repository'], '--draft=false', '--latest'], check=True)
    # Keep the historical feed URL: existing installed clients read main/appcast.xml.
    current = api(f'repos/{CONFIG["repository"]}/contents/appcast.xml?ref=main')
    assert current['sha'] == current_feed['sha'], 'Feed changed during publication; finish feed handoff from the verified release'
    api(f'repos/{CONFIG["repository"]}/contents/appcast.xml', method='PUT', message=f'chore: publish {tag} update feed', content=base64.b64encode((directory / 'appcast.xml').read_bytes()).decode(), sha=current['sha'], branch='main')
    after = api(f'repos/{CONFIG["repository"]}/contents/appcast.xml?ref=main')
    assert base64.b64decode(after['content']) == (directory / 'appcast.xml').read_bytes(), 'Published appcast differs'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'availability', 'inventory', 'appcast', 'publish'])
    globals()[parser.parse_args().command]()
