import { CASTLE } from './castle-data.mjs';
import { DETAILS } from './detail-data.mjs';

export const PALETTE = Object.freeze({
  background: '#FCF8EA', ink: '#0B3C30', sage: '#B3C1A6',
  sageOutline: '#ADBA9E', gold: '#D29A25', blue: '#477A9D',
  red: '#B94F40', white: '#FCF8EA', muted: '#64765E',
});

// Original compact bitmap glyphs: each filled cell becomes a vector rectangle.
// No font download, font licensing dependency, or live text reflow.
const FONT = {
 '0':['01110','11011','11011','11011','11011','11011','01110'],
 '1':['00100','01100','00100','00100','00100','00100','01110'],
 '2':['01110','11011','00011','00110','01100','11000','11111'],
 '3':['11110','00011','00011','01110','00011','00011','11110'],
 '4':['00110','01110','11010','11010','11111','00010','00010'],
 '5':['11111','11000','11000','11110','00011','00011','11110'],
 '6':['01110','11000','11000','11110','11011','11011','01110'],
 '7':['11111','00011','00110','00110','01100','01100','01100'],
 '8':['01110','11011','11011','01110','11011','11011','01110'],
 '9':['01110','11011','11011','01111','00011','00011','01110'],
 A:['01110','11011','11011','11111','11011','11011','11011'],
 C:['01111','11000','11000','11000','11000','11000','01111'],
 D:['11110','11011','11011','11011','11011','11011','11110'],
 E:['11111','11000','11000','11110','11000','11000','11111'],
 F:['11111','11000','11000','11110','11000','11000','11000'],
 I:['111','010','010','010','010','010','111'],
 J:['00111','00011','00011','00011','00011','11011','01110'],
 L:['11000','11000','11000','11000','11000','11000','11111'],
 M:['11011','11111','11111','11011','11011','11011','11011'],
 N:['11001','11101','11101','11011','11011','11011','11001'],
 O:['01110','11011','11011','11011','11011','11011','01110'],
 P:['11110','11011','11011','11110','11000','11000','11000'],
 R:['11110','11011','11011','11110','11100','11010','11011'],
 S:['01111','11000','11000','01110','00011','00011','11110'],
 T:['11111','00100','00100','00100','00100','00100','00100'],
 U:['11011','11011','11011','11011','11011','11011','01110'],
 V:['11011','11011','11011','11011','11011','01110','00100'],
 a:['00000','00000','01110','00011','01111','11011','01111'],
 f:['00110','01100','01100','11110','01100','01100','01100'],
 i:['010','000','110','010','010','010','111'],
 l:['110','010','010','010','010','010','111'],
 s:['00000','00000','01111','11000','01110','00011','11110'],
 '/':['00001','00010','00010','00100','01000','01000','10000'],
 ' ':['000','000','000','000','000','000','000'],
};
// Bold 7 x 9 digits for the team size inside each mission circle.
const BIG = {
 '1':['0001100','0011100','0111100','0001100','0001100','0001100','0001100','0001100','0111111'],
 '2':['0111110','1100011','0000011','0000110','0001100','0011000','0110000','1100000','1111111'],
 '3':['0111110','1100011','0000011','0000011','0011110','0000011','0000011','1100011','0111110'],
 '4':['0000110','0001110','0011110','0110110','1100110','1111111','0000110','0000110','0000110'],
 '5':['1111111','1100000','1100000','1111110','0000011','0000011','0000011','1100011','0111110'],
 '6':['0011110','0110000','1100000','1111110','1100011','1100011','1100011','1100011','0111110'],
 '7':['1111111','0000011','0000110','0001100','0001100','0011000','0011000','0011000','0011000'],
 '8':['0111110','1100011','1100011','1100011','0111110','1100011','1100011','1100011','0111110'],
 '9':['0111110','1100011','1100011','1100011','0111111','0000011','0000011','0000110','0111100'],
};
// Serif Roman numerals for the mission headings.
const ROMAN = {
 I:['1111','0110','0110','0110','0110','0110','1111'],
 V:['1100011','1100011','0110110','0110110','0110110','0011100','0001000'],
};
const ESC = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&apos;'}[c]));
const theme = p => ({...PALETTE, ...p});

function documentSvg(body, width, height, title, description = '') {
 return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img" aria-label="${ESC(title)}" shape-rendering="crispEdges"><title>${ESC(title)}</title>${description ? `<desc>${ESC(description)}</desc>` : ''}${body}</svg>`;
}
function textPixels(value, x, y, unit, color, anchor = 'middle', font = FONT) {
 const chars = [...String(value)];
 const widths = chars.map(ch => {
   if (!font[ch]) throw new TypeError(`Missing bitmap glyph: ${ch}`);
   return font[ch][0].length;
 });
 const width = (widths.reduce((a,b) => a+b, 0) + Math.max(0,chars.length-1)) * unit;
 let left = x - (anchor === 'middle' ? width/2 : 0), d = '';
 for (let i=0;i<chars.length;i++) {
   font[chars[i]].forEach((row, ry) => {
     let rx = 0;
     while(rx < row.length) {
       if (row[rx] !== '1') { rx++; continue; }
       const start = rx;
       while (row[rx] === '1') rx++;
       d += `M${left+start*unit} ${y+ry*unit}h${(rx-start)*unit}v${unit}h${-(rx-start)*unit}z`;
     }
   });
   left += (widths[i]+1)*unit;
 }
 // Small text lands on fractional screen pixels when the board is scaled down; antialias it so every
 // stroke keeps the same weight instead of snapping to uneven widths.
 const smooth = unit < 4 ? ' shape-rendering="geometricPrecision"' : '';
 return `<g aria-label="${ESC(value)}"${smooth}><title>${ESC(value)}</title><path fill="${color}" d="${d}"/></g>`;
}

function traced(name, x, y, color, scale=1) {
 const v=DETAILS[name];
 return `<path fill="${color}" transform="translate(${x-v.width*scale/2} ${y}) scale(${scale})" d="${v.d}"/>`;
}
function currentLabel(number,x,y,color) {
 return textPixels(`MISSION ${number}`,x,y,3,color);
}

// Pixel-circle shape uses an even grid; all edges remain horizontal or vertical.
function disk(cx, cy, radius, fill, step = 6) {
 const bands = Math.round(radius*2/step), top = cy - bands*step/2;
 let d='';
 for(let row=0;row<bands;row++) {
   const dy=(row+.5)*step-bands*step/2;
   const half=Math.max(step/2, Math.round(Math.sqrt(Math.max(0,radius*radius-dy*dy))/step)*step);
   d += `M${cx-half} ${top+row*step}h${half*2}v${step}h${-half*2}z`;
 }
 return `<path fill="${fill}" d="${d}"/>`;
}
function ring(cx, cy, r, outer, inner, step=3, thick=2*step) {
 return disk(cx,cy,r,outer,step)+disk(cx,cy,r-thick,inner,step);
}
function pointer(cx, y, color) {
 return `<path fill="${color}" d="M${cx-18} ${y+18}h36v6h-36zM${cx-12} ${y+12}h24v6h-24zM${cx-6} ${y+6}h12v6h-12zM${cx-3} ${y}h6v6h-6z"/>`;
}
function badge(kind, cx, cy, p) {
 const bg = kind === 'success' ? p.blue : p.red;
 const glyph = kind === 'success'
   ? ['00000001','00000011','00000110','11001100','01111000','00110000']
   : ['1100011','0110110','0011100','0001000','0011100','0110110','1100011'];
 let d='';
 const s=4, x=cx-glyph[0].length*s/2, y=cy-glyph.length*s/2;
 glyph.forEach((row,ry)=>[...row].forEach((v,rx)=>{if(v==='1')d+=`M${x+rx*s} ${y+ry*s}h${s}v${s}h-${s}z`;}));
 return disk(cx,cy,32,bg,2)+`<path fill="${p.white}" d="${d}"/>`;
}
function castleBody(x=0,y=0) {
 return `<g id="castle" transform="translate(${x} ${y})" aria-label="Pixel castle">`+CASTLE.paths.map((p,i)=>`<g id="castle-color-${i}" fill="${p.fill}"><path d="${p.d}"/></g>`).join('')+'</g>';
}
function missionBody({number=1,teamSize=2,result=null,current=false,requiredFails=1,showDetails=true}, p) {
 const roman=['I','II','III','IV','V'][number-1];
 let s=`<g data-mission="${number}" aria-label="Mission ${number}, ${teamSize} agents${current?', current':''}${result?', '+result:''}">`;
 s+=textPixels(roman,104,2,4,p.ink,'middle',ROMAN);
 s+=ring(104,140,90,current?p.gold:p.ink,p.sage);
 s+=textPixels(teamSize,104,113,6,p.ink,'middle',BIG);
 if(result)s+=badge(result,current?48:104,current?214:222,p);
 if(current) s+=pointer(104,238,p.gold);
 if(showDetails && current)s+=currentLabel(number,104,269,p.ink);
 if(showDetails && requiredFails>1)s+=textPixels(`${requiredFails} fails`,104,current?306:result?262:244,3,p.muted);   // below the badge
 return s+'</g>';
}
function rejectionBody({count=0}, p) {
 let s='<g id="rejection-tracker" aria-label="'+count+' of 5 rejected proposals">';
 s+=textPixels('REJECTED PROPOSALS',220,0,3,p.ink);
 for(let i=0;i<5;i++) {
   const cx=76+i*72,cy=70;
   s+=`<g id="proposal-${i+1}" data-filled="${i<count}">`;
   s+=i<count?disk(cx,cy,22,p.red,2):ring(cx,cy,22,p.sageOutline,p.background,2,4);
   s+='</g>';
 }
 return s+textPixels(`${count} / 5`,220,120,3,p.ink)+'</g>';
}

export const DEMO_STATE = Object.freeze({
 currentMission:3, teamSizes:[2,3,3,4,4],
 missionResults:['fail','success',null,null,null],
 requiredFails:[1,1,1,2,1], rejectedProposals:2,
});
function validate(state) {
 if(state.currentMission!==null && (!Number.isInteger(state.currentMission)||state.currentMission<1||state.currentMission>5))throw new RangeError('currentMission must be null or 1–5');
 if(!Number.isInteger(state.rejectedProposals)||state.rejectedProposals<0||state.rejectedProposals>5)throw new RangeError('rejectedProposals must be 0–5');
 for(const key of ['teamSizes','missionResults','requiredFails'])if(!Array.isArray(state[key])||state[key].length!==5)throw new RangeError(`${key} must contain exactly five entries`);
 if(state.teamSizes.some(n=>!Number.isInteger(n)||n<1||n>9))throw new RangeError('teamSizes entries must be integers from 1 to 9');
 if(state.requiredFails.some(n=>!Number.isInteger(n)||n<1||n>2))throw new RangeError('requiredFails entries must be 1 or 2');
 if(state.missionResults.some(r=>![null,'success','fail'].includes(r)))throw new TypeError('missionResults entries must be null, success, or fail');
}
export function renderBoard(options={}) {
 const s={...DEMO_STATE,...options},p=theme(s.palette); validate(s);
 let body=s.transparent?'':`<rect id="background" width="1254" height="1254" fill="${p.background}"/>`;
 body+=castleBody(232,140);
 for(let i=0;i<5;i++)body+=`<g id="mission-${i+1}" transform="translate(${78+i*222} 624)">`+missionBody({number:i+1,teamSize:s.teamSizes[i],result:s.missionResults[i],current:s.currentMission===i+1,requiredFails:s.requiredFails[i]},p)+'</g>';
 body+=`<g transform="translate(407 1008)">`+rejectionBody({count:s.rejectedProposals},p)+'</g>';
 return documentSvg(body,1254,1254,'Avalon mission board',`Five mission slots. ${s.currentMission===null?'No active mission.':'Mission '+s.currentMission+' active.'} ${s.rejectedProposals} of 5 proposals rejected. Team sizes: ${s.teamSizes.join(', ')}.`);
}
export function renderCastle() { return documentSvg(castleBody(),792,424,'Pixel castle'); }
export function renderBackground({transparent=false,palette}={}) {
 const p=theme(palette);
 return documentSvg((transparent?'':`<rect width="1254" height="1254" fill="${p.background}"/>`)+castleBody(232,140),1254,1254,'Castle board background');
}
export function renderMission(options={}) {
 const s={number:1,teamSize:2,result:null,current:false,requiredFails:1,showDetails:true,...options};
 if(!Number.isInteger(s.number)||s.number<1||s.number>5)throw new RangeError('number must be 1–5');
 validate({...DEMO_STATE,teamSizes:[s.teamSize,2,2,2,2],missionResults:[s.result,null,null,null,null],requiredFails:[s.requiredFails,1,1,1,1]});
 return documentSvg(missionBody(s,theme(s.palette)),208,s.current&&s.requiredFails>1?336:304,`Mission ${s.number}`);
}
export function renderRejectionTrack({count=0,palette}={}) {
 validate({...DEMO_STATE,rejectedProposals:count});
 return documentSvg(rejectionBody({count},theme(palette)),440,152,`${count} of 5 proposals rejected`);
}
export function renderIcon(name,{palette}={}) {
 const p=theme(palette);let body;
 if(name==='success'||name==='fail')body=badge(name,36,36,p);
 else if(name==='proposal-filled')body=disk(36,36,22,p.red,2);
 else if(name==='proposal-empty')body=ring(36,36,22,p.sageOutline,p.background,2,4);
 else if(name==='current-pointer')body=pointer(36,22,p.gold);
 else if(name==='current-ring') {
   // Even-odd clip retains genuine transparency in the center of this overlay.
   const outer=disk(96,96,90,p.gold,3).match(/d="([^"]+)"/)[1];
   const inner=disk(96,96,84,p.gold,3).match(/d="([^"]+)"/)[1];
   body=`<path fill="${p.gold}" fill-rule="evenodd" d="${outer}${inner}"/>`;
   return documentSvg(body,192,192,'Current mission ring');
 } else throw new TypeError(`Unknown icon: ${name}`);
 return documentSvg(body,72,72,name.replaceAll('-',' '));
}
