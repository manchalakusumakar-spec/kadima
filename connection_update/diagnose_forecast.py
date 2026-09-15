"""Read-only source inventory. Prints filenames/function names, never source or secrets."""
import ast
import os
from pathlib import Path

roots=[Path('/home/ubuntu/kadima-py'),Path('/opt/kadima')]
files=list(Path('/home/ubuntu').glob('*.py'))
for root in roots:
    if not root.exists():
        continue
    for directory, dirs, names in os.walk(root):
        dirs[:]=[d for d in dirs if not d.startswith('.') and d not in
                 {'venv','env','node_modules','__pycache__','tests','cache','state'}]
        files.extend(Path(directory)/name for name in names if name.endswith('.py'))
        if len(files)>3000:
            break
found=0
for path in sorted(set(files)):
    try:
        if path.stat().st_size>500_000:
            continue
        tree=ast.parse(path.read_text())
        names=[n.name for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
               and any(s in n.name.lower() for s in ('forecast','predict','projection'))]
        if names:
            print(str(path)+': '+', '.join(names))
            found+=1
    except (OSError,UnicodeError,SyntaxError):
        pass
if not found:
    print('No forecast/predict/projection function names found in searched source directories.')
print('Output lists source locations only; it does not establish that a model is running.')
