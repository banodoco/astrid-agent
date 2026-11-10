# Test art for the preview loop only (not a shipped asset): a palette-only
# 320x180 pixel plate and 16px sprites. Usage: make_art.py <dir>.
from PIL import Image, ImageDraw
import sys
import os
out = sys.argv[1]
os.makedirs(out, exist_ok=True)
PAL = {
    'paper': (247, 244, 237), 'peach': (244, 210, 178), 'peach2': (233, 180, 140),
    'ink': (37, 36, 31), 'rust': (169, 71, 20), 'orange': (237, 107, 35),
    'mink': (255, 122, 46), 'charcoal': (31, 31, 31), 'panel': (255, 254, 250),
}
W, H = 320, 180
im = Image.new('RGB', (W, H), PAL['paper'])
d = ImageDraw.Draw(im)
# Warm sky bands (flat, 3 tones), then a desk and props.
d.rectangle([0, 0, W, 70], fill=PAL['peach'])
d.rectangle([0, 70, W, 110], fill=PAL['peach2'])
for x in range(0, W, 12):           # window grid blocks
    d.rectangle([220 + x // 4, 20, 222 + x // 4, 60], fill=PAL['paper'])
d.rectangle([200, 14, 300, 66], outline=PAL['ink'])
d.ellipse([40, 24, 84, 68], fill=PAL['orange'], outline=PAL['ink'])   # sun/lamp
d.rectangle([0, 110, W, H], fill=PAL['charcoal'])                    # desk
d.rectangle([150, 96, 176, 110], fill=PAL['rust'], outline=PAL['ink'])  # mug
d.rectangle([20, 120, 120, 124], fill=PAL['ink'])                     # shelf line
for i in range(8):
    d.rectangle([24 + i * 12, 104, 30 + i * 12, 118], fill=PAL['mink'] if i % 2 else PAL['rust'])
im.save(f'{out}/plate_320x180.png')

# Sprite: 16x16 blob with outline, transparent background, 4-frame strip.
def sprite(frame):
    s = Image.new('RGBA', (16, 16), (0, 0, 0, 0))
    sd = ImageDraw.Draw(s)
    sd.ellipse([3, 4, 13, 14], fill=PAL['mink'] + (255,), outline=PAL['ink'] + (255,))
    sd.polygon([(4, 5), (5, 0), (8, 4)], fill=PAL['mink'] + (255,), outline=PAL['ink'] + (255,))
    sd.polygon([(12, 5), (11, 0), (8, 4)], fill=PAL['mink'] + (255,), outline=PAL['ink'] + (255,))
    sd.point((6, 8), fill=PAL['ink'] + (255,)); sd.point((10, 8), fill=PAL['ink'] + (255,))
    leg = 1 if frame % 2 else 0
    sd.rectangle([5, 13 + leg, 6, 15], fill=PAL['ink'] + (255,))
    sd.rectangle([9, 13 - leg, 10, 15], fill=PAL['ink'] + (255,))
    return s
single = sprite(0); single.save(f'{out}/sprite_16.png')
strip = Image.new('RGBA', (64, 16), (0, 0, 0, 0))
for i in range(4):
    strip.paste(sprite(i), (i * 16, 0))
strip.save(f'{out}/sprite_strip_4x16.png')
print('plate palette size', len(im.getcolors(maxcolors=100000)), 'colours; wrote', out)
