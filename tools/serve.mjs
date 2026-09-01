// Zero-dependency static server for local review: node tools/serve.mjs
// Serves the project root so /site/index.html can fetch /output/*.json.
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { join, extname, resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const port = 8031;
const TYPES = { '.html': 'text/html', '.json': 'application/json',
                '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml' };

createServer(async (req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0]);
  const file = resolve(join(root, url === '/' ? '/site/index.html' : url));
  // Path-traversal guard: a resolved path outside root is never served.
  if (!file.startsWith(root)) { res.writeHead(403).end('forbidden'); return; }
  try {
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch {
    res.writeHead(404).end('not found');
  }
}).listen(port, () => console.log(`fedstruct → http://localhost:${port}/`));
