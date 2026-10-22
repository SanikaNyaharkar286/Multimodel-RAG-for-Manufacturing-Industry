import {spawn} from 'node:child_process';
import {writeFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const qa=path.dirname(fileURLToPath(import.meta.url)), port=9328;
const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const browser=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe', ['--headless','--disable-gpu','--no-first-run','--no-default-browser-check',`--remote-debugging-port=${port}`,`--user-data-dir=${qa}/chrome-responsive-preview`],{windowsHide:true,stdio:'ignore'});
let version;
for(let attempt=0;attempt<60;attempt++) {try {version=await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();break;} catch {await pause(100);} }
if(!version) throw new Error('Preview browser did not start');
const page=await (await fetch(`http://127.0.0.1:${port}/json/new?about:blank`,{method:'PUT'})).json();
const ws=new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});
let id=0;const waiting=new Map();
ws.addEventListener('message',event=>{const result=JSON.parse(event.data);const callback=waiting.get(result.id);if(callback){waiting.delete(result.id);result.error?callback.reject(result.error):callback.resolve(result.result);}});
const call=(method,params={})=>new Promise((resolve,reject)=>{waiting.set(++id,{resolve,reject});ws.send(JSON.stringify({id,method,params}));});
try {
 for(const [name,view,width,height] of [['welcome-laptop','welcome',1280,620],['welcome-phone','welcome',390,844],['admin-page','admin',1280,900],['admin-phone','admin',390,844]]){
  await call('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:width<600});
  await call('Page.navigate',{url:`http://127.0.0.1:5181/__ui_preview?view=${view}`});
  let result;
  for(let attempt=0;attempt<70;attempt++){
   const value=await call('Runtime.evaluate',{expression:"document.querySelector('#qa-results')?.textContent",returnByValue:true});
   if(value.result.value){result=JSON.parse(value.result.value);break;} await pause(100);
  }
  if(!result) throw new Error(`${name} did not render`);
  if(view==='admin'){
   const value=await call('Runtime.evaluate',{expression:`(async()=>{const buttons=[...document.querySelectorAll('.admin-filters button')];buttons[1].click();await new Promise(r=>setTimeout(r,50));return {separatePage:!!document.querySelector('.admin-page'),overlay:!!document.querySelector('.admin-overlay'),allRequests:document.querySelectorAll('.admin-request').length};})()`,awaitPromise:true,returnByValue:true});
   Object.assign(result,value.result.value);
  }
  const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
  await writeFile(path.join(qa,`${name}.png`),Buffer.from(shot.data,'base64'));
  console.log(name,JSON.stringify(result));
  if(result.overflow || (view==='welcome' && result.verticalOverflow)) throw new Error(`${name} overflow`);
 }
}finally{await call('Browser.close').catch(()=>{});ws.close();browser.unref();}
