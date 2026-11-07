import { sciCharacter, graphicCharacter } from './unicode18.ts';
export function requireThat(ok: unknown, code: string): asserts ok { if (!ok) throw new Error(code); }
export function objectFields(o: any, required: string[], optional: string[], code: string) {
  requireThat(o && typeof o === 'object' && !Array.isArray(o), code);
  requireThat(required.every(k => Object.hasOwn(o,k)) && Object.keys(o).every(k => required.includes(k) || optional.includes(k)),code);
}
export function textField(s: unknown, max: number, lines = false): asserts s is string {
  requireThat(typeof s === 'string', 'text_type');
  let n=0, nonblank=false;
  for (const c of s) {
    requireThat(++n<=max, 'text_length');
    const cp=c.codePointAt(0)!;
    if (cp===32 || (lines && (cp===9 || cp===10 || cp===13))) continue;
    requireThat(sciCharacter(cp),'text_character'); nonblank=true;
  }
  requireThat(nonblank,'text_blank');
}
export function doiKey(target: string): string {
  requireThat(/^doi:(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+\/(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+$/.test(target),'doi_syntax');
  let prefix: string, suffix: string;
  const encoded=target.slice(4).split('/');
  try { prefix=decodeURIComponent(encoded[0]); suffix=decodeURIComponent(encoded[1]); } catch { throw new Error('doi_utf8'); }
  requireThat(/^10\.[0-9]+(?:\.[0-9]+)*$/.test(prefix) && suffix.length>0,'doi_name');
  const decoded=prefix+'/'+suffix;
  for (const c of decoded) requireThat(graphicCharacter(c.codePointAt(0)!),'doi_character');
  // DOI Foundation equivalence: decode, then fold Basic Latin only; never normalize Unicode.
  return decoded.replace(/[A-Z]/g,c=>c.toLowerCase());
}
export function metadata(m: any, paths: Set<string>) {
  objectFields(m,['title','abstract'],['categories','keywords','display','citations'],'metadata_fields');
  textField(m.title,200); textField(m.abstract,2500,true);
  if (m.categories!==undefined) {
    requireThat(Array.isArray(m.categories) && m.categories.length<=3,'categories');
    const seen=new Set();
    for (const c of m.categories) {
      objectFields(c,['scheme','code'],[],'category_fields'); textField(c.scheme,64); textField(c.code,128);
      requireThat(/^[a-z][a-z0-9.-]{0,63}$/.test(c.scheme),'category_scheme');
      const key=JSON.stringify([c.scheme,c.code]);requireThat(!seen.has(key),'category_duplicate');seen.add(key);
    }
  }
  if (m.keywords!==undefined) {
    requireThat(Array.isArray(m.keywords) && m.keywords.length<=20,'keywords');
    for (const k of m.keywords) textField(k,64);
  }
  if (m.display!==undefined) {
    requireThat(Array.isArray(m.display),'display'); const seen=new Set();
    for (const d of m.display) {
      objectFields(d,['path'],['caption'],'display_fields');textField(d.path,240);
      requireThat(d.path.startsWith('files/') && paths.has(d.path) && !seen.has(d.path),'display_path');seen.add(d.path);
      if (d.caption!==undefined) textField(d.caption,2000);
    }
  }
  if (m.citations!==undefined) {
    requireThat(Array.isArray(m.citations) && m.citations.length<=1000,'citations');
    for (const c of m.citations) {
      objectFields(c,['target'],['relations','reason'],'citation_fields');textField(c.target,4096);
      if (!/^sci:sha256:[0-9a-f]{64}$/.test(c.target)) doiKey(c.target);
      if (c.reason!==undefined) textField(c.reason,1000,true);
      if (c.relations!==undefined) requireThat(Array.isArray(c.relations) &&
        new Set(c.relations).size===c.relations.length &&
        c.relations.every((r: unknown)=>['uses','reviews','supports','challenges','supersedes'].includes(r as string)),'relations');
    }
  }
}
