#!/usr/bin/env python3
"""Read named source files only. No imports from KADIMA, secrets, network or writes.

Run on the host to identify installed source. This DOES NOT verify loaded process
identity, freshness, current memberships, broker health, or a live deployment.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path

PATHS = ('kadima/hourly_forecast.py','kadima/hourly_forecast_model.py',
         'kadima/forecast_chain_data.py','kadima/constituent_radar.py',
         'kadima/feed.py','kadima/engine.py','tools/forecast_nightly.py')
SAFE_NAMES = {'MODEL_VERSION','REVISION','STEP_MIN','HEAVY_WEIGHTS','BREADTH_INDEX',
              'FORECAST_INDICES','SIGMA_LOOKBACK','LOOP_SEC','SPOT_FRESH_SEC'}


def inspect(root):
    root=Path(root).resolve(); out={}
    for relative in PATHS:
        path=root/relative
        if not path.is_file():
            out[relative]={'status':'ABSENT'}; continue
        # Do not follow a symlink out of the selected checkout.
        if not path.resolve().is_relative_to(root):
            out[relative]={'status':'OUTSIDE_ROOT_NOT_READ'}; continue
        content=path.read_bytes()
        item={'sha256':hashlib.sha256(content).hexdigest(),
              'git_blob_sha1':hashlib.sha1(b'blob '+str(len(content)).encode()+b'\0'+content).hexdigest(),
              'bytes':len(content),'status':'READ'}
        try:
            tree=ast.parse(content)
            constants={}
            for node in tree.body:
                if isinstance(node,ast.Assign):
                    for target in node.targets:
                        if isinstance(target,ast.Name) and target.id in SAFE_NAMES:
                            try: constants[target.id]=ast.literal_eval(node.value)
                            except (ValueError,TypeError): constants[target.id]='DYNAMIC_NOT_EVALUATED'
            item['selected_constants']=constants
            item['make_forecast_call_keywords']=[sorted(k.arg for k in n.keywords if k.arg)
                for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
                and n.func.attr=='make_forecast']
            item['make_forecast_signatures']=[{'positional':[a.arg for a in n.args.args],
                                             'keyword_only':[a.arg for a in n.args.kwonlyargs]}
                for n in ast.walk(tree) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef))
                and n.name=='make_forecast']
        except (SyntaxError,UnicodeDecodeError): item['ast_status']='NOT_PARSED'
        out[relative]=item
    return {'inspection':'READ_ONLY_STATIC_SOURCE_NOT_RUNTIME_VERIFICATION','files':out}

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--root',required=True)
    args=p.parse_args(); print(json.dumps(inspect(args.root),indent=2))
