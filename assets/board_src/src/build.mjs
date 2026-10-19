import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import {renderBoard,renderCastle,renderBackground,renderMission,renderRejectionTrack,renderIcon,DEMO_STATE,PALETTE} from './avalon-assets.mjs';

const ROOT=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const assets=[];
async function writeSvg(name,svg) {
 const target=path.join(ROOT,'svg',name+'.svg');
 await fs.mkdir(path.dirname(target),{recursive:true});await fs.writeFile(target,svg+'\n');
 assets.push({name,path:'svg/'+name+'.svg',width:Number(svg.match(/width="([\d.]+)"/)[1]),height:Number(svg.match(/height="([\d.]+)"/)[1])});
}
await writeSvg('board',renderBoard());
await writeSvg('board-blank',renderBoard({currentMission:null,missionResults:[null,null,null,null,null],rejectedProposals:0}));
await writeSvg('board-background',renderBackground());
await writeSvg('board-transparent',renderBoard({transparent:true}));
await writeSvg('castle',renderCastle());
for(let i=0;i<5;i++) await writeSvg(`mission-${i+1}`,renderMission({number:i+1,teamSize:DEMO_STATE.teamSizes[i],result:DEMO_STATE.missionResults[i],current:i===2,requiredFails:DEMO_STATE.requiredFails[i]}));
await writeSvg('mission-empty',renderMission());
await writeSvg('mission-current',renderMission({number:3,teamSize:3,current:true}));
for(let count=0;count<=5;count++)await writeSvg(`rejections-${count}`,renderRejectionTrack({count}));
for(const name of ['success','fail','current-ring','current-pointer','proposal-filled','proposal-empty'])await writeSvg('icons/'+name,renderIcon(name));

const spriteNames=['castle','mission-empty','mission-current','rejections-0','icons/success','icons/fail','icons/current-ring','icons/current-pointer','icons/proposal-filled','icons/proposal-empty'];
let symbols='';
for(const name of spriteNames) {
 const svg=await fs.readFile(path.join(ROOT,'svg',name+'.svg'),'utf8');
 const viewBox=svg.match(/viewBox="([^"]+)"/)[1];
 const content=svg.slice(svg.indexOf('>')+1,svg.lastIndexOf('</svg>')).replace(/\sid="([^"]+)"/g,(_,id)=>` id="symbol-${name.replace('icons/','')}-${id}"`);
 symbols+=`<symbol id="${name.replace('icons/','')}" viewBox="${viewBox}">${content}</symbol>`;
}
await fs.writeFile(path.join(ROOT,'svg/sprite.svg'),`<svg xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">${symbols}</svg>\n`);
await fs.writeFile(path.join(ROOT,'palette.json'),JSON.stringify(PALETTE,null,2)+'\n');
await fs.writeFile(path.join(ROOT,'layout.json'),JSON.stringify({
 canvas:{width:1254,height:1254},castle:{x:232,y:140,width:792,height:424},
 missionRow:{y:624,firstX:78,spacing:222,componentViewBox:[0,0,208,304],circleCenter:[104,140],circleRadius:90},
 rejectionTrack:{x:407,y:1008,width:440,height:152},demoState:DEMO_STATE,
},null,2)+'\n');
await fs.writeFile(path.join(ROOT,'assets.json'),JSON.stringify({version:'1.0.0',assets},null,2)+'\n');
await fs.writeFile(path.join(ROOT,'tokens.css'),`:root {\n${Object.entries(PALETTE).map(([k,v])=>`  --avalon-${k.replace(/[A-Z]/g,c=>'-'+c.toLowerCase())}: ${v};`).join('\n')}\n}\n.avalon-board { width: 100%; height: auto; display: block; }\n.avalon-pixel { image-rendering: pixelated; }\n`);

// A fully offline preview: no CDN, network request, bundler, or local server.
const data=(await fs.readFile(path.join(ROOT,'src/castle-data.mjs'),'utf8')).replace('export const','const');
const details=(await fs.readFile(path.join(ROOT,'src/detail-data.mjs'),'utf8')).replace('export const','const');
const module=(await fs.readFile(path.join(ROOT,'src/avalon-assets.mjs'),'utf8')).replace(/^import .*;\n/gm,'').replaceAll('export const','const').replaceAll('export function','function');
const preview=`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Avalon asset preview</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f2f0e7;color:#173e32;font:15px system-ui,sans-serif}main{max-width:1150px;margin:0 auto;padding:28px;display:grid;grid-template-columns:minmax(0,1fr) 260px;gap:28px}.board{background:#fcf8ea;align-self:start}.board svg{display:block;width:100%;height:auto}aside{padding:16px 0}h1{font-size:24px;margin:0 0 8px}p{line-height:1.5;color:#5b6b5c;margin-bottom:26px}label{display:block;font-weight:600;font-size:13px;margin:20px 0 7px}select,button{width:100%;background:#fcf8ea;border:1px solid #bec7b4;border-radius:5px;padding:10px;color:#173e32;font:inherit}button{cursor:pointer;margin-top:12px}button.primary{background:#173e32;color:#fcf8ea}fieldset{padding:0;border:0;margin:20px 0}legend{font-weight:600;font-size:13px}.results{display:grid;grid-template-columns:22px 1fr;align-items:center;gap:8px;margin-top:10px}.results select{padding:7px;font-size:13px}.note{font-size:12px;color:#71806e;margin-top:18px}a{color:inherit}@media(max-width:760px){main{grid-template-columns:1fr;padding:16px;gap:10px}aside{padding:0 8px}} 
</style></head><body><main><div class="board" id="board"></div><aside><h1>Avalon assets</h1><p>The approved minimalist board, rebuilt as editable vector shapes.</p>
<label for="mission">Current mission</label><select id="mission"><option value="0">None</option><option value="1">1</option><option value="2">2</option><option value="3" selected>3</option><option value="4">4</option><option value="5">5</option></select>
<label for="rejections">Rejected proposals</label><select id="rejections"><option>0</option><option>1</option><option selected>2</option><option>3</option><option>4</option><option>5</option></select>
<fieldset><legend>Mission results</legend><div class="results">${[1,2,3,4,5].map(n=>`<label for="result-${n}" style="margin:0">${n}</label><select id="result-${n}"><option value="">Pending</option><option value="success"${n===2?' selected':''}>Success</option><option value="fail"${n===1?' selected':''}>Fail</option></select>`).join('')}</div></fieldset>
<button class="primary" id="download">Download current SVG</button><button id="reset">Reset to approved state</button><p class="note">Open this file directly. It works offline. Asset paths and integration examples are in README.md.</p></aside></main>
<script type="module">${data}\n${details}\n${module}\n
const board=document.querySelector('#board');
function state(){return {currentMission:Number(document.querySelector('#mission').value)||null,rejectedProposals:Number(document.querySelector('#rejections').value),missionResults:[1,2,3,4,5].map(n=>document.querySelector('#result-'+n).value||null)}}
function draw(){board.innerHTML=renderBoard(state())}
document.querySelectorAll('select').forEach(el=>el.addEventListener('change',draw));
document.querySelector('#reset').addEventListener('click',()=>{document.querySelector('#mission').value='3';document.querySelector('#rejections').value='2';[1,2,3,4,5].forEach(n=>document.querySelector('#result-'+n).value=DEMO_STATE.missionResults[n-1]||'');draw()});
document.querySelector('#download').addEventListener('click',()=>{const url=URL.createObjectURL(new Blob([renderBoard(state())],{type:'image/svg+xml'}));const a=document.createElement('a');a.href=url;a.download='avalon-board.svg';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)});draw();
</script></body></html>`;
await fs.writeFile(path.join(ROOT,'preview.html'),preview);
console.log(`Built ${assets.length} standalone SVG assets, sprite sheet, and offline preview.`);

// PNG export is optional for a developer rebuilding the assets.
if(process.argv.includes('--png')) {
 let sharp; const require=createRequire(import.meta.url);
 try {sharp=require('sharp');}
 catch {if(!process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES)throw new Error('Install sharp to rebuild PNGs: npm install sharp');sharp=require(path.join(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES,'sharp'));}
 for(const asset of assets) {
   const dst=path.join(ROOT,'png',asset.name+'.png');await fs.mkdir(path.dirname(dst),{recursive:true});
   await sharp(path.join(ROOT,asset.path)).png().toFile(dst);
 }
 await sharp(path.join(ROOT,'svg/board.svg')).resize(2508,2508,{kernel:'nearest'}).png().toFile(path.join(ROOT,'png/board@2x.png'));
 console.log('Built PNG exports, including a 2× board.');
}
