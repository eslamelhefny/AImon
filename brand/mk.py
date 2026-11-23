import math, re, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))  # writes logo files next to this script
# needs BarlowCondensed-SemiBold.ttf here (OFL, github.com/google/fonts ofl/barlowcondensed)
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
cx, cy, r = 32, 36, 19
start, end, n, gap, frac_n = 215, -35, 7, 11, 0.62
lit, dim, warn, hot = "#a8f0d8", "#2e5a56", "#e3a33a", "#e0543f"
step = (start-end)/n; segs=[]
for i in range(n):
    a0 = math.radians(start-i*step-gap/2); a1 = math.radians(start-(i+1)*step+gap/2)
    p = lambda a:(cx+r*math.cos(a), cy-r*math.sin(a))
    (x0,y0),(x1,y1) = p(a0),p(a1)
    f=(i+0.5)/n
    col = lit if f<=frac_n else hot if i==n-1 else warn if i==n-2 else dim
    segs.append(f'<path d="M{x0:.2f} {y0:.2f}A{r} {r} 0 0 1 {x1:.2f} {y1:.2f}" stroke="{col}"/>')
na = math.radians(start-frac_n*(start-end)); nx,ny = cx+(r-6)*math.cos(na), cy-(r-6)*math.sin(na)
inner = f'''<rect width="64" height="64" rx="15" fill="#12302f"/>
<g fill="none" stroke-width="6.4">{"".join(segs)}</g>
<path d="M{cx} {cy}L{nx:.2f} {ny:.2f}" stroke="#e6fff6" stroke-width="3.6" stroke-linecap="round"/>
<circle cx="{cx}" cy="{cy}" r="4.6" fill="#e6fff6"/><circle cx="{cx}" cy="{cy}" r="1.8" fill="#12302f"/>
<rect x="22" y="48.5" width="20" height="4" rx="1" fill="#a8f0d8" opacity=".4"/>'''
open("mark.svg","w").write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64" role="img" aria-label="AI Meter">\n{inner}\n</svg>\n')
f = TTFont("BarlowCondensed-SemiBold.ttf"); gs=f.getGlyphSet(); cmap=f.getBestCmap()
upm=f["head"].unitsPerEm; capH=f["OS/2"].sCapHeight; size=46; s=size/upm; base=32+capH*s/2
x=0; cmds=[]
track={"A":3.2,"I":2.6,"M":1.2}
for ch in "AI Meter":
    g=cmap[ord(ch)]; pen=SVGPathPen(gs); gs[g].draw(TransformPen(pen,(s,0,0,-s,x,base))); cmds.append(pen.getCommands())
    x+=gs[g].width*s+track.get(ch,1.0)-(4 if ch==" " else 0)
for ink,name in (("#1c2321","logo.svg"),("#e8ebe6","logo-dark.svg")):
    W=80+x
    open(name,"w").write(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W:.0f} 64" width="{W:.0f}" height="64" role="img" aria-label="AI Meter">\n{inner}\n<path transform="translate(80 0)" fill="{ink}" d="{" ".join(cmds)}"/>\n</svg>\n')
