import argparse
from pathlib import Path
from adapter import source
p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
for name in ['verify.ts','metadata.ts','unicode18.ts','timestamp.ts','vendor/pkijs.js']:
 target=a.output/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source(name))
for name in ['index.ts','check.ts']:(a.output/name).write_text(Path(__file__).with_name(name).read_text())
