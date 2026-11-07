"""Only changes needed by these current-spec fixtures; not a conformance upgrade."""
from pathlib import Path
ROOT=Path(__file__).parent.parent/'full'
def source(name):
 s=(ROOT/name).read_text()
 if name=='verify.ts':
  changes={
   'zip: 64_000_000, payload: 50_000_000':'zip: 67_108_864, payload: 67_108_864',
   'text: 1_000_000':'text: 67_108_864',
   'm.schema === "sci-publication/0.18"':'m.schema === "sci-publication/0.1"',
   'u16(p + 4) === 10 && u16(p + 6) === 0':'u16(p + 4) === 10 && u16(p + 10) === 0 && u16(p + 12) === 0x0021 && u16(p + 6) === 0',
   'u16(p + 4) >>> 8 === 0':'u16(p + 4) === 10',
   'const hasTimestamp = entries.at(-1)?.name === "manifest.json.sig.tsr";':'const hasTimestamp = false;',
  }
  for old,new in changes.items():
   assert s.count(old)==1,old
   s=s.replace(old,new)
 return s
