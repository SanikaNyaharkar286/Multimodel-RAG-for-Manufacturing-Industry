import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '../../..');
const {createServer} = await import(pathToFileURL(path.join(repo, 'frontend/node_modules/vite/dist/node/index.js')));
const server = await createServer({root:path.join(repo,'frontend'), server:{host:'127.0.0.1',port:5181,strictPort:true,fs:{allow:[repo]}},
  plugins:[{name:'isolated-ui-preview',configureServer(server) { server.middlewares.use(async (req,res,next) => {
    if (!req.url.startsWith('/__ui_preview')) return next();
    const html=await server.transformIndexHtml(req.url,`<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"></head><body><div id="root"></div><script type="module" src="/@fs/${here.replaceAll('\\','/')}/ui-preview.jsx"></script></body></html>`);
    res.setHeader('Content-Type','text/html');res.end(html);
  });}}]});
await server.listen(); console.log('Isolated UI preview on 5181');
