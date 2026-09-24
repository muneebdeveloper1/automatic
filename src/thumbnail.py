from __future__ import annotations
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

def _font(size):
    candidates=[
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]
    for p in candidates:
        if Path(p).exists():return ImageFont.truetype(p,size)
    return ImageFont.load_default()

def make(template,cover,hook,out,cfg):
    im=Image.open(template).convert("RGBA")
    if cover and Path(cover).exists():
        c=Image.open(cover).convert("RGBA")
        c.thumbnail((int(cfg["cover_width"]),int(cfg["cover_height"])),Image.Resampling.LANCZOS)
        x=int(cfg["cover_x"])+(int(cfg["cover_width"])-c.width)//2
        y=int(cfg["cover_y"])+(int(cfg["cover_height"])-c.height)//2
        im.alpha_composite(c,(x,y))
    hook=" ".join(str(hook).replace("\n"," ").split()[:5]).strip()
    if not hook:raise ValueError("Empty thumbnail hook")
    draw=ImageDraw.Draw(im)
    font=_font(int(cfg["hook_font_size"]))
    max_width=int(cfg["hook_max_width"])
    # Shrink font if needed instead of letting text run outside the template.
    while draw.textbbox((0,0),hook,font=font,stroke_width=2)[2] > max_width and getattr(font,"size",0)>24:
        font=_font(getattr(font,"size",int(cfg["hook_font_size"]))-2)
    draw.text((int(cfg["hook_x"]),int(cfg["hook_y"])),hook,font=font,
              fill=cfg.get("hook_fill","#FFFFFF"),stroke_width=3,stroke_fill=cfg.get("hook_stroke","#000000"))
    out.parent.mkdir(parents=True,exist_ok=True);im.convert("RGB").save(out,"PNG",optimize=True)
    with Image.open(out) as check:
        if check.size != im.size:raise RuntimeError("Thumbnail dimension validation failed")
