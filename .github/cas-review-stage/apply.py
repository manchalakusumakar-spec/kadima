"""CI-only publication of pinned, locally tested source. No deployment or activation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import zlib

STAGE = Path('.github/cas-review-stage')
WORKFLOW = Path('.github/workflows/cas-safety-review.yml')
BASE = '6d6aff7ef82ab6fec95c62e809a4e262059a550d'
PATCH_HASH = '83eb699ab047018514e35c9d7e82e55bee4f54d7e4062e5337260c2550b6da9a'
REPAIRS = {
  1: ('1d520c074721624e779163acd9b03e9ade1a0783a1b6e646660b30c7212f8f4b',
      'cb80cf2cae97169ed2a682f9d9907eb83d9314b0e2262d24de7941b6ffd02d07',
      [(15271, 15271, '', '/ftEm4Rr7tUue3')]),
  2: ('c13f222fb8c1857b1c6e3e84a1c246a00ab7eebaf7632ed66bc36b878a446133',
      '825add4748c50648796d5d0207366a128044e2e00c9566320000cc164ec756bb',
      [(870, 871, '+', 'T')]),
  3: ('aa2b3ba73b4e966af952066549ede018a00a97b3f97d204df42b5f93edae131c',
      'aa2b3ba73b4e966af952066549ede018a00a97b3f97d204df42b5f93edae131c', []),
  4: ('63d50d7059779cbc20e8b218036f0cea4639f92cb4849d97a145d5d15cd4b509',
      '63d50d7059779cbc20e8b218036f0cea4639f92cb4849d97a145d5d15cd4b509', [])}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def git(*args, data=None):
    return subprocess.run(['git', *args], input=data, check=True, capture_output=True).stdout


def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true'
            or os.environ.get('GITHUB_REPOSITORY') != 'manchalakusumakar-spec/kadima'
            or os.environ.get('GITHUB_REF') != 'refs/heads/codex/cas-safety-refinement'
            or git('rev-parse', 'HEAD').decode().strip() != os.environ.get('GITHUB_SHA')):
        raise RuntimeError('isolated_review_checkout_required')
    if git('status', '--porcelain'):
        raise RuntimeError('clean_review_checkout_required')
    metadata = json.loads((STAGE/'publication.json').read_text())
    if metadata['baseline_commit'] != BASE or metadata['patch_sha256'] != PATCH_HASH:
        raise RuntimeError('review_identity_changed')
    parts = []
    for number, (received, expected, edits) in REPAIRS.items():
        text = (STAGE/f'part{number}.b64').read_text().strip()
        if sha(text.encode()) != received:
            raise RuntimeError('staged_transport_changed')
        for start, end, old, new in reversed(edits):
            if text[start:end] != old:
                raise RuntimeError('transport_repair_anchor_changed')
            text = text[:start] + new + text[end:]
        if sha(text.encode()) != expected:
            raise RuntimeError('corrected_transport_mismatch')
        parts.append(text)
    decoder = zlib.decompressobj()
    patch = decoder.decompress(base64.b64decode(''.join(parts), validate=True), 1000000)
    if not decoder.eof or decoder.unused_data or sha(patch) != PATCH_HASH:
        raise RuntimeError('patch_digest_mismatch')
    publisher = WORKFLOW.read_bytes()
    WORKFLOW.write_bytes(git('show', BASE+':'+WORKFLOW.as_posix()))
    names = set(re.findall(rb'^\+\+\+ b/(.+)$', patch, flags=re.MULTILINE))
    if names != {p.encode() for p in metadata['files']}:
        raise RuntimeError('patch_path_set_changed')
    for name, hashes in metadata['files'].items():
        p = Path(name)
        if p.is_absolute() or '..' in p.parts or p.is_symlink():
            raise RuntimeError('unsafe_source_path')
        before = sha(p.read_bytes()) if p.exists() else None
        if before != hashes['before']:
            raise RuntimeError('baseline_source_changed:'+name)
    for name, hashes in metadata['files'].items():
        if hashes['before'] is None:
            header = f'diff --git a/{name} b/{name}\n'.encode()
            if patch.count(header) != 1:
                raise RuntimeError('new_file_header_changed')
            patch = patch.replace(header, header+b'new file mode 100644\n')
    if sha(patch) != '928e24733ac4b282f7bf48e7394142feaa9a69ad5e4a0f00df61c9efe682b83f':
        raise RuntimeError('canonical_patch_digest_mismatch')
    git('apply', '--check', '-', data=patch)
    git('apply', '-', data=patch)
    for name, hashes in metadata['files'].items():
        if sha(Path(name).read_bytes()) != hashes['after']:
            raise RuntimeError('resulting_source_changed:'+name)
    out = Path('/tmp/cas-publication-results')
    out.mkdir(exist_ok=True)
    (out/'final-workflow.yml').write_bytes(WORKFLOW.read_bytes())
    commands = [
        ('source.json', ['python3','-I','-B','tools/check_cas_release.py']),
        ('cas-tests.txt', ['python3','-I','-B','tools/test_cas_safety.py']),
        ('legacy-offline.txt', ['python3','-I','-B','tools/run_cas_regressions.py','test_offline.py']),
        ('legacy-audit.txt', ['python3','-I','-B','tools/run_cas_regressions.py','test_audit_fixes.py']),
        ('compilation.txt', ['python3','-m','compileall','-q','kadima','tools'])]
    for log, command in commands:
        with (out/log).open('wb') as handle:
            subprocess.run(command, check=True, stdout=handle, stderr=subprocess.STDOUT, timeout=90)
    for name, hashes in metadata['files'].items():
        if sha(Path(name).read_bytes()) != hashes['after']:
            raise RuntimeError('source_changed_during_tests:'+name)
    expected_stage = {'publication.json','apply.py',*(f'part{i}.b64' for i in range(1,5))}
    if {p.name for p in STAGE.iterdir()} != expected_stage:
        raise RuntimeError('unexpected_staging_file')
    for p in STAGE.iterdir():
        p.unlink()
    STAGE.rmdir()
    WORKFLOW.write_bytes(publisher)
    (out/'tested-patch-sha256.txt').write_text(PATCH_HASH+'\n')
    print('Pinned source verified; 100 CAS tests and both legacy suites passed.')
    print('Publication only; server deployment and activation are not performed.')
    print('Intermediate publication requires the final read-only workflow commit.')


if __name__ == '__main__':
    main()
