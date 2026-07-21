import {createServer} from 'node:http';
import {readFile, unlink} from 'node:fs/promises';
import {join} from 'node:path';
import {randomUUID} from 'node:crypto';
import {bundle} from '@remotion/bundler';
import {renderMedia, selectComposition} from '@remotion/renderer';
import {validateRenderRequest} from './validation.mjs';

const port = Number(process.env.RENDER_PORT ?? 8091);
const serveUrl = await bundle({entryPoint: join(process.cwd(), 'src/index.jsx'), webpackOverride: (config) => config});
let queue = Promise.resolve();

const parseBody = async (request) => {
  let body = '';
  for await (const chunk of request) {
    body += chunk;
    if (body.length > 10_000_000) throw new Error('request exceeds limit');
  }
  return validateRenderRequest(JSON.parse(body));
};

const render = async (props) => {
  const output = `/tmp/${randomUUID()}.mp4`;
  try {
    const composition = await selectComposition({serveUrl, id: 'EvidenceVideo', inputProps: props, logLevel: 'warn'});
    await renderMedia({
      codec: 'h264', composition, serveUrl, outputLocation: output, inputProps: props,
      concurrency: 1, crf: 22, x264Preset: 'veryfast', logLevel: 'warn',
      chromiumOptions: {enableMultiProcessOnLinux: true},
      timeoutInMilliseconds: 120000,
    });
    return await readFile(output);
  } finally {
    await unlink(output).catch(() => undefined);
  }
};

createServer(async (request, response) => {
  if (request.method === 'GET' && request.url === '/health') {
    response.writeHead(200, {'Content-Type': 'application/json'}).end(JSON.stringify({status: 'ready', engine: 'remotion', version: '4.0.495'}));
    return;
  }
  if (request.method !== 'POST' || request.url !== '/render') {
    response.writeHead(404, {'Content-Type': 'application/json'}).end(JSON.stringify({detail: 'not found'}));
    return;
  }
  try {
    const props = await parseBody(request);
    const task = queue.then(() => render(props));
    queue = task.then(() => undefined, () => undefined);
    const output = await task;
    response.writeHead(200, {'Content-Type': 'video/mp4', 'Content-Length': output.length, 'X-Render-Engine': 'remotion', 'X-Render-Engine-Version': '4.0.495', 'X-Composition': 'EvidenceVideo'}).end(output);
  } catch (error) {
    response.writeHead(422, {'Content-Type': 'application/json'}).end(JSON.stringify({detail: error instanceof Error ? error.message : 'render failed'}));
  }
}).listen(port, '0.0.0.0');
