// Builds the public site into _site/ with its JavaScript obfuscated, so the
// deployed code is miserable to read/copy while the source in the repo stays
// readable. Run with `npm run build`. The GitHub Pages workflow runs this and
// deploys _site/.
//
// Design choices that keep the site WORKING:
//  - renameGlobals:false — top-level function names are kept. index.html has
//    an inline handler (onload="adHostLoaded(this)") and any such HTML-attribute
//    handler calls a global by name, so those names must not change. Only the
//    insides of functions (local variables) and string literals get scrambled.
//  - no controlFlowFlattening / deadCodeInjection / base64 string encoding —
//    those balloon size and slow execution, which matters on mobile. We keep a
//    light-but-effective pass: mangled identifiers, compacting, a shuffled
//    string array.
//  - every obfuscated block has "</script" neutralised so a string literal
//    can never break the inline <script> when embedded back into the HTML.

import { readFileSync, writeFileSync, mkdirSync, rmSync, cpSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';
import JavaScriptObfuscator from 'javascript-obfuscator';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const out = join(root, '_site');

const OPTS = {
  compact: true,
  simplify: true,
  renameGlobals: false,
  identifierNamesGenerator: 'mangled-shuffled',
  stringArray: true,
  stringArrayThreshold: 0.75,
  stringArrayRotate: true,
  stringArrayShuffle: true,
  // Medium: strings are base64-encoded (hidden, not just moved to an array),
  // and selfDefending makes the code break if someone runs it through a
  // "beautifier" to un-minify it. Still no control-flow flattening or
  // dead-code injection — those are what actually slow a site down, and the
  // base64 bloat compresses away over gzip, so this stays mobile-fast.
  stringArrayEncoding: ['base64'],
  splitStrings: false,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  numbersToExpressions: false,
  transformObjectKeys: false,
  unicodeEscapeSequence: false,
  selfDefending: true,
};

function obf(code) {
  return JavaScriptObfuscator.obfuscate(code, OPTS)
    .getObfuscatedCode()
    // make it safe to sit inside an inline <script> in HTML
    .replace(/<\/script/gi, '<\\/script');
}

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });

// index.html — obfuscate each INLINE <script> (ones without a src), leave the
// HTML and any external <script src=…> untouched.
let html = readFileSync(join(root, 'index.html'), 'utf8');
let n = 0;
html = html.replace(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi, (m, attrs, body) => {
  if (/\bsrc\s*=/.test(attrs)) return m;
  if (!body.trim()) return m;
  n += 1;
  return `<script${attrs}>${obf(body)}</script>`;
});
writeFileSync(join(out, 'index.html'), html);
console.log(`index.html: obfuscated ${n} inline script block(s)`);

// ocr-worker.js — a standalone web worker that does the on-phone OCR
writeFileSync(join(out, 'ocr-worker.js'), obf(readFileSync(join(root, 'ocr-worker.js'), 'utf8')));
console.log('ocr-worker.js: obfuscated');

// Copy the rest verbatim. sw.js is tiny push-notification boilerplate — not
// worth the risk of obfuscating a service worker.
for (const f of ['sw.js', 'manifest.json']) cpSync(join(root, f), join(out, f));
cpSync(join(root, 'icons'), join(out, 'icons'), { recursive: true });
writeFileSync(join(out, '.nojekyll'), '');
console.log('copied: sw.js, manifest.json, icons/, .nojekyll');
console.log('done -> _site/');
