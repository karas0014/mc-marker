# -*- coding: utf-8 -*-
"""Fetch the CJK fonts the PDF reports need, into ./fonts/.

Run at build time (see render.yaml). Render's container has no CJK font
installed and no system package manager available on the free tier, so
report_engine.resolve_fonts() would otherwise raise "No CJK font found".

Google ships Noto Sans TC only as a *variable* font whose default instance is
Thin (wght=100) -- embedding that directly gives a report that is legible but
far too light. So we instantiate two static faces, Regular (400) and Bold
(700), which is what the reports actually ask for.

Idempotent: exits immediately if both faces are already present, so a warm
build or a repeated local run costs nothing.
"""
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(os.path.dirname(HERE), 'fonts')
SRC_URL = ('https://github.com/google/fonts/raw/main/ofl/notosanstc/'
           'NotoSansTC%5Bwght%5D.ttf')
FACES = [(400, 'NotoSansTC-Regular.ttf'), (700, 'NotoSansTC-Bold.ttf')]


def main():
    targets = [os.path.join(FONT_DIR, n) for _, n in FACES]
    if all(os.path.exists(t) for t in targets):
        print('fonts: already present, nothing to do')
        return 0

    os.makedirs(FONT_DIR, exist_ok=True)
    vf_path = os.path.join(FONT_DIR, '_NotoSansTC-VF.ttf')
    if not os.path.exists(vf_path):
        print('fonts: downloading %s' % SRC_URL)
        req = urllib.request.Request(SRC_URL, headers={'User-Agent': 'mc-marker-build'})
        with urllib.request.urlopen(req, timeout=180) as r, open(vf_path, 'wb') as f:
            f.write(r.read())
        print('fonts: downloaded %.1f MB' % (os.path.getsize(vf_path) / 1e6))

    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer
    for wght, name in FACES:
        out = os.path.join(FONT_DIR, name)
        if os.path.exists(out):
            continue
        font = TTFont(vf_path)
        instancer.instantiateVariableFont(
            font, {'wght': wght}, inplace=True, updateFontNames=True)
        font.save(out)
        print('fonts: wrote %s (wght=%d, %.1f MB)' % (name, wght, os.path.getsize(out) / 1e6))

    # The variable source is only needed to derive the two static faces.
    try:
        os.remove(vf_path)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
