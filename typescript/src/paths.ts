/** SCI §3.1 file path rules and §3 root-path restrictions. */
import { check, fail } from './errors.ts';

const COMPONENT = /^[A-Za-z0-9._-]+$/;
const DEVICE = /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$/i;

/** Validate one path's syntax under §3.1 (not conflicts). */
export function checkPathSyntax(path: string, jsonPath = `files/${path}`): void {
  check(path.length > 0, 'path.empty', '§3.1', 'Path must not be empty', { path: jsonPath });
  check(path.length <= 255, 'path.too_long', '§3.1', 'Path exceeds 255 ASCII bytes', {
    path: jsonPath,
    observed: path.length,
  });
  for (const component of path.split('/')) {
    check(COMPONENT.test(component), 'path.component', '§3.1', 'Path component must be nonempty portable filename characters', {
      path: jsonPath,
      observed: component,
    });
    check(
      component !== '.' && component !== '..' && !component.endsWith('.'),
      'path.dot',
      '§3.1',
      'Dot components and trailing periods are prohibited',
      {
        path: jsonPath,
        observed: component,
      },
    );
    check(!DEVICE.test(component), 'path.device_name', '§3.1', 'Windows device names are prohibited', {
      path: jsonPath,
      observed: component,
    });
  }
}

/** True if `path` satisfies §3.1 syntax and the §3 root rule. */
export function isValidManifestPath(path: string): boolean {
  try {
    checkPathSyntax(path);
    checkRootRule(path, '');
    return true;
  } catch {
    return false;
  }
}

function checkRootRule(path: string, jsonPath: string): void {
  check(
    path === 'sci.json' || path === 'README.md' || path === 'LICENSE' || path.startsWith('files/'),
    'manifest.path_root',
    '§3',
    'Listed paths other than sci.json, README.md, and LICENSE must be under files/',
    { path: jsonPath, observed: path },
  );
}

/** Validate a full set of manifest paths: syntax, root rule, and case-insensitive conflicts. */
export function checkManifestPaths(paths: Iterable<string>): void {
  const files = new Set<string>();
  const directories = new Map<string, string>();
  for (const path of paths) {
    const jsonPath = `files/${path}`;
    checkPathSyntax(path, jsonPath);
    checkRootRule(path, jsonPath);
    const folded = path.toLowerCase();
    if (files.has(folded)) fail('path.case_conflict', '§3.1', 'Paths conflict without regard to ASCII case', { path: jsonPath });
    files.add(folded);
    const parts = path.split('/');
    for (let n = 1; n < parts.length; n++) {
      const dir = parts.slice(0, n).join('/');
      const lower = dir.toLowerCase();
      const seen = directories.get(lower);
      if (seen !== undefined && seen !== dir) {
        fail('path.directory_case', '§3.1', 'Directory path capitalization differs between entries', {
          path: jsonPath,
          observed: dir,
        });
      }
      directories.set(lower, dir);
    }
  }
  for (const f of files) {
    if (directories.has(f)) fail('path.file_directory_conflict', '§3.1', 'A file path also names a directory', { observed: f });
  }
}
