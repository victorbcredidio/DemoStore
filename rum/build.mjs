// Builds the browser observer into one IIFE bundle plus its dependency licences and a manifest.
// Output directory: $RUM_OUT_DIR, or ./dist next to this file. The Docker build copies it into the app.
import {build} from 'esbuild';
import {mkdir, readFile, writeFile, readdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const target = process.env.RUM_OUT_DIR
  ? pathToFileURL(resolve(process.env.RUM_OUT_DIR) + '/')
  : new URL('./dist/', import.meta.url);
await mkdir(target, {recursive:true});
const result = await build({entryPoints:[fileURLToPath(new URL('./rum.mjs', import.meta.url))],
  bundle:true, write:false, platform:'browser', format:'iife', target:['es2020'], minify:true,
  legalComments:'eof', metafile:true});
const bytes = result.outputFiles.find(f => f.path.endsWith('.js') || f.path === '<stdout>')?.contents;
if (!bytes) throw new Error('No browser bundle produced');
await writeFile(new URL('rum-v1.js', target), bytes);
// Preserve the exact dependency licences, including transitive packages.
const licenses = [];
async function visit(dir) {
  for (const item of await readdir(dir, {withFileTypes:true})) {
    if (!item.isDirectory() || item.name === '.bin') continue;
    const path = new URL(item.name+'/', dir);
    if (item.name.startsWith('@')) {await visit(path); continue;}
    try {
      const pkg = JSON.parse(await readFile(new URL('package.json', path), 'utf8'));
      for (const name of ['LICENSE', 'LICENSE.md', 'LICENSE.txt']) {
        try { licenses.push(`\n=== ${pkg.name}@${pkg.version} ===\n`+await readFile(new URL(name,path),'utf8')); break; } catch {}
      }
    } catch {}
    try {await visit(new URL('node_modules/',path));} catch {}
  }
}
await visit(new URL('node_modules/', import.meta.url));
await writeFile(new URL('LICENSES.txt', target), licenses.join('\n'));
const lock = await readFile(new URL('package-lock.json', import.meta.url));
const pkg = JSON.parse(await readFile(new URL('package.json', import.meta.url), 'utf8'));
const manifest = {version:pkg.version, bundle:'rum-v1.js', bytes:bytes.length,
  sha256:createHash('sha256').update(bytes).digest('hex'),
  lock_sha256:createHash('sha256').update(lock).digest('hex'),
  packages:JSON.parse(lock).packages[''].dependencies};
await writeFile(new URL('manifest.json',target), JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify(manifest));
