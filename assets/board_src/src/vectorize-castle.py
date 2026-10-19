"""Rebuild genuine vector paths from the approved pixel-art castle.

Optional rebuild dependency: Pillow + NumPy. No embedded image is used in SVGs.
"""
from pathlib import Path
import json
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
X, Y, W, H, STEP = 232, 140, 792, 424, 4
COLORS = [
    '#123e2e', '#536c5b', '#7b8b78', '#9da98c', '#b9c3a6',
    '#acbda0', '#ce9525', '#dca938', '#477a9d',
]
source = Image.open(ROOT / 'reference/approved-design.png').convert('RGB')
small = source.crop((X, Y, X + W, Y + H)).resize((W // STEP, H // STEP), Image.Resampling.BOX)
rgb = np.asarray(small).astype(np.int32)
mask = rgb.min(axis=2) < 216
palette = np.array([[int(c[i:i+2], 16) for i in (1, 3, 5)] for c in COLORS])
distances = ((rgb[:, :, None, :] - palette[None, None, :, :]) ** 2).sum(axis=-1)
indices = distances.argmin(axis=2)
indices[~mask] = -1
# Suppress isolated quantization speckles without moving the silhouette.
smooth = indices.copy()
for yy in range(1, indices.shape[0]-1):
    for xx in range(1, indices.shape[1]-1):
        if indices[yy, xx] < 0:
            continue
        values, counts = np.unique(indices[yy-1:yy+2, xx-1:xx+2], return_counts=True)
        k = counts.argmax()
        if values[k] >= 0 and counts[k] >= 6:
            smooth[yy, xx] = values[k]
indices = smooth
paths = []
for ci, color in enumerate(COLORS):
    active, rects = {}, []
    for y in range(indices.shape[0]):
        x, runs = 0, set()
        while x < indices.shape[1]:
            if indices[y, x] != ci:
                x += 1
                continue
            start = x
            while x < indices.shape[1] and indices[y, x] == ci:
                x += 1
            runs.add((start, x - start))
        for key in list(active):
            if key not in runs:
                rects.append((*key, *active.pop(key)))
        for key in runs:
            if key in active:
                active[key][1] += 1
            else:
                active[key] = [y, 1]
    for key, (start, height) in active.items():
        rects.append((*key, start, height))
    d = ''.join(f'M{x*STEP} {y*STEP}h{w*STEP}v{h*STEP}h{-w*STEP}z'
                for x, w, y, h in sorted(rects))
    if d:
        paths.append({'fill': color, 'd': d})
data = {'viewBox': [0, 0, W, H], 'placement': [X, Y], 'grid': STEP, 'paths': paths}
(ROOT / 'src/castle.json').write_text(json.dumps(data, indent=2) + '\n')
(ROOT / 'src/castle-data.mjs').write_text('export const CASTLE = ' + json.dumps(data, separators=(',', ':')) + ';\n')
print(f'Castle: {len(paths)} color layers, {sum(len(p["d"]) for p in paths):,} path characters')

# Trace the approved lettering into compact, editable paths. These are shapes,
# not a raster image or a dependency on a third-party font.
regions = {
    'digit2': (145,725,215,800), 'digit3': (595,725,665,800), 'digit4': (815,725,885,800),
    'roman1': (160,618,202,660), 'roman2': (378,618,432,660),
    'roman3': (590,618,665,660), 'roman4': (812,618,888,660), 'roman5': (1052,618,1092,660),
    'mission-prefix': (550,890,675,921), 'fails': (792,862,915,895),
    'rejected': (450,1002,804,1040), 'counter2': (578,1116,677,1160),
}
details={}
for name,(x0,y0,x1,y1) in regions.items():
    a=np.asarray(source.crop((x0,y0,x1,y1)))
    m=(a[:,:,0]<90)&(a[:,:,1]<135)&(a[:,:,2]<110)
    if name=='fails':
        m=a.astype(np.float32).mean(axis=2)<180
    ys,xs=np.where(m); m=m[ys.min():ys.max()+1,xs.min():xs.max()+1]
    active,rects={},[]
    for yy,row in enumerate(m):
        xx=0;runs=set()
        while xx<len(row):
            if not row[xx]:xx+=1;continue
            start=xx
            while xx<len(row) and row[xx]:xx+=1
            runs.add((start,xx-start))
        for key in list(active):
            if key not in runs:rects.append((*key,*active.pop(key)))
        for key in runs:
            if key in active:active[key][1]+=1
            else:active[key]=[yy,1]
    for key,value in active.items():rects.append((*key,*value))
    d=''.join(f'M{x} {y}h{w}v{h}h{-w}z' for x,w,y,h in sorted(rects))
    details[name]={'width':m.shape[1],'height':m.shape[0],'d':d}
(ROOT/'src/detail-data.mjs').write_text('export const DETAILS = '+json.dumps(details,separators=(',',':'))+';\n')
