#!/usr/bin/env python3
"""Run in YOUR terminal to save a key without putting it in chat, argv or shell history."""
import argparse
import getpass
import os
from pathlib import Path
import sys
import warnings


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path',type=Path,default=Path.home()/'.config/jbrain/openai.key')
    args=parser.parse_args()
    path=args.path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        print(f'File already exists; it was not read or overwritten: {path}',file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print('Run this command yourself in an interactive terminal. Piped input is refused.',file=sys.stderr)
        return 1
    try:
        with warnings.catch_warnings():
            # Never permit getpass to fall back to an input method that echoes the key.
            warnings.simplefilter('error',getpass.GetPassWarning)
            key=getpass.getpass('OpenAI API key (hidden): ').strip()
    except (getpass.GetPassWarning,EOFError,KeyboardInterrupt):
        print('\nHidden input unavailable or cancelled; no key was saved.',file=sys.stderr)
        return 1
    if not key or any(ch.isspace() for ch in key):
        print('Enter one nonempty API key without whitespace; no key was saved.',file=sys.stderr)
        return 1
    path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Exclusive creation also prevents following/replacing a file created after the initial check.
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w') as handle:
        handle.write(key+'\n')
        handle.flush()
        os.fsync(handle.fileno())
    del key
    print(f'Saved privately: {path}')
    print('Share only this file path with the agent, never the key.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
