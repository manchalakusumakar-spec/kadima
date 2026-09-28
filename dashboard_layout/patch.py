"""Build a layout-enabled HTML copy; never changes services or trading state."""
from pathlib import Path
import argparse

ROOT = Path(__file__).resolve().parent
START = '<!-- BEGIN CELESTIAL LAYOUT -->'
END = '<!-- END CELESTIAL LAYOUT -->'


def patch(source):
    if START in source or END in source:
        raise ValueError('layout_already_present')
    if source.count('</body>') != 1 or source.count('id="desk"') != 1:
        raise ValueError('unsupported_dashboard_structure')
    css = (ROOT/'layout.css').read_text()
    script = (ROOT/'layout.js').read_text()
    return source.replace('</body>', f'{START}\n<style>{css}</style>\n<script>{script}</script>\n{END}\n</body>')


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('source', type=Path)
    cli.add_argument('--output', type=Path, required=True)
    args = cli.parse_args()
    prepared = patch(args.source.read_text())
    with args.output.open('x') as output:
        output.write(prepared)
    print('Created layout-enabled copy:', args.output)
