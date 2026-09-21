"""Additive dashboard/gateway patch. Never touches broker or trading settings.

Default is check-only. Apply to a reviewed staging tree; verify service paths,
backups and restart the gateway separately after backend deployment.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import ast

MARKER = '<!-- KADIMA STOCK FAST PANEL v1 -->'
GATEWAY_MARKER = '# KADIMA STOCK FAST ROUTES v1'


def patch_dashboard(text, script):
    if MARKER in text:
        return text
    if text.lower().count('</body>') != 1 or '</script' in script.lower():
        raise ValueError('Unrecognized dashboard or unsafe script terminator')
    index = text.lower().index('</body>')
    return text[:index] + MARKER + '\n<script>\n' + script + '\n</script>\n' + text[index:]


def patch_gateway(text):
    if GATEWAY_MARKER in text:
        return text
    anchor = "@app.post('/celestial/{path:path}')"
    if text.count(anchor) != 1 or "def authenticated(request)" not in text or "def origin_check(request)" not in text:
        raise ValueError('Unrecognized gateway; review before patching')
    addition = (GATEWAY_MARKER + "\nREADS['stock_fast'] = (8081, '/api/kadima/stock-fast')\n"
                "CELESTIAL_WRITES.add('kadima/stock-fast/config')\n\n\n")
    result = text.replace(anchor, addition + anchor)
    ast.parse(result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dashboard', type=Path, required=True)
    parser.add_argument('--gateway', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    script = Path(__file__).with_name('panel.js').read_text()
    changes = [(args.dashboard, patch_dashboard(args.dashboard.read_text(), script)),
               (args.gateway, patch_gateway(args.gateway.read_text()))]
    # Validate both outputs before any write. Existing backups are never overwritten.
    for path, result in changes:
        if path.is_symlink() or not path.is_file():
            raise ValueError('Refusing symlink or non-file destination')
    if not args.apply:
        print('CHECK OK: additive panel and authenticated gateway routes; no files changed.')
        return
    suffix = datetime.now(timezone.utc).strftime('.stock-fast-backup-%Y%m%dT%H%M%S.%fZ')
    for path, result in changes:
        original = path.read_text()
        if original == result:
            continue
        with path.with_name(path.name + suffix).open('x') as handle:
            handle.write(original)
        temporary = path.with_name(path.name + suffix + '.tmp')
        temporary.write_text(result)
        temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    print('Panel/routes written. No service restart or live-trading activation performed.')

if __name__ == '__main__':
    main()
