"""Cut-first reel reproduction (instagram-reel-trace v4.3, 2026-11-21).

Flow: init -> (AI writes cuts/cut-N.yaml + overlays + lines with ja) -> stills
      -> confirm (ONE question: cut images, pasted product images, voice, captions EN/JA, telop, price)
      -> order (paid, MiniMax) -> assemble (overlays + telop + voice) -> done.

  python scripts/reel.py init     <project> (--url URL | --file MP4) --look IMAGE
  python scripts/reel.py frames   <project>                  # re-extract first frames after editing project.yaml
  python scripts/reel.py stills   <project> [--cut N] [--tries 3]
  python scripts/reel.py confirm  <project>
  python scripts/reel.py order    <project> --cap JPY [--cut N]
  python scripts/reel.py assemble <project> --voice A|B|C --telop A|B|C [--voice-engine minimax|edge] [--check]

<project> is a production folder (any folder, e.g. productions/2026-11-21-xxx). Nothing paid runs except
`order` (MiniMax video) and `assemble --voice-engine minimax` (a few yen of speech).
"""
from __future__ import annotations

import argparse, asyncio, hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont

for _s in (sys.stdout, sys.stderr):  # Japanese log lines stay readable when the output is redirected to a file
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass

SKILL = Path(__file__).resolve().parents[1]
ASSETS = SKILL / 'assets'


def find_up(start, rel):
    """first existing <dir>/<rel> walking up from start"""
    for d in [Path(start).resolve(), *Path(start).resolve().parents]:
        if (d / rel).exists():
            return d / rel
    return None


# image tool: REEL_IMAGE_TOOL, else a Codex image helper found above the skill (skills/codex-image/codex_image.py)
_tool = os.environ.get('REEL_IMAGE_TOOL')
CODEX = Path(_tool) if _tool else find_up(SKILL, Path('skills') / 'codex-image' / 'codex_image.py')


def ensure_key(project=None):
    """MINIMAX_API_KEY from the environment, else the nearest .env above the project or the skill (value never printed)"""
    if os.environ.get('MINIMAX_API_KEY'):
        return
    for start in [x for x in (project, SKILL) if x]:
        env = find_up(start, '.env')
        if env:
            m = re.search(r'^MINIMAX_API_KEY=(.+)$', env.read_text(encoding='utf-8'), re.M)
            if m and m.group(1).strip():
                os.environ['MINIMAX_API_KEY'] = m.group(1).strip()
                return
    raise SystemExit('[停止] MINIMAX_API_KEY が見つからない。依頼者にキーを設定してもらう（環境変数か .env）')
FONT_NOTO = ASSETS / 'fonts' / 'NotoSansJP-VF.ttf'
FONT_ANTON = ASSETS / 'fonts' / 'Anton-Regular.ttf'
VW, VH = 768, 1344            # MiniMax 768P vertical
FW, FH = 1080, 1920           # still size
CLIP_SEC = 4                  # MiniMax minimum per order


def clip_sec(c):
    """order length for a cut: 4 s minimum, the cut length rounded up for longer cuts, 15 s maximum"""
    import math
    return min(15, max(CLIP_SEC, math.ceil(c['end'] - c['start'] - 0.05)))


def log(tag, msg):
    print(f'[{tag}] {msg}', flush=True)


def ff(*args, timeout=900):
    """ffmpeg with a time limit: a filter combination that never ends becomes a clear stop instead of a silent wait"""
    try:
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', *map(str, args)], check=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemExit(f'[停止] ffmpeg が {timeout} 秒で終わらない（止まっている）: {" ".join(map(str, args))[:300]}')


def dur(p):
    return float(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', str(p)], text=True))


def load(p):
    return yaml.safe_load(Path(p).read_text(encoding='utf-8'))


def save(p, data):
    Path(p).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120), encoding='utf-8')


class P:
    """project paths"""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.yaml = self.root / 'project.yaml'
        self.cuts = self.root / 'cuts'
        self.src = self.root / 'work' / 'src'
        self.stills = self.root / 'work' / 'stills'
        self.clips = self.root / 'work' / 'clips'
        self.voice = self.root / 'work' / 'voice'
        self.telop = self.root / 'work' / 'telop'
        self.out = self.root / 'outputs'
        for d in (self.cuts, self.src, self.stills, self.clips, self.voice, self.telop, self.out):
            d.mkdir(parents=True, exist_ok=True)

    def data(self):
        return load(self.yaml)

    def cut(self, n):
        return load(self.cuts / f'cut-{n}.yaml')


# ---------------------------------------------------------------- init / frames

def detect_cuts(ref, threshold=0.3):
    r = subprocess.run(['ffmpeg', '-nostdin', '-i', str(ref), '-vf', f"select='gt(scene,{threshold})',showinfo", '-f', 'null', '-'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    ts = sorted({round(float(m), 3) for m in re.findall(r'pts_time:([0-9.]+)', r.stderr)})
    return [t for t in ts if t > 0.3]


CUT_TEMPLATE = {
    'cut': 0,
    'time': '0.00-0.00',
    'first_frame': 0.0,
    'camera': {'height': '', 'angle': '', 'lens': '', 'crop': '', 'notes': ''},
    'person': {'position_pct': [0, 0, 0, 0], 'body': '', 'hands': '', 'gaze': '', 'expression': ''},
    'products': [
        {'id': 'P1', 'what': '', 'count': 1, 'layout': '', 'bbox_pct': [0, 0, 0, 0], 'held': '',
         'art': {'colors': [], 'elements': '', 'title': ''}, 'keep_print': True}],
    'containers': [],
    'room_map': {},
    'remove': ['参考店のロゴ・透かし', '動画の上のテロップ'],
    'keep': [],
    'talk': False,
    'motion': {'action': '', 'within_sec': 2.0},
    'use': {'in': 0.0},
    'checks': [],
}


def cmd_init(a):
    p = P(a.project)
    ref = p.root / 'inputs' / 'reference.mp4'
    ref.parent.mkdir(exist_ok=True)
    if a.file:
        shutil.copy(a.file, ref)
    elif not ref.exists():
        import yt_dlp
        opts = {'outtmpl': str(ref.with_suffix('.%(ext)s')), 'noplaylist': True, 'noprogress': True, 'format': 'bv*+ba/b',
                'merge_output_format': 'mp4', 'writeinfojson': True, 'quiet': True, 'no_warnings': True}
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([a.url])
    look = p.root / 'inputs' / ('look' + Path(a.look).suffix)
    shutil.copy(a.look, look)
    total = dur(ref)
    edges = [0.0] + detect_cuts(ref) + [round(total, 3)]
    cuts = [{'n': i + 1, 'start': edges[i], 'end': edges[i + 1], 'first_frame': round(edges[i] + 0.05, 3)}
            for i in range(len(edges) - 1) if edges[i + 1] - edges[i] >= 0.5]
    data = {
        'reference': {'url': a.url or '', 'file': 'inputs/reference.mp4', 'seconds': round(total, 3)},
        'look': {'file': str(look.relative_to(p.root)).replace('\\', '/'),
                 'person': '(AI: 画像の人物の見た目を英語で。例 short black hair, white T-shirt, navy apron ...)',
                 'room': '(AI: 画像の部屋・店の見た目を英語で)'},
        'cuts': cuts,
        'lines': [{'caption': '', 'ja': '', 'spoken': '', 'start': 0.0, 'end': 0.0, 'voice_start': 0.0, 'emotion': 'happy'}],
        # images pasted on top of the video in the reference (not held / not in the room): official images, same time/place/size
        'overlays': [],   # [{file, start, end, cx_pct, cy_pct, w_pct}]; sources -> assets/sources.json
        'voice': None, 'telop': None, 'cap_jpy': None,
    }
    save(p.yaml, data)
    for c in cuts:
        t = dict(CUT_TEMPLATE, cut=c['n'], time=f"{c['start']:.2f}-{c['end']:.2f}", first_frame=c['first_frame'])
        cp = p.cuts / f"cut-{c['n']}.yaml"
        if not cp.exists():
            save(cp, t)
    log('OK', f"{len(cuts)} cuts detected: {[c['start'] for c in cuts]} (check them, edit project.yaml, then run frames)")
    cmd_frames(a)


def grid(im):
    d = ImageDraw.Draw(im)
    for i in range(1, 10):
        x, y = im.width * i / 10, im.height * i / 10
        d.line([(x, 0), (x, im.height)], fill=(255, 230, 0), width=2)
        d.line([(0, y), (im.width, y)], fill=(255, 230, 0), width=2)
        d.text((x + 4, 4), f'{i * 10}', fill=(255, 230, 0), font=noto(28, 700))
        d.text((4, y + 4), f'{i * 10}', fill=(255, 230, 0), font=noto(28, 700))
    return im


def cmd_frames(a):
    p = P(a.project)
    d = p.data()
    ref = p.root / d['reference']['file']
    for c in d['cuts']:
        f = p.src / f"ref-{c['n']}.png"
        ff('-ss', c['first_frame'], '-i', ref, '-frames:v', 1, '-vf', f'scale={FW}:{FH}', f)
        grid(Image.open(f).convert('RGB')).save(p.src / f"grid-{c['n']}.png")   # read positions in % from this
    sheet([Image.open(p.src / f"ref-{c['n']}.png") for c in d['cuts']], p.out / 'cuts-reference.jpg',
          [f"{c['n']}  {c['start']:.2f}-{c['end']:.2f}s" for c in d['cuts']])
    # every 0.25 s of the whole reference, 32 frames per sheet: check cut edges AND images pasted on top in editing
    # (cards / boxes that pop up mid-cut) -> list them in project.yaml overlays
    for old in p.src.glob('scan-*.jpg'):
        old.unlink()
    total, step, per = dur(ref), 0.25, 32
    tw, th = 180, 320
    for k in range(int(total / step / per) + 1):
        t0 = k * per * step
        if t0 >= total:
            break
        raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(t0), '-t', str(per * step), '-i', str(ref), '-vf',
                              f'fps={1 / step},scale={tw}:{th}', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'], capture_output=True).stdout
        n_fr = len(raw) // (tw * th * 3)
        sh = Image.new('RGB', (8 * (tw + 6) + 6, 4 * (th + 30) + 6), 'white')
        dr = ImageDraw.Draw(sh)
        for i in range(n_fr):
            fr = Image.frombytes('RGB', (tw, th), raw[i * tw * th * 3:(i + 1) * tw * th * 3])
            x, y = 6 + (i % 8) * (tw + 6), 6 + (i // 8) * (th + 30)
            sh.paste(fr, (x, y + 24))
            dr.text((x, y), f'{t0 + i * step:.2f}s', fill='black', font=noto(20, 700))
        sh.save(p.src / f'scan-{k + 1:02d}.jpg', quality=85)
    log('OK', f'first frames + grids in {p.src}; overview {p.out / "cuts-reference.jpg"}; '
              f'0.25 s scan sheets {p.src}/scan-*.jpg (look for cut edges and images pasted on top)')


# ---------------------------------------------------------------- stills

BASE_PROMPT = (
    "Edit the FIRST attached image (a frame from a phone video). Keep EXACTLY its camera angle, framing, crop, the person's "
    "pose, body position, hand positions and gaze, and keep EXACTLY every product and container in it. "
    "Change only two things: (1) the person becomes the person of the SECOND attached image ({person}); "
    "(2) the room becomes the place of the SECOND attached image ({room}), seen from this same camera angle and distance. "
    "Same camera distance: the person and every product keep the same position and size in the frame (percent) as in the "
    "first image; do not zoom in or move closer. Exactly one person. Remove ALL video overlays: captions, subtitles, watermark logos, shop logos added on top of the video; "
    "printed flyers on a desk become plain white paper. Photorealistic smartphone photo, vertical 9:16."
)
PRODUCT_RULE = (
    " The THIRD attached image shows close-ups of the exact products in the first image (one panel per product, labelled P1, P2 ...). "
    "Reproduce each product IDENTICALLY: same number of items, same separation between items, same proportions, same package "
    "artwork, characters, layout, colors and printed package text. Do not invent, simplify, merge or rearrange any package design. "
    "The package printing is part of the product and must be kept; only video overlays are removed."
)


def pct_box(b, w=FW, h=FH, margin=0.0):
    x0, y0, x1, y1 = b
    mx, my = (x1 - x0) * margin, (y1 - y0) * margin
    return (max(0, int((x0 - mx) * w / 100)), max(0, int((y0 - my) * h / 100)),
            min(w, int((x1 + mx) * w / 100)), min(h, int((y1 + my) * h / 100)))


def product_tile(p, n, cut):
    """all product close-ups of a cut in ONE image (so the image model never gets more than 3 images)"""
    prods = [x for x in cut.get('products') or [] if any(x.get('bbox_pct') or [])]
    if not prods:
        return None
    ref = Image.open(p.src / f'ref-{n}.png').convert('RGB')
    crops = []
    for x in prods:
        c = ref.crop(pct_box(x['bbox_pct'], margin=0.08))
        s = 600 / max(c.size)
        crops.append((x['id'], c.resize((max(1, int(c.width * s)), max(1, int(c.height * s))), Image.LANCZOS)))
    W = max(c.width for _, c in crops) + 40
    H = sum(c.height + 70 for _, c in crops)
    tile = Image.new('RGB', (W, H), 'white')
    d, y = ImageDraw.Draw(tile), 0
    for pid, c in crops:
        d.text((20, y + 8), pid, fill='black', font=noto(40, 900))
        tile.paste(c, (20, y + 60))
        y += c.height + 70
    out = p.src / f'products-{n}.png'
    tile.save(out)
    return out


def describe(cut):
    """design doc -> prompt text, products first (highest priority)"""
    parts = []
    for x in cut.get('products') or []:
        art = x.get('art') or {}
        parts.append(
            f"Product {x.get('id')}: {x.get('what')}; count {x.get('count')}; layout: {x.get('layout')}; "
            f"position in frame (percent x0,y0,x1,y1): {x.get('bbox_pct')}; held: {x.get('held')}; "
            f"artwork: {art.get('elements')}; main colors {', '.join(art.get('colors') or [])}; title: {art.get('title')}.")
    for x in cut.get('containers') or []:
        parts.append(f"Container: {json.dumps(x, ensure_ascii=False)}.")
    cam, per = cut.get('camera') or {}, cut.get('person') or {}
    parts.append(f"Camera: {json.dumps(cam, ensure_ascii=False)}.")
    parts.append(f"Person pose: {json.dumps(per, ensure_ascii=False)}.")
    if cut.get('room_map'):
        parts.append('Room mapping (reference part -> new place): ' + '; '.join(f'{k} -> {v}' for k, v in cut['room_map'].items()) + '.')
    if cut.get('keep'):
        parts.append('Keep: ' + '; '.join(cut['keep']) + '.')
    if cut.get('remove'):
        parts.append('Remove: ' + '; '.join(cut['remove']) + '.')
    if cut.get('extra'):
        parts.append(str(cut['extra']))
    return ' '.join(parts)


def scores(p, n, cut, still):
    """product match per product box: SIFT inliers + color histogram correlation (aids; the AI still looks)"""
    ref = cv2.imread(str(p.src / f'ref-{n}.png'))
    gen = cv2.resize(cv2.imread(str(still)), (FW, FH))
    res = []
    for x in cut.get('products') or []:
        if not any(x.get('bbox_pct') or []):
            continue
        x0, y0, x1, y1 = pct_box(x['bbox_pct'], margin=0.15)
        a, b = ref[y0:y1, x0:x1], gen[y0:y1, x0:x1]
        ha = cv2.calcHist([cv2.cvtColor(a, cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 32], [0, 180, 0, 256])
        hb = cv2.calcHist([cv2.cvtColor(b, cv2.COLOR_BGR2HSV)], [0, 1], None, [30, 32], [0, 180, 0, 256])
        corr = float(cv2.compareHist(cv2.normalize(ha, ha), cv2.normalize(hb, hb), cv2.HISTCMP_CORREL))
        sift = cv2.SIFT_create()
        k1, d1 = sift.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), None)
        k2, d2 = sift.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), None)
        inl = 0
        if d1 is not None and d2 is not None and len(d1) > 1 and len(d2) > 1:
            good = [m for m, n2 in (pair for pair in cv2.BFMatcher().knnMatch(d1, d2, k=2) if len(pair) == 2) if m.distance < 0.75 * n2.distance]
            if len(good) >= 4:
                _, mask = cv2.estimateAffinePartial2D(np.float32([k1[m.queryIdx].pt for m in good]),
                                                      np.float32([k2[m.trainIdx].pt for m in good]), ransacReprojThreshold=5)
                inl = int(mask.sum()) if mask is not None else 0
        res.append({'id': x.get('id'), 'color_corr': round(corr, 3), 'sift_inliers': inl})
    return res


def passed(sc, th):
    return all(s['color_corr'] >= th['color_corr'] and s['sift_inliers'] >= th['sift_inliers'] for s in sc)


def cmd_stills(a):
    p = P(a.project)
    d = p.data()
    th = {'color_corr': a.min_color, 'sift_inliers': a.min_inliers}
    report = json.loads((p.stills / 'scores.json').read_text(encoding='utf-8')) if (p.stills / 'scores.json').exists() else {}
    for c in d['cuts']:
        n = c['n']
        if a.cut and n != a.cut:
            continue
        cut = p.cut(n)
        final = p.stills / f'first-{n}.png'
        if final.exists() and not a.cut:
            log('INFO', f'cut {n}: {final.name} exists, skip')
            continue
        tile = product_tile(p, n, cut)
        prompt = BASE_PROMPT.format(person=d['look']['person'], room=d['look']['room']) + (PRODUCT_RULE if tile else '') + ' ' + describe(cut)
        (p.stills / f'prompt-{n}.txt').write_text(prompt, encoding='utf-8')
        ref_list = [p.src / f'ref-{n}.png', p.root / d['look']['file']] + ([tile] if tile else [])
        (p.stills / f'refs-{n}.txt').write_text('\n'.join(str(x) for x in ref_list), encoding='utf-8')
        if not CODEX or not Path(CODEX).exists():
            log('STOP', f'画像生成の道具が見つからない。prompt-{n}.txt と refs-{n}.txt（この順に画像を渡す）で描き、'
                        f'{final} に 1080x1920 で保存してから confirm へ進む（REEL_IMAGE_TOOL で道具を指定してもよい）')
            continue
        best = None
        for t in range(1, a.tries + 1):
            out = p.stills / f'first-{n}-try{t}.png'
            refs = ['--ref', str(p.src / f'ref-{n}.png'), '--ref', str(p.root / d['look']['file'])] + (['--ref', str(tile)] if tile else [])
            log('INFO', f'cut {n}: drawing (try {t}/{a.tries}) ...')
            r = subprocess.run([sys.executable, str(CODEX), *refs, '--aspect', '9:16', '--size', f'{FW}x{FH}', '--out', str(out),
                                '--timeout', '600', '--prompt', prompt], capture_output=True, text=True, encoding='utf-8', errors='replace')
            if not out.exists():
                log('NG', f'cut {n}: no image ({(r.stdout + r.stderr).strip().splitlines()[-1:]})')
                continue
            sc = scores(p, n, cut, out)
            total = sum(s['color_corr'] * 100 + s['sift_inliers'] for s in sc)
            log('INFO', f'cut {n} try {t}: {sc}')
            if best is None or total > best[0]:
                best = (total, out, sc)
            if passed(sc, th):
                break
        if best:
            shutil.copy(best[1], final)
            report[str(n)] = {'file': best[1].name, 'scores': best[2], 'passed': passed(best[2], th)}
            (p.stills / 'scores.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            log('OK' if passed(best[2], th) else 'WARN', f"cut {n}: {final.name} ({'基準内' if passed(best[2], th) else '基準未達・目で確認'})")
    log('INFO', '数値は目安。商品の拡大を参考と並べ、設計書の checks を必ず目で確かめる')


# ---------------------------------------------------------------- confirm (the ONE question)

def noto(px, wght=900):
    f = ImageFont.truetype(str(FONT_NOTO), px)
    try:
        f.set_variation_by_axes([wght])
    except Exception:
        pass
    return f


def sheet(images, out, labels=None, w=300):
    h = int(w * 16 / 9)
    im = Image.new('RGB', (len(images) * (w + 10) + 10, h + 60), 'white')
    d = ImageDraw.Draw(im)
    for i, x in enumerate(images):
        im.paste(x.convert('RGB').resize((w, h)), (10 + i * (w + 10), 50))
        if labels:
            d.text((10 + i * (w + 10), 8), labels[i], fill='black', font=noto(26, 700))
    im.save(out, quality=90)


def estimate_jpy(cuts):
    sys.path.insert(0, str(SKILL / 'engine'))
    from lib import cost
    return sum(cost.estimate('768P', clip_sec(c), ref_image_count=0, ref_video_seconds=0)['total_jpy'] for c in cuts)


def cmd_confirm(a):
    p = P(a.project)
    d = p.data()
    cuts = d['cuts']
    w, h = 300, 533
    rows = 2
    zooms = []
    for c in cuts:
        cut = p.cut(c['n'])
        for x in cut.get('products') or []:
            if any(x.get('bbox_pct') or []) and (p.stills / f"first-{c['n']}.png").exists():
                bb = pct_box(x['bbox_pct'], margin=0.15)
                r = Image.open(p.src / f"ref-{c['n']}.png").convert('RGB').crop(bb)
                g = Image.open(p.stills / f"first-{c['n']}.png").convert('RGB').resize((FW, FH)).crop(bb)
                zooms.append((f"{c['n']}-{x.get('id')}", r, g))
    zh = 260
    W = len(cuts) * (w + 10) + 130
    H = rows * (h + 50) + (len(zooms) and (zh + 50) * ((len(zooms) + 1) // 2)) + 40
    im = Image.new('RGB', (max(W, 1400), H), 'white')
    dr = ImageDraw.Draw(im)
    for r, (label, src) in enumerate([('参考', 'src/ref-{}.png'), ('今回', 'stills/first-{}.png')]):
        dr.text((10, r * (h + 50) + 50 + h // 2), label, fill='black', font=noto(34))
        for i, c in enumerate(cuts):
            f = p.root / 'work' / src.format(c['n'])
            x0 = 120 + i * (w + 10)
            if r == 0:
                dr.text((x0, 8), f"カット{c['n']}", fill='black', font=noto(28, 700))
            if f.exists():
                im.paste(Image.open(f).convert('RGB').resize((w, h)), (x0, r * (h + 50) + 50))
            else:
                dr.rectangle((x0, r * (h + 50) + 50, x0 + w, r * (h + 50) + 50 + h), outline='red', width=3)
    y = rows * (h + 50) + 20
    for i, (lab, r_, g_) in enumerate(zooms):
        col, row = i % 2, i // 2
        x0, y0 = 10 + col * 700, y + row * (zh + 50)
        dr.text((x0, y0), f'商品 {lab}（左＝参考／右＝今回）', fill='black', font=noto(24, 700))
        for j, z in enumerate((r_, g_)):
            s = zh / z.height
            zz = z.resize((min(330, int(z.width * s)), zh))
            im.paste(zz, (x0 + j * 340, y0 + 34))
    out = p.out / 'confirm.jpg'
    im.save(out, quality=88)
    # product images laid over the video (if any): one preview per overlay start, on the still of that moment's cut
    ovs = [o for o in d.get('overlays') or [] if (p.root / o['file']).exists()]
    ov_out = None
    if ovs:
        times = sorted({o['start'] for o in ovs})
        frames, labels = [], []
        for t in times:
            acc, cut_n = 0.0, cuts[-1]['n']
            for c in cuts:
                if acc <= t < acc + (c['end'] - c['start']):
                    cut_n = c['n']
                    break
                acc += c['end'] - c['start']
            base = p.stills / f'first-{cut_n}.png'
            if not base.exists():
                continue
            fr = Image.open(base).convert('RGBA').resize((VW, VH))
            for o in ovs:
                if o['start'] <= t < o['end']:
                    ov = Image.open(p.root / o['file']).convert('RGBA')
                    w = int(VW * o['w_pct'] / 100)
                    ov = ov.resize((w, int(ov.height * w / ov.width)))
                    fr.alpha_composite(ov, (int(VW * o['cx_pct'] / 100 - ov.width / 2), int(VH * o['cy_pct'] / 100 - ov.height / 2)))
            frames.append(fr.convert('RGB'))
            labels.append(f'カット{cut_n} {t:.1f}秒〜')
        if frames:
            ov_out = p.out / 'confirm-overlays.jpg'
            sheet(frames, ov_out, labels, w=260)
    jpy = estimate_jpy(cuts)
    vj = json.loads((ASSETS / 'voices' / 'voices.json').read_text(encoding='utf-8'))
    tj = json.loads((ASSETS / 'telop' / 'telop-styles.json').read_text(encoding='utf-8'))
    prev_v, prev_t = d.get('voice'), d.get('telop')
    lines = [x for x in d.get('lines') or [] if x.get('caption') or x.get('spoken')]
    talk_cuts = [c['n'] for c in cuts if p.cut(c['n']).get('talk')]
    over = [ln for ln in lines if ln.get('spoken') and line_cut(d, ln) not in talk_cuts]
    # telop A/B/C drawn on a real frame of this video with a real caption (not only on the plain sample)
    telop_out = None
    if lines:
        ln0 = max(lines, key=lambda x: len(x.get('caption') or ''))
        base = p.stills / f'first-{line_cut(d, ln0)}.png'
        if base.exists():
            panels = []
            for s in tj['styles']:
                tmp = p.telop / f"confirm-{s['key']}.png"
                telop_png(s['key'], ln0['caption'], ln0.get('accent', ''), tmp, ln0.get('y', tj.get('position_y', 0.7)))
                fr = Image.open(base).convert('RGBA').resize((VW, VH))
                fr.alpha_composite(Image.open(tmp).convert('RGBA'))
                panels.append(fr)
            telop_out = p.out / 'confirm-telop.jpg'
            sheet(panels, telop_out, [f"テロップ{s['key']}" for s in tj['styles']], w=300)
    msg = ['【1】カットの対比画像（上＝参考／下＝今回）: ' + str(out)]
    # 2) images pasted on top (official), per cut
    msg += ['【2】上に貼る商品画像（公式・参考と同じ時刻と位置）:']
    if ovs:
        if ov_out:
            msg += [f'  貼った見本: {ov_out}']
        for o in ovs:
            msg += [f"  カット{line_cut(d, {'start': o['start']})} {o['start']:.1f}〜{o['end']:.1f}秒: {o['file']}"]
    else:
        msg += ['  なし（参考に、上から貼られた画像はない）']
    for o in d.get('overlays') or []:
        if not (p.root / o['file']).exists():
            msg += [f"  ※画像ファイルがない: {o['file']}（公式から取得して置く）"]
    # 3) voice
    msg += ['【3】声:']
    if talk_cuts:
        opts = d.get('talk_voice_options') or [
            'a bright, friendly shop owner in his forties, warm natural American English',
            'a bright, energetic young man in his twenties, natural American English',
            'a cheerful, upbeat young woman, natural American English']
        msg += [f"  話す型のカット {talk_cuts}: 本人が話す声は動画と一緒に作られるので、音の見本はありません。声の特徴を選んでください"]
        msg += [f"    {i + 1}. {o}{'（今の設定）' if o == d.get('talk_voice') else ''}" for i, o in enumerate(opts)]
    if over:
        msg += ['  話さないカットに重ねる声（ファイルを開くと声だけ流れます）:']
        msg += [f"    {v['key']} {v['name']}{'（前回）' if v['key'] == prev_v else ''}: {ASSETS / 'voices' / v['sample']}" for v in vj['voices']]
    if not lines:
        msg += ['  なし（セリフのない動画）']
    # 4) captions per cut, English + Japanese
    msg += ['【4】カットごとの字幕（英語／日本語訳）:']
    for c in cuts:
        cl = [ln for ln in lines if line_cut(d, ln) == c['n']]
        msg += [f"  カット{c['n']}{'（話す型）' if c['n'] in talk_cuts else ''}:" + ('' if cl else ' 字幕なし')]
        for ln in cl:
            msg += [f"    {ln.get('caption', '')}", f"    （{ln.get('ja') or '※日本語訳が未記入'}）"]
            if ln.get('spoken') and ln['spoken'] != ln.get('caption'):
                msg += [f"    話す言葉: {ln['spoken']}"]
    # 5) telop
    msg += ['【5】テロップの型:' + ('' if lines else ' なし（字幕がない）')]
    if lines:
        if telop_out:
            msg += [f'  この動画に重ねた見本: {telop_out}']
        msg += [f"    {s['key']} {s['name']}{'（前回）' if s['key'] == prev_t else ''}" for s in tj['styles']]
    # 6) price
    msg += [f"【6】料金: 動画にすると約{jpy}円です（{len(cuts)}カット・合計{sum(clip_sec(c) for c in cuts)}秒分。"
            f"カットごとの秒数 {[clip_sec(c) for c in cuts]}）。"]
    ex = ['OK'] + (['声の特徴2'] if talk_cuts else []) + (['声B'] if over else []) + (['テロップA'] if lines else [])
    msg += [f"返事の例:「{'・'.join(ex)}」／直すカットがあれば「3番直して」も一緒に"]
    (p.out / 'confirm.txt').write_text('\n'.join(msg), encoding='utf-8')
    print('\n'.join(msg))


# ---------------------------------------------------------------- order (paid)

def clip_key(n, talk, tag=''):
    """cut-N / cut-N-talk (the person speaks the lines in the clip), plus an optional tag"""
    return f'cut-{n}' + ('-talk' if talk else '') + (f'-{tag}' if tag else '')


def level_db(path, t, length):
    """loudness (dBFS, RMS) of a short stretch of a file's audio"""
    raw = subprocess.run(['ffmpeg', '-v', 'error', '-ss', str(t), '-t', str(length), '-i', str(path), '-ac', '1', '-ar', '16000',
                          '-f', 's16le', '-'], capture_output=True).stdout
    x = np.frombuffer(raw, np.int16).astype(float) / 32768
    return float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)) if len(x) else -120.0


def line_cut(d, ln):
    """number of the cut whose time span (on the finished video's timeline) holds this line's voice start"""
    t, acc = ln.get('voice_start', ln['start']), 0.0
    for c in d['cuts']:
        ln_ = c['end'] - c['start']
        if acc - 0.05 <= t < acc + ln_:
            return c['n']
        acc += ln_
    return d['cuts'][-1]['n']


MOTION_RULES = (
    "The first frame is the exact start of this shot. ONE continuous shot from a handheld phone: no cuts, no zoom-ins, no new "
    "camera angles. Exactly one person, the person of the first frame. The room is always the place of the first frame; nothing "
    "else appears. Every product, crate, case and carton keeps exactly the shape, size, colors and package artwork of the first "
    "frame in every frame; do not redesign, recolor or swap any product. No text, no logos added, no subtitles. Hands anatomically "
    "plausible. No speech, no lip movement. Sound: quiet indoor ambience only, no music."
)


def cmd_order(a):
    ensure_key(a.project)
    sys.path.insert(0, str(SKILL / 'engine'))
    from lib import client, cost, preflight, upload
    from lib.common import probe_summary
    p = P(a.project)
    d = p.data()
    ledger = p.root / 'orders.json'
    st = json.loads(ledger.read_text(encoding='utf-8')) if ledger.exists() else {}
    spent = lambda: sum(v.get('actual_jpy') or v.get('estimated_jpy') or 0 for v in st.values())  # noqa: E731
    for c in d['cuts']:
        n = c['n']
        if a.cut and n != a.cut:
            continue
        cut = p.cut(n)
        talk = a.talk or bool(cut.get('talk'))
        key = clip_key(n, talk, a.tag)
        out = p.clips / f'{key}.mp4'
        if out.exists():
            log('INFO', f'{key}: clip exists, skip')
            continue
        mot = cut.get('motion') or {}
        if talk:   # the man speaks the lines himself (MiniMax makes voice + lip sync); no reference audio, so first_frame still works
            off = sum(x['end'] - x['start'] for x in d['cuts'] if x['n'] < n)   # this cut's start on the finished timeline
            said = [ln for ln in d.get('lines') or [] if ln.get('spoken') and line_cut(d, ln) == n]
            script = '\n'.join(f"- at about {max(0, ln.get('voice_start', ln['start']) - off):.1f}s: \"{ln['spoken']}\"" for ln in said)
            voice = d.get('talk_voice') or 'a bright, energetic young man in his twenties, natural American English, friendly shop-staff tone'
            act = re.sub(r',?\s*(with (his )?)?mouth closed( \(a voice-over is added later\))?', '', mot.get('action') or '')
            prompt = (f"He speaks directly to the camera in English, lips in sync with the words, with natural hand gestures: "
                      f"{act}\nVoice: {voice}. He says exactly these lines, word for word, nothing else:\n{script}\n\n"
                      + MOTION_RULES.replace('No speech, no lip movement. Sound: quiet indoor ambience only, no music.',
                                             'Sound: only his voice and quiet indoor ambience; no music, no other voices.'))
        else:
            prompt = f"Action (all within the first {mot.get('within_sec', 2)} seconds, then hold): {mot.get('action')}\n\n{MOTION_RULES}"
        if a.dry:
            print(f'--- {key} ({clip_sec(c)}s)\n{prompt}\n')
            continue
        first = p.stills / f'first-{n}.png'
        pf = preflight.run(prompt=prompt, duration=clip_sec(c), resolution='768P', ratio='9:16', first_frame=str(first), last_frame=None,
                           ref_images=[], ref_videos=[], ref_audios=[])
        est = cost.estimate('768P', clip_sec(c), ref_image_count=0, ref_video_seconds=pf.ref_video_seconds)
        s = st.get(key, {})
        if not s.get('task_id'):
            if s.get('status') == 'submitting':
                raise SystemExit(f'[停止] {key}: 前回の注文の成否が不明。MiniMaxの画面で確認するまで再注文しない')
            if spent() + est['total_jpy'] > a.cap:
                raise SystemExit(f"[停止] 上限 {a.cap}円を超えるので注文しない（使用済み約{spent()}円＋{est['total_jpy']}円）")
            ok, why = cost.check_daily_limit(est['total_jpy'])
            if not ok:
                raise SystemExit('[停止] ' + why)
            upload.upload_all(pf)
            payload = client.build_payload(prompt=prompt, duration=clip_sec(c), resolution='768P', ratio='9:16', assets=pf.assets)
            st[key] = {'status': 'submitting'}
            ledger.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
            tid = client.create_task(payload)
            st[key] = {'status': 'ordered', 'task_id': tid, 'estimated_jpy': est['total_jpy']}
            ledger.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
            cost.append_ledger({'task_id': tid, 'status': 'created', 'billed': True, 'est_jpy': est['total_jpy'], 'title': f'{p.root.name}-{key}'})
            log('受付', f'{key} 注文番号 {tid}')
        task = client.poll(st[key]['task_id'])
        if task.get('status') != 'succeeded':
            raise SystemExit(f"[停止] {key} は {task.get('status')}。作り直さずに報告する")
        task = client.settle_price(st[key]['task_id'], task)
        client.download(st[key]['task_id'], task, out)
        act = cost.actual_from_task(task)
        st[key].update(status='completed', actual_jpy=act['jpy'], actual_usd=act['usd'], probe=probe_summary(out))
        ledger.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding='utf-8')
        log('完成', f"{key} 実費 約{act['jpy']}円（この制作の合計 約{spent()}円）")


# ---------------------------------------------------------------- assemble (voice + telop)

def telop_png(style, text, key, path, y_frac):
    im = Image.new('RGBA', (VW, VH), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cy = VH * y_frac

    def fit(t, fn, px, maxw):
        f = fn(px)
        while d.textbbox((0, 0), t, font=f)[2] > maxw and px > 20:
            px -= 2
            f = fn(px)
        return f

    if style == 'A':
        f = fit(text, noto, 52, VW - 140)
        x0, y0, x1, y1 = d.textbbox((VW / 2, cy), text, font=f, anchor='mm')
        d.rounded_rectangle((x0 - 26, y0 - 18, x1 + 26, y1 + 18), radius=22, fill='white')
        d.text((VW / 2, cy), text, font=f, anchor='mm', fill='#111111')
    elif style == 'B':
        up = text.upper()
        anton = lambda px: ImageFont.truetype(str(FONT_ANTON), px)  # noqa: E731
        f = fit(up, anton, 78, VW - 90)
        x = (VW - d.textbbox((0, 0), up, font=f)[2]) / 2
        i = up.find((key or '').upper()) if key else -1
        parts = [(up[:i], 'white'), (up[i:i + len(key)], '#FFD400'), (up[i + len(key):], 'white')] if i >= 0 else [(up, 'white')]
        for s, col in parts:
            d.text((x, cy), s, font=f, anchor='lm', fill=col, stroke_width=4, stroke_fill='#1A1A1A')
            x += d.textbbox((0, 0), s, font=f)[2]
    else:  # C
        f = fit(text, noto, 54, VW - 100)
        x0, y0, x1, y1 = d.textbbox((VW / 2, cy), text, font=f, anchor='mm')
        d.rectangle((x0 - 22, y0 - 14, x1 + 22, y1 + 14), fill='#FFD400')
        d.text((VW / 2, cy), text, font=f, anchor='mm', fill='#111111')
    im.save(path)


def speak_minimax(text, voice_id, emotion, path, speed):
    import requests
    ensure_key()
    sys.path.insert(0, str(SKILL / 'engine'))
    from lib.common import API_BASE, auth_headers
    data = {'model': 'speech-2.8-hd', 'text': text, 'stream': False, 'language_boost': 'English', 'output_format': 'hex',
            'voice_setting': {'voice_id': voice_id, 'speed': speed, 'vol': 1, 'pitch': 0, 'emotion': emotion},
            'audio_setting': {'sample_rate': 32000, 'bitrate': 128000, 'format': 'mp3', 'channel': 1}}
    r = requests.post(f'{API_BASE}/v1/t2a_v2', headers=auth_headers(), json=data, timeout=120)
    body = r.json() if r.content else {}
    audio = (body.get('data') or {}).get('audio')
    if r.status_code != 200 or body.get('base_resp', {}).get('status_code') != 0 or not audio:
        raise SystemExit(f"[停止] 音声を作れない: HTTP {r.status_code} {body.get('base_resp')}")
    Path(path).write_bytes(bytes.fromhex(audio))


def cmd_assemble(a):
    p = P(a.project)
    d = p.data()
    d['voice'], d['telop'] = a.voice or d.get('voice'), a.telop        # remembered for the next confirm ("前回")
    save(p.yaml, d)
    # talk cuts (cut YAML `talk: true`): the person speaks the lines in the clip -> keep the clip's voice, no voice-over there.
    # other cuts: silent picture + the chosen voice (A/B/C) laid over.
    talk_cuts = {c['n'] for c in d['cuts'] if p.cut(c['n']).get('talk')}
    # 1) pieces: each cut keeps the reference cut length, taken from the moving part of its clip (use.in)
    parts = []
    for c in d['cuts']:
        n, length = c['n'], round(c['end'] - c['start'], 4)
        cut = p.cut(n)
        talk = n in talk_cuts
        clip = next((p.clips / f'{k}.mp4' for k in (clip_key(n, talk, a.tag), clip_key(n, talk)) if (p.clips / f'{k}.mp4').exists()), None)
        if clip is None:
            raise SystemExit(f"[停止] cut {n}: 映像がない（{clip_key(n, talk)}.mp4）。{'話す型なので order で注文する' if talk else 'order で注文する'}")
        inp = float((cut.get('use') or {}).get('in', 0))
        if inp + length > dur(clip) + 0.01:
            log('WARN', f'cut {n}: in {inp}+{length}s is longer than the clip ({dur(clip):.2f}s); the end will be short')
        seg = p.root / 'work' / f'seg-{n}.mp4'
        vf = ['-vf', f'scale={VW}:{VH},fps=30,setsar=1', '-c:v', 'libx264', '-crf', 16, '-pix_fmt', 'yuv420p']
        aenc = ['-ar', 48000, '-ac', 2, '-c:a', 'aac', '-b:a', '192k', '-t', length]
        if talk:
            for where, t in (('start', inp), ('end', inp + length)):   # a talk cut must not start or end in the middle of a word
                lv = level_db(clip, max(0, t - 0.08), 0.08) if where == 'end' else level_db(clip, t, 0.08)
                if lv > -35:
                    log('WARN', f'cut {n}: voice is still sounding at the {where} of the cut ({lv:.0f} dBFS) -> words get cut off. '
                                'Move the cut boundary in project.yaml (keep the total length) or change use.in')
            # no loudnorm here: loudnorm together with a pad filter and video never finishes (2026-11-21); it runs on the joined voice below
            ff('-ss', inp, '-t', length, '-i', clip, *vf, '-af', f'aresample=48000,apad=whole_dur={length}', *aenc, seg)
        else:
            ff('-ss', inp, '-t', length, '-i', clip, '-f', 'lavfi', '-t', length, '-i', 'anullsrc=r=48000:cl=stereo',
               '-map', '0:v', '-map', '1:a', *vf, *aenc, seg)
        parts.append(seg)
    lst = p.root / 'work' / 'seg-list.txt'
    lst.write_text(''.join(f"file '{x.as_posix()}'\n" for x in parts), encoding='utf-8')
    joined = p.root / 'work' / 'joined.mp4'
    ff('-f', 'concat', '-safe', 0, '-i', lst, '-c', 'copy', joined)
    end = round(sum(c['end'] - c['start'] for c in d['cuts']), 3)
    clip_wav = p.root / 'work' / 'joined-voice.wav'   # the talk cuts' own voice (silence in the other cuts)
    ff('-i', joined, '-vn', *(['-af', 'loudnorm=I=-16:TP=-1.5'] if talk_cuts else []), '-ar', 48000, '-ac', 2, clip_wav)
    # 1b) product images shown on top of the video (official images only), same place and time as in the reference
    #     project.yaml overlays: [{file, start, end, cx_pct, cy_pct, w_pct}]  (center x/y and width in % of the frame)
    ovs = [o for o in d.get('overlays') or [] if (p.root / o['file']).exists()]
    if ovs:
        ins, fc, v = ['-i', str(joined)], [], '0:v'
        for i, o in enumerate(ovs):
            ins += ['-loop', '1', '-t', str(end + 1), '-i', str(p.root / o['file'])]
            w = int(VW * o['w_pct'] / 100) // 2 * 2
            fc.append(f"[{i + 1}:v]scale={w}:-2,format=rgba[o{i}];[{v}][o{i}]overlay=x={VW * o['cx_pct'] / 100:.0f}-w/2:"
                      f"y={VH * o['cy_pct'] / 100:.0f}-h/2:enable='between(t,{o['start']},{o['end']})'[w{i}]")
            v = f'w{i}'
        withov = p.root / 'work' / 'joined-overlays.mp4'
        ff(*ins, '-filter_complex', ';'.join(fc), '-map', f'[{v}]', '-t', end, '-c:v', 'libx264', '-crf', 16, '-pix_fmt', 'yuv420p', withov)
        joined = withov
    missing = [o['file'] for o in d.get('overlays') or [] if not (p.root / o['file']).exists()]
    if missing:
        log('WARN', f'overlay images not found (skipped): {missing}')
    # 2) voice
    vj = json.loads((ASSETS / 'voices' / 'voices.json').read_text(encoding='utf-8'))
    tj = json.loads((ASSETS / 'telop' / 'telop-styles.json').read_text(encoding='utf-8'))
    lines = [x for x in d['lines'] if x.get('caption') or x.get('spoken')]
    over = [i for i, ln in enumerate(lines) if ln.get('spoken') and line_cut(d, ln) not in talk_cuts]   # lines that need a voice-over
    if over and not a.voice:
        raise SystemExit('[停止] 話さないカットにセリフがあるので --voice A/B/C が要る')
    v = next((x for x in vj['voices'] if x['key'] == a.voice), None)
    inputs, filt, mix, vin = ['-i', str(joined)], [], [], '0:v'
    voice_files = {}
    for i, ln in enumerate(lines):
        png = p.telop / f'{a.telop}-{i}.png'
        telop_png(a.telop, ln['caption'], ln.get('accent', ''), png, ln.get('y', tj.get('position_y', 0.7)))
        inputs += ['-loop', '1', '-t', str(end + 1), '-i', str(png)]
        if i not in over:
            continue
        h = hashlib.sha256(f"{a.voice_engine}|{v['voice_id']}|{ln.get('emotion')}|{ln['spoken']}".encode()).hexdigest()[:12]
        mp3 = p.voice / f'{h}.mp3'
        if not mp3.exists():
            if a.voice_engine == 'minimax':
                speak_minimax(ln['spoken'], v['voice_id'], ln.get('emotion', 'happy'), mp3, vj.get('speed', 1.1))
            else:  # free test voice
                import edge_tts
                asyncio.run(edge_tts.Communicate(ln['spoken'], 'en-US-AndrewNeural', rate='+8%').save(str(mp3)))
        wav = p.voice / f'{h}.wav'
        ff('-i', mp3, '-af', 'silenceremove=start_periods=1:start_threshold=-45dB,areverse,silenceremove=start_periods=1:start_threshold=-45dB,areverse,apad=pad_dur=0.05',
           '-ar', 48000, wav)
        voice_files[i] = wav
    idx = 1
    for i, ln in enumerate(lines):
        filt.append(f"[{idx}:v]format=rgba[c{i}];[{vin}][c{i}]overlay=0:0:enable='between(t,{ln['start']},{ln['end']})'[v{i}]")
        vin, idx = f'v{i}', idx + 1
    inputs += ['-i', str(clip_wav)]
    mix.append(f'[{idx}:a]aresample=48000,apad,atrim=0:{end}[base]')
    idx += 1
    for i, ln in enumerate(lines):
        if i not in voice_files:
            continue
        slot = (lines[i + 1]['voice_start'] if i + 1 < len(lines) else end) - ln['voice_start'] - 0.1
        dd = dur(voice_files[i])
        tempo = max(1.0, min(1.35, dd / slot)) if slot > 0 else 1.0
        if dd / max(slot, 0.01) > 1.35:
            log('WARN', f"line {i + 1}: voice {dd:.2f}s does not fit {slot:.2f}s even at 1.35x; shorten the line or move voice_start")
        inputs += ['-i', str(voice_files[i])]
        mix.append(f"[{idx}:a]atempo={tempo:.3f},aresample=48000,adelay={int(ln['voice_start'] * 1000)}:all=1,volume=1.6[s{i}]")
        idx += 1
    mix.append('[base]' + ''.join(f'[s{i}]' for i in voice_files) + f'amix=inputs={len(voice_files) + 1}:normalize=0:duration=first,alimiter=limit=0.95[a]')
    out = p.out / 'final.mp4'
    vmap = vin if vin == '0:v' else f'[{vin}]'   # no captions at all -> the plain input stream, written without brackets
    ff(*inputs, '-filter_complex', ';'.join(filt + mix), '-map', vmap, '-map', '[a]', '-t', end,
       '-c:v', 'libx264', '-crf', 18, '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', out)
    ref = p.root / d['reference']['file']
    cmp_ = p.out / 'compare-2up.mp4'
    ff('-i', ref, '-i', out, '-filter_complex', '[0:v]scale=540:960,setsar=1,tpad=stop_mode=clone:stop_duration=1[a];[1:v]scale=540:960,setsar=1[b];[a][b]hstack=shortest=1[v]',
       '-map', '[v]', '-map', '1:a', '-c:v', 'libx264', '-crf', 20, '-pix_fmt', 'yuv420p', '-c:a', 'aac', cmp_)
    log('OK', f'{out} ({dur(out):.2f}s) / {cmp_}')
    if a.check:
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel('large-v3', device='cuda')
        except Exception:  # no NVIDIA GPU (e.g. Mac): CPU with a smaller model
            model = WhisperModel('small', device='cpu', compute_type='int8')
        segs, _ = model.transcribe(str(out), language='en')
        heard = ' '.join(s.text.strip() for s in segs)
        log('CHECK', heard)


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest='cmd', required=True)
    x = sp.add_parser('init'); x.add_argument('project'); x.add_argument('--url'); x.add_argument('--file'); x.add_argument('--look', required=True)
    x = sp.add_parser('frames'); x.add_argument('project')
    x = sp.add_parser('stills'); x.add_argument('project'); x.add_argument('--cut', type=int); x.add_argument('--tries', type=int, default=3)
    x.add_argument('--min-color', type=float, default=0.5); x.add_argument('--min-inliers', type=int, default=8)
    x = sp.add_parser('confirm'); x.add_argument('project')
    x = sp.add_parser('order'); x.add_argument('project'); x.add_argument('--cap', type=int, required=True); x.add_argument('--cut', type=int); x.add_argument('--tag', default='')
    x.add_argument('--talk', action='store_true', help='the person speaks the lines (MiniMax voice + lip sync)')
    x.add_argument('--dry', action='store_true', help='print the prompts only (no order, no cost)')
    x = sp.add_parser('assemble'); x.add_argument('project'); x.add_argument('--voice', choices='ABC', help='voice-over for cuts that are not talk cuts'); x.add_argument('--telop', required=True, choices='ABC')
    x.add_argument('--voice-engine', default='minimax', choices=['minimax', 'edge']); x.add_argument('--tag', default=''); x.add_argument('--check', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'init' and not (a.url or a.file):
        ap.error('--url か --file が必要')
    globals()[f'cmd_{a.cmd}'](a)


if __name__ == '__main__':
    main()
