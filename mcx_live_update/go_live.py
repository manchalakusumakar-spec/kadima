#!/usr/bin/env python3
"""One-command guarded MCX install + activation.

Runs the reviewed source installer first, then the reviewed activation helper.
No test order is sent. Any failed source, broker, reconciliation, news, service,
runtime-flag or capital check stops the sequence immediately.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PACKAGE=Path(__file__).resolve().parents[1]


def run(stage, args):
    result=subprocess.run(args,cwd=PACKAGE,text=True)
    if result.returncode:
        print(json.dumps({
            "mcx_go_live":"BLOCKED",
            "stage":stage,
            "exit_code":result.returncode,
            "note":"No bypass attempted. Resolve the reported fail-closed blocker and rerun the same command."
        }))
        raise SystemExit(result.returncode)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mcx-capital",type=float,required=True,
                        help="Maximum rupees allocated to the MCX lane.")
    args=parser.parse_args()
    if os.geteuid()!=0:
        raise SystemExit("Run with sudo -n python3.")
    if not (0 < args.mcx_capital <= 100000):
        raise SystemExit("--mcx-capital must be >0 and <=100000.")

    python=sys.executable
    run("source_install",[
        python,str(PACKAGE/"mcx_live_update/install.py"),
        "--broker-check","--apply"
    ])
    run("live_activation",[
        python,str(PACKAGE/"tools/activate_execution.py"),
        "--apply","--enable-mcx","--mcx-capital",str(args.mcx_capital)
    ])
    print(json.dumps({
        "mcx_go_live":"ENABLED_SUBJECT_TO_EXISTING_ENTRY_GATES",
        "capital_limit_inr":args.mcx_capital,
        "note":"No test order submitted. MCX worker still requires its live 5-minute confirmation and all runtime risk gates."
    },indent=2))


if __name__=="__main__":
    main()
