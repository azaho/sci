#!/usr/bin/env python3
"""Regenerate into a temporary directory and compare every committed artifact."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile

HERE=Path(__file__).resolve().parent
CORPUS=HERE.parent/'0.1'

def inventory(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}

with tempfile.TemporaryDirectory(prefix='sci-fixtures-rebuild-') as td:
    rebuilt=Path(td)/'0.1'
    subprocess.run([sys.executable,str(HERE/'generate.py'),'--output',str(rebuilt)],check=True)
    actual,expected=inventory(rebuilt),inventory(CORPUS)
    differences=sorted(p for p in actual.keys()|expected.keys() if actual.get(p)!=expected.get(p))
    if differences:
        raise SystemExit('Regeneration differs: '+', '.join(differences))
    print(f'All {len(actual)} corpus files regenerate byte for byte.')
