"""Proposal deck generator: fills Proposal_Template_v2.pptx from structured input.

Usage:  python generate.py input.json output.pptx
All image paths in the input are relative to the input file's folder.
"""
import copy
import json
import os
import subprocess
import sys
import tempfile

from lxml import etree
from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn
from pptx.enum.shapes import MSO_SHAPE
from pptx.util import Emu, Pt

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "Proposal_Template_v2.pptx")
NS = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}

# Template layout indexes (by position in the template's layout list)
L_COVER, L_VISION, L_MATERIALS, L_FLOORPLAN, L_DIVIDER, L_FULL, _, L_QUOTE, L_DATES, L_TERMS1, L_TERMS2, L_END = range(12)

# Template slide indexes (0-based) that hold tables we clone
T_QUOTE_SLIDE, T_DATES_SLIDE = 9, 10
N_TEMPLATE_SLIDES = 14

SLIDE_W, SLIDE_H = 10691813, 7559675
CONTENT_X, CONTENT_Y, CONTENT_W, CONTENT_H = 359999, 1358753, 9957186, 5840921
GAP = 90000


# ---------------------------------------------------------------- images
def to_images(path, outdir=None):
    """Return a list of image paths for a file. PDFs become one JPG per page."""
    if not path.lower().endswith(".pdf"):
        return [path]
    outdir = outdir or tempfile.mkdtemp(prefix="pdfpages_")
    prefix = os.path.join(outdir, os.path.splitext(os.path.basename(path))[0])
    subprocess.run(["pdftoppm", "-jpeg", "-r", "150", "-l", "60", path, prefix], check=True)  # max 60 pages
    return sorted(
        os.path.join(outdir, f) for f in os.listdir(outdir)
        if f.startswith(os.path.basename(prefix)) and f.endswith(".jpg")
    )


def add_cover(slide, path, x, y, w, h, focus=(0.5, 0.5), zoom=1.0):
    """Place image filling the box, cropping the overflow (like CSS cover). focus says which part stays in view."""
    iw, ih = Image.open(path).size
    pic = slide.shapes.add_picture(path, x, y, w, h)
    box, img = w / h, iw / ih
    hs = max(1.0, box / img) * max(1.0, zoom)   # the picture's height when it fills the box, then zoomed (box height = 1)
    wf, hf = box / (hs * img), 1 / hs            # the share of the picture that shows
    pic.crop_left, pic.crop_right = (1 - wf) * focus[0], (1 - wf) * (1 - focus[0])
    pic.crop_top, pic.crop_bottom = (1 - hf) * focus[1], (1 - hf) * (1 - focus[1])
    return pic


def add_contain(slide, path, x, y, w, h, valign="center"):
    """Place image fully visible inside the box, centred, keeping aspect."""
    iw, ih = Image.open(path).size
    s = min(w / iw, h / ih)
    pw, ph = int(iw * s), int(ih * s)
    py = y + (h - ph) // 2 if valign == "center" else y + h - ph
    return slide.shapes.add_picture(path, x + (w - pw) // 2, py, pw, ph)


# ---------------------------------------------------------------- slide helpers
def new_slide(prs, layout_idx):
    return prs.slides.add_slide(prs.slide_layouts[layout_idx])


def ph(slide, idx):
    for p in slide.placeholders:
        if p.placeholder_format.idx == idx:
            return p
    return None


def strip_empty_placeholders(slide):
    for p in list(slide.placeholders):
        if p.has_text_frame and p.text_frame.text.strip():
            continue
        p._element.getparent().remove(p._element)


def set_lines(placeholder, lines):
    tf = placeholder.text_frame
    tf.text = lines[0]
    for line in lines[1:]:
        tf.add_paragraph().text = line


def money(value, cur):
    return f"{'-' if value < 0 else ''}{cur}{abs(value):,.2f}"


def set_cell(cell, text, bold=None, size=None):
    """Set a table cell's first paragraph text, keeping the template formatting."""
    p = cell.text_frame.paragraphs[0]._p
    runs = p.findall("a:r", NS)
    if not runs:
        end = p.find("a:endParaRPr", NS)
        r = etree.SubElement(p, f"{{{NS['a']}}}r")
        rpr = etree.SubElement(r, f"{{{NS['a']}}}rPr")
        if end is not None:
            for k, v in end.attrib.items():
                rpr.set(k, v)
            for child in end:
                rpr.append(copy.deepcopy(child))
            p.remove(end)
        etree.SubElement(r, f"{{{NS['a']}}}t")
        runs = [r]
    for extra in runs[1:]:
        p.remove(extra)
    r = runs[0]
    r.find("a:t", NS).text = text
    rpr = r.find("a:rPr", NS)
    if rpr.find("a:solidFill", NS) is None:  # make the text colour explicit (tx1)
        fill = etree.Element(f"{{{NS['a']}}}solidFill")
        etree.SubElement(fill, f"{{{NS['a']}}}schemeClr").set("val", "tx1")
        rpr.insert(0, fill)
    if bold is not None:
        rpr.set("b", "1" if bold else "0")
    if size:
        rpr.set("sz", str(size))
    # remove any additional paragraphs (descriptions) when we override a cell
    for extra_p in cell.text_frame.paragraphs[1:]:
        extra_p._p.getparent().remove(extra_p._p)


def template_shapes(prs_tpl_slide_idx, prs):
    return [s for s in prs.slides[prs_tpl_slide_idx].shapes if s.has_table]


def clone_table(prs, tpl_slide_idx, table_idx, dest_slide):
    src = template_shapes(tpl_slide_idx, prs)[table_idx]
    el = copy.deepcopy(src._element)
    dest_slide.shapes._spTree.append(el)
    return [s for s in dest_slide.shapes if s.has_table][-1]


# ---------------------------------------------------------------- pages
def page_cover(prs, data):
    s = new_slide(prs, L_COVER)
    box = (3631406, 3179761, 3429000, 1200152)
    logo = data["client"].get("logo")
    clip = ph(s, 22)
    if clip is not None:
        clip._element.getparent().remove(clip._element)
    if logo:
        add_contain(s, logo, *box)
    else:
        tb = s.shapes.add_textbox(*box)
        tb.text_frame.text = data["client"]["name"]
    lines = []
    for show in data["shows"]:
        lines.append(show.get("cover_line") or f"{show['name']}, {show['location']} ({show['dates']})")
    set_lines(ph(s, 11), lines)
    strip_empty_placeholders(s)


MOOD_PRESETS = {  # boxes in px of a 1308x924 frame (the example's proportions); later boxes sit on top
    3: [(138, 167, 610, 243), (760, 167, 478, 420), (292, 422, 456, 371)],
    4: [(138, 167, 610, 243), (760, 167, 478, 420), (50, 422, 231, 371), (292, 422, 456, 371)],
    5: [(138, 167, 610, 243), (760, 167, 478, 380), (50, 422, 231, 371), (292, 422, 400, 371), (711, 530, 210, 330)],
    6: [(138, 167, 610, 243), (760, 167, 478, 380), (50, 422, 231, 371), (292, 422, 400, 371), (711, 530, 210, 330), (940, 610, 300, 230)],
}


def _assign(images, boxes):
    """Give each slot the image whose shape needs the least cropping."""
    import itertools
    import math
    ars = [Image.open(p).size[0] / Image.open(p).size[1] for p in images]
    sl = [w / h for (_, _, w, h) in boxes]
    best = min(itertools.permutations(range(len(images))),
               key=lambda perm: sum(abs(math.log(ars[i] / sl[k])) for k, i in enumerate(perm)))
    return [images[i] for i in best]


MOOD_TWO = {  # one or two dropped-in pictures, in px of the 1308x924 frame
    1: [(0, 203, 488, 721)],
    2: [(0, 203, 488, 721), (518, 203, 486, 721)],   # two upright pictures from the left edge down to the bottom edge
}


def page_mood_board(prs, images, views=None):
    s = new_slide(prs, L_VISION)
    images = images[:6]
    if len(images) <= 2:  # pictures dropped in by the salesperson: one big frame each
        boxes = MOOD_TWO[len(images)]
    else:  # a library-picked mood board
        boxes = MOOD_PRESETS[min(len(images), 6)]
    k = SLIDE_W / 1308
    dropped = len(images) <= 2   # the salesperson's own pictures stay in the order given, each with its own position and zoom
    for n, (path, (x, y, w, h)) in enumerate(zip(images if dropped else _assign(images, boxes), boxes)):
        v = list((views or [])[n]) + [1.0] if dropped and views and n < len(views) else [0.5, 0.5, 1.0]
        add_cover(s, path, int(x * k), int(y * k), int(w * k), int(h * k), (v[0], v[1]), zoom=v[2])
    strip_empty_placeholders(s)


FURN_REGION = (3.3e6, 10.5e6)   # left and right limits (EMU) of the furniture area: it stands in front of the flooring and the feature wall
LIGHT_REGION = (6.2e6, 10.0e6)  # where the lights hang
FURN_BASELINE, FURN_MAX_H, FURN_GAP = 6.95e6, 4.6e6, -0.10e6
MAX_SCALE = 30000   # EMU per cm: stops one small piece being blown up to fill the space


def _furn_entry(f):
    return f if isinstance(f, dict) else {"path": f, "height_cm": 80}


def layout_furniture(items, region=FURN_REGION, baseline=FURN_BASELINE, max_h=FURN_MAX_H, gap=FURN_GAP):
    """Positions for furniture pieces so they are drawn to the SAME real-world scale on one floor line.

    items: [{"path", "height_cm"}]. Returns ([(path, x, y, w, h)] in EMU, EMU-per-cm scale)."""
    ents = []
    for f in items:
        f = _furn_entry(f)
        iw, ih = Image.open(f["path"]).size
        hc = max(10.0, float(f.get("height_cm") or 80))
        ents.append((f["path"], hc, hc * iw / ih))  # path, height in cm, width in cm
    if not ents:
        return [], MAX_SCALE * 0.7
    width = region[1] - region[0] - gap * (len(ents) - 1)
    scale = min(max_h / max(e[1] for e in ents), width / sum(e[2] for e in ents), MAX_SCALE)  # EMU per cm
    total = sum(e[2] for e in ents) * scale + gap * (len(ents) - 1)
    x = region[0] + (region[1] - region[0] - total) / 2
    out = []
    for path, hc, wc in ents:
        w, h = wc * scale, hc * scale
        out.append((path, int(x), int(baseline - h), int(w), int(h)))
        x += w + gap
    return out, scale


def layout_lighting(items, scale, region=LIGHT_REGION):
    """Lights hang from the very top edge of the page (the picture itself includes the cable and ceiling cap),
    drawn to the same scale as the furniture. Returns [(path, x, y, w, h)] in EMU."""
    ents = []
    for f in items:
        f = _furn_entry(f)
        iw, ih = Image.open(f["path"]).size
        hc = max(10.0, float(f.get("height_cm") or 75))
        ents.append((f["path"], hc, hc * iw / ih))
    if not ents:
        return []
    slot = (region[1] - region[0]) / len(ents)
    fit = min(1.0, min(slot * 0.95 / (e[2] * scale) for e in ents))  # shrink only if a light is wider than its slot
    out = []
    for i, (path, hc, wc) in enumerate(ents):
        w, h = wc * scale * fit, hc * scale * fit
        cx = region[0] + slot * (i + 0.5)
        out.append((path, int(cx - w / 2), 0, int(w), int(h)))
    return out


MAT_K = SLIDE_W / 1308   # the page is laid out on a 1308-wide grid
MAT_WALL = (886, 0, 1308 - 886, 724)          # feature wall: top right, bleeding off the edges
MAT_FLOOR = (0, 398, 895, 192)                # the long flooring strip
MAT_SWATCH_X, MAT_SWATCH_Y, MAT_SWATCH_W, MAT_SWATCH_H = (47, 256, 466, 676), 202, 199, 265   # the four small upright boxes


MAT_DOT = 720000   # the colour circles: 2 x 2 at the bottom left (EMU, as in the template)
MAT_DOTS = [(379493, 5002045), (1179692, 5002045), (379493, 5815035), (1179692, 5815035)]


def _px(box):
    return tuple(int(v * MAT_K) for v in box)


def materials_boxes(m):
    """Where the furniture and lights go: [(path, id, x, y, w, h)] in EMU. Anything the salesperson moved or
    resized in the layout editor (m["layout"], fractions of the page) wins over the automatic position."""
    lay = m.get("layout") or {}
    fe, le = m.get("furniture", [])[:6], m.get("lighting", [])[:3]
    furn, scale = layout_furniture(fe)
    lights = layout_lighting(le, scale)

    def place(ents, boxes, z0):
        out = []
        for n, (ent, (path, x, y, w, h)) in enumerate(zip(ents, boxes)):
            o = lay.get(ent.get("id")) if isinstance(ent, dict) else None
            z = z0 + n   # lights are in front of furniture unless moved
            if o:
                x, y, w, h = int(o[0] * SLIDE_W), int(o[1] * SLIDE_H), int(o[2] * SLIDE_W), int(o[3] * SLIDE_H)
                if len(o) > 4:
                    z = o[4]
            out.append((path, ent.get("id") if isinstance(ent, dict) else None, x, y, w, h, z))
        return out
    ex = m.get("additional", [])[:8]   # anything else from the library: a neat default spot that is easy to move
    extra = []
    for n, ent in enumerate(ex):
        iw, ih = Image.open(ent["path"]).size
        w = int(0.16 * SLIDE_W)
        h = int(w * ih / iw)
        if h > 0.28 * SLIDE_H:
            h = int(0.28 * SLIDE_H)
            w = int(h * iw / ih)
        extra.append((ent["path"], int((0.30 + (n % 4) * 0.17) * SLIDE_W), int((0.08 + (n // 4) * 0.30) * SLIDE_H), w, h))
    return place(fe, furn, 1) + place(ex, extra, 100), place(le, lights, 50)


def page_materials(prs, m):
    s = new_slide(prs, L_MATERIALS)
    for p in list(s.placeholders):
        p._element.getparent().remove(p._element)
    if m.get("lifestyle"):  # the feature wall (a curtain, slatted wall...)
        x, y, w, h = _px(MAT_WALL)
        wp = list((m.get("layout") or {}).get("wall") or [0.5, 0.5, 1.0]) + [1.0]
        add_cover(s, m["lifestyle"], x, y, SLIDE_W - x, h, (wp[0], wp[1]), zoom=wp[2])
    if m.get("flooring"):  # the long horizontal strip
        add_cover(s, m["flooring"], *_px(MAT_FLOOR))
    for path, x in zip(m.get("swatches", [])[:4], MAT_SWATCH_X):
        add_cover(s, path, int(x * MAT_K), int(MAT_SWATCH_Y * MAT_K), int(MAT_SWATCH_W * MAT_K), int(MAT_SWATCH_H * MAT_K))
    for (x, y), col in zip(MAT_DOTS, m.get("colours", [])[:4]):   # the client's colours, four circles
        dot = s.shapes.add_shape(MSO_SHAPE.OVAL, x, y, MAT_DOT, MAT_DOT)
        dot.fill.solid()
        dot.fill.fore_color.rgb = RGBColor.from_string(col.lstrip("#").upper())
        dot.line.fill.background()
        dot.shadow.inherit = False
    furn, lights = materials_boxes(m)
    lay = m.get("layout") or {}
    for path, _id, x, y, w, h, _z in sorted(furn + lights, key=lambda b: b[6]):   # back to front
        pic = s.shapes.add_picture(path, x, y, w, h)
        o = lay.get(_id) or []
        if len(o) > 5 and o[5]:   # mirrored left-to-right
            pic._element.spPr.find(qn("a:xfrm")).set("flipH", "1")
        if len(o) >= 10:   # trimmed edges
            pic.crop_left, pic.crop_top, pic.crop_right, pic.crop_bottom = o[6], o[7], o[8], o[9]


def page_divider(prs, show):
    s = new_slide(prs, L_DIVIDER)
    ph(s, 10).text_frame.text = f"{show['name']} Booth Design"
    ph(s, 11).text_frame.text = show.get("stand_size", "")
    if show.get("logo"):
        add_contain(s, show["logo"], (SLIDE_W - 3200000) // 2, 5150000, 3200000, 1500000)
    strip_empty_placeholders(s)


# where the floorplan sits on the Booth Location page: central and large, with a margin all round (EMU)
LOC_BOX = (534590, 1358753, 9622633, 5620000)
BOOTH_RED = RGBColor(0xD9, 0x50, 0x46)


def add_floorplan(slide, path, view):
    """The floorplan inside its fixed frame (LOC_BOX), optionally zoomed and moved; whatever falls outside the frame is cut off."""
    fx, fy, fw, fh = LOC_BOX
    z, px, py = view if view else (1.0, 0.0, 0.0)
    if z <= 1.001 and not px and not py:
        return add_contain(slide, path, *LOC_BOX, valign="center")
    iw, ih = Image.open(path).size
    k = min(fw / iw, fh / ih) * z
    pw, ph_ = iw * k, ih * k
    left, top = fx + fw / 2 + px * SLIDE_W - pw / 2, fy + fh / 2 + py * SLIDE_H - ph_ / 2
    l, t, r, b = max(left, fx), max(top, fy), min(left + pw, fx + fw), min(top + ph_, fy + fh)
    if r - l < 1000 or b - t < 1000:
        return None
    im = Image.open(path)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGB")
    crop = im.crop((round((l - left) / k), round((t - top) / k), round((r - left) / k), round((b - top) / k)))
    out = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    crop.save(out)
    pic = slide.shapes.add_picture(out, int(l), int(t), int(r - l), int(b - t))
    os.unlink(out)
    return pic


def page_location(prs, show):
    s = new_slide(prs, L_FLOORPLAN)
    for idx in (12, 13):
        p = ph(s, idx)
        if p is not None:
            p._element.getparent().remove(p._element)
    if show.get("floorplan"):
        add_floorplan(s, show["floorplan"], show.get("floorplan_view"))
    bx = show.get("booth_box")  # the red box the salesperson placed over the booth
    if show.get("floorplan") and bx:
        x, y, w, h = int(bx[0] * SLIDE_W), int(bx[1] * SLIDE_H), int(bx[2] * SLIDE_W), int(bx[3] * SLIDE_H)
        r = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
        r.fill.background()
        r.line.color.rgb = BOOTH_RED
        r.line.width = Pt(5)
        r.shadow.inherit = False


LABEL_FONT_PT = 14
ANNOT_TEXT = RGBColor(0x3A, 0x3A, 0x3A)   # dark grey label text on a white label


def hand_arrow(x1, y1, x2, y2, steps=40):
    """A fine flowing line with a gentle S in it and a small open arrowhead at the end: the editor draws the same curve."""
    import math
    L = math.hypot(x2 - x1, y2 - y1) or 1.0
    ux, uy = (x2 - x1) / L, (y2 - y1) / L
    vx, vy = -uy, ux
    c1 = (x1 + ux * L * 0.3 + vx * L * 0.16, y1 + uy * L * 0.3 + vy * L * 0.16)
    c2 = (x2 - ux * L * 0.3 - vx * L * 0.16, y2 - uy * L * 0.3 - vy * L * 0.16)
    pts = []
    for i in range(steps + 1):
        t = i / steps
        m = 1 - t
        pts.append((int(m ** 3 * x1 + 3 * m * m * t * c1[0] + 3 * m * t * t * c2[0] + t ** 3 * x2),
                    int(m ** 3 * y1 + 3 * m * m * t * c1[1] + 3 * m * t * t * c2[1] + t ** 3 * y2)))
    tx, ty = x2 - c2[0], y2 - c2[1]
    tl = math.hypot(tx, ty) or 1.0
    tx, ty = tx / tl, ty / tl
    hl = min(max(0.2 * L, 0.022 * SLIDE_W), 0.04 * SLIDE_W, L * 0.6)
    a = 0.5
    def back(k):
        return (-tx * math.cos(a) + ty * math.sin(a) * k, -ty * math.cos(a) - tx * math.sin(a) * k)
    lf, rt = back(-1), back(1)
    head = [(int(x2 + lf[0] * hl), int(y2 + lf[1] * hl)), (int(x2), int(y2)), (int(x2 + rt[0] * hl * 0.85), int(y2 + rt[1] * hl * 0.85))]
    return pts, head


def draw_notes(slide, notes):
    """Labels (white box, dark grey text) and arrows (a bright colour) on a full-page picture. Positions are fractions of the page."""
    from pptx.enum.shapes import MSO_CONNECTOR
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
    for n in notes or []:
        if n.get("t") != "arrow":
            continue
        x1, y1, x2, y2 = int(n["x1"] * SLIDE_W), int(n["y1"] * SLIDE_H), int(n["x2"] * SLIDE_W), int(n["y2"] * SLIDE_H)
        if abs(x1 - x2) + abs(y1 - y2) < 20000:
            continue
        pts, head = hand_arrow(x1, y1, x2, y2)
        fb = slide.shapes.build_freeform(pts[0][0], pts[0][1], scale=1.0)
        fb.add_line_segments(pts[1:], close=False)
        fb.move_to(*head[0])
        fb.add_line_segments(head[1:], close=False)
        shp = fb.convert_to_shape()
        shp.fill.background()
        shp.line.color.rgb = RGBColor.from_string(n["c"].lstrip("#").upper())
        shp.line.width = Pt(1.5)
        lnxml = shp.line._get_or_add_ln()
        lnxml.set("cap", "rnd")
        etree.SubElement(lnxml, qn("a:round"))
        shp.shadow.inherit = False
    for n in notes or []:
        if n.get("t") != "label":
            continue
        lines = str(n["text"]).split("\n")
        size = LABEL_FONT_PT
        w = int((max(len(t) for t in lines) * size * 0.60 + 2 * 4) * 12700)
        h = int((len(lines) * size * 1.22 + 2 * 2) * 12700)
        x = min(int(n["x"] * SLIDE_W), SLIDE_W - w)
        y = min(int(n["y"] * SLIDE_H), SLIDE_H - h)
        box = slide.shapes.add_textbox(x, y, w, h)
        box.fill.background()   # no box: just the words
        box.line.fill.background()
        txt_rgb = RGBColor.from_string(str(n.get("c") or "#000000").lstrip("#").upper())
        tf = box.text_frame
        tf.word_wrap = False
        tf.vertical_anchor = MSO_ANCHOR.MIDDLE
        tf.margin_left = tf.margin_right = Pt(4)
        tf.margin_top = tf.margin_bottom = Pt(2)
        for k, t in enumerate(lines):
            para = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
            para.alignment = PP_ALIGN.CENTER
            run = para.add_run()
            run.text = t
            run.font.size = Pt(size)
            run.font.color.rgb = txt_rgb
            run.font._element.set('spc', '110')   # a little space between the letters


def page_full_image(prs, path, label="Render / drawing", notes=None):
    s = new_slide(prs, L_FULL)
    p = ph(s, 10)
    pic = p.insert_picture(path)  # crops to fill the full-bleed placeholder
    pic.name = "label: " + label   # shown on the page-order screen
    if notes:
        draw_notes(s, notes)


def _drop_rows(shape, keep):
    """Remove table rows for which keep(row_index) is False, and shrink the frame to fit."""
    tbl = shape.table._tbl
    # the table style gives the first row special (white) text; the template relies on
    # its first row having explicit colours, so switch the header styling off
    pr = tbl.find("a:tblPr", NS)
    if pr is not None:
        pr.set("firstRow", "0")
        pr.set("bandRow", "0")
    rows = tbl.tr_lst
    removed = 0
    for i, tr in enumerate(rows):
        if not keep(i):
            removed += int(tr.get("h") or 0)
            tbl.remove(tr)
    shape.height = Emu(max(int(shape.height) - removed, 1))


def _set_name_and_description(cell, name, desc, size):
    """First paragraph = bold name; second = description (or removed when there isn't one)."""
    paras = cell.text_frame.paragraphs
    p0 = paras[0]._p
    r0 = p0.findall("a:r", NS)[0]
    for extra in p0.findall("a:r", NS)[1:]:
        p0.remove(extra)
    r0.find("a:t", NS).text = name
    r0.find("a:rPr", NS).set("sz", str(size))
    if len(paras) > 1:
        p1 = paras[1]._p
        if not desc:
            p1.getparent().remove(p1)
            return
        runs = p1.findall("a:r", NS)
        for extra in runs[1:]:
            p1.remove(extra)
        runs[0].find("a:t", NS).text = desc
        runs[0].find("a:rPr", NS).set("sz", str(size))


def _signed(sign, v, cur):
    return f"{'-' if sign == '-' else '+'}{money(abs(v), cur)}"


# ---- the Quotation page: two columns of lines, then the discount, the boxed-free total and the options
QUOTE_LINES = {
    "rental_structure": ("Rental Structure", "Premium Aluvision modular aluminium framework system"),
    "graphics": ("Graphics", "High-quality, dye-sublimation printed fabric graphics."),
    "custom_rental": ("Custom Rental", None),
    "flooring": ("Flooring", None),
    "audio_visual": ("Audio Visual", None),
    "av_furniture": ("AV & Furniture", None),      # proposals made before Audio Visual and Furniture were separate lines
    "furniture": ("Furniture", None),
    "installation_dismantle": ("Installation & Dismantle", "Including pre-show preparation, stand build and breakdown, labour, travel costs and post show-services"),
    "project_management": ("Project Management", "Including Stand Design Fee, Scheduling, Risk Assessment and Stand Plan Submissions, Construction Phase Plans, Technical CAD drawings and Graphic templates and On-site Project Management"),
    "logistics_handling": ("Logistics & Handling", "Transport and on-site logistics of stand to and from venue including forklift, pallet truck and storage of crates during event"),
}
QUOTE_LEFT = ["rental_structure", "graphics", "custom_rental", "flooring", "audio_visual", "av_furniture", "furniture"]
QUOTE_RIGHT = ["installation_dismantle", "project_management", "logistics_handling"]
Q_X_LEFT, Q_X_RIGHT, Q_COL_W, Q_AMT_W, Q_TOP = 359998, 5540000, 4790000, 1250000, 1377280
Q_AVAIL_PT = 440.0     # points of height a column may use (the page is 595pt tall, the table starts 108pt down)
_GREY = '<a:solidFill><a:schemeClr val="bg1"><a:lumMod val="85000"/></a:schemeClr></a:solidFill>'
_INK = '<a:solidFill><a:schemeClr val="tx1"/></a:solidFill>'


def _xml_esc(t):
    return str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _q_run(text, sz, bold):
    return (f'<a:r><a:rPr lang="en-GB" sz="{sz}" b="{1 if bold else 0}" dirty="0">{_INK}'
            f'<a:latin typeface="Europa-Regular"/></a:rPr><a:t>{_xml_esc(text)}</a:t></a:r>')


def _q_para(runs, algn="l", bullet=False, end_sz=1200):
    ppr = (f'<a:pPr marL="{165100 if bullet else 0}" indent="{-165100 if bullet else 0}" algn="{algn}"><a:lnSpc><a:spcPct val="100000"/></a:lnSpc>'
           '<a:spcBef><a:spcPts val="0"/></a:spcBef><a:spcAft><a:spcPts val="0"/></a:spcAft>'
           + ('<a:buFont typeface="Arial"/><a:buChar char="&#8226;"/>' if bullet else '<a:buNone/>') + '</a:pPr>')
    return f'<a:p>{ppr}{"".join(runs)}<a:endParaRPr lang="en-GB" sz="{end_sz}" dirty="0"/></a:p>'


def _q_line(tag, kind):
    if kind == "none":
        return f'<a:{tag} w="12700"><a:noFill/></a:{tag}>'
    fill, w = (_GREY, 12700) if kind == "grey" else (_INK, 19050)
    return f'<a:{tag} w="{w}" cap="flat" cmpd="sng" algn="ctr">{fill}<a:prstDash val="solid"/></a:{tag}>'


def _q_cell(paras, top, bottom, mar, anchor="t"):
    return (f'<a:tc><a:txBody><a:bodyPr/><a:lstStyle/>{"".join(paras)}</a:txBody>'
            f'<a:tcPr marL="0" marR="0" marT="{mar}" marB="{mar}" anchor="{anchor}">{_q_line("lnL", "none")}{_q_line("lnR", "none")}'
            f'{_q_line("lnT", top)}{_q_line("lnB", bottom)}<a:noFill/></a:tcPr></a:tc>')


def _q_row(left_paras, right_paras, h, top="grey", bottom="grey", mar=108000, anchor="t"):
    xml = (f'<a:tr xmlns:a="{NS["a"]}" h="{int(h)}">{_q_cell(left_paras, top, bottom, mar, anchor)}'
           f'{_q_cell(right_paras, top, bottom, mar, anchor)}</a:tr>')
    return etree.fromstring(xml)


def _q_lines_needed(text, size_pt, width_pt):
    per = max(int(width_pt / (size_pt * 0.5)), 8)
    return max(1, -(-len(text) // per))


def _q_item_rows(items, cur, size):
    """items: [(name, [description lines], bullets?, amount)] -> (row elements, estimated height in points)."""
    sz = int(size * 100)
    mar = int(108000 * size / 12)
    name_w = (Q_COL_W - Q_AMT_W) / 12700
    rows, total = [], 0.0
    for name, desc, bullets, amount in items:
        paras = [_q_para([_q_run(name, sz, True)], end_sz=sz)]
        n = 1
        for d in desc:
            paras.append(_q_para([_q_run(d, sz, False)], bullet=bullets, end_sz=sz))
            n += _q_lines_needed(d, size, name_w - (13 if bullets else 0))
        h_pt = n * size * 1.22 + 2 * mar / 12700
        rows.append(_q_row(paras, [_q_para([_q_run(money(amount, cur), sz, True)], algn="r", end_sz=sz)], h_pt * 12700, mar=mar))
        total += h_pt
    return rows, total


def _q_extra_rows(discount, total, options, cur, size):
    """The discount (when there is one), the total between two lines, then the options. Returns (rows, estimated height)."""
    sz = int(size * 100)
    mar = int(108000 * size / 12)
    rh = (size * 1.22) * 12700 + 2 * mar
    rows, est = [], 0.0
    if discount:
        rows.append(_q_row([_q_para([_q_run("Discount", sz, True)], end_sz=sz)], [_q_para([_q_run(money(-discount, cur), sz, True)], algn="r", end_sz=sz)], rh, mar=mar))
        est += rh / 12700
    rows.append(_q_row([_q_para([_q_run("Quotation Total (excluding VAT)", sz, True)], end_sz=sz)],
                       [_q_para([_q_run(money(total, cur), sz, True)], algn="r", end_sz=sz)], rh * 1.1, top="ink", bottom="ink", mar=mar, anchor="ctr"))
    est += rh * 1.1 / 12700
    if options:
        rows.append(_q_row([_q_para([], end_sz=600)], [_q_para([], end_sz=600)], 200000, top="none", bottom="none", mar=0))
        est += 200000 / 12700
        for i, o in enumerate(options, 1):
            val = "" if o.get("amount") is None else _signed(o.get("sign"), o["amount"], cur)
            rows.append(_q_row([_q_para([_q_run(f"Option {i}: ", sz, True), _q_run(o["text"], sz, False)], end_sz=sz)],
                               [_q_para([_q_run(val, sz, False)] if val else [], algn="r", end_sz=sz)], rh, top="none", bottom="grey", mar=mar))
            est += rh / 12700
    return rows, est


def _q_table(prs, slide, x, rows, est_pt):
    shape = clone_table(prs, T_QUOTE_SLIDE, 0, slide)
    tb = shape.table
    tbl = tb._tbl
    for tr in list(tbl.tr_lst):
        tbl.remove(tr)
    pr = tbl.find("a:tblPr", NS)
    if pr is not None:
        pr.set("firstRow", "0")
        pr.set("bandRow", "0")
    for r in rows:
        tbl.append(r)
    tb.columns[0].width, tb.columns[1].width = Emu(Q_COL_W - Q_AMT_W), Emu(Q_AMT_W)
    shape.left, shape.top, shape.width, shape.height = Emu(x), Emu(Q_TOP), Emu(Q_COL_W), Emu(int(est_pt * 12700))


def page_quote(prs, q, cur):
    """One show's quotation. q: {"lines": {...}, "details": {key: [bullets]}} or {"custom": [...]}, plus "discount" and "options"."""
    if q.get("custom"):
        rows = q["custom"]
        items = [(r["label"], [r["description"]] if r.get("description") else [], False, r["amount"]) for r in rows]
        subtotal = sum(r["amount"] for r in rows)
        half = -(-len(items) // 2)
        left_items, right_items = items[:half], items[half:]
    else:
        lines, details = q["lines"], q.get("details") or {}
        def item(k):
            name, fixed = QUOTE_LINES[k]
            return (name, [fixed] if fixed else list(details.get(k) or []), not fixed, lines[k])
        left_items = [item(k) for k in QUOTE_LEFT if lines.get(k) is not None]
        right_items = [item(k) for k in QUOTE_RIGHT if lines.get(k) is not None]
        subtotal = sum(v for v in lines.values() if v is not None)
    discount = _quote_extras(q, subtotal)
    total = subtotal - discount
    options = q.get("options") or []
    for size in (12, 11, 10, 9, 8):          # the largest text that keeps both columns on the page
        left_rows, left_h = _q_item_rows(left_items, cur, size)
        right_item_rows, right_h = _q_item_rows(right_items, cur, size)
        extra_rows, extra_h = _q_extra_rows(discount, total, options, cur, size)
        if max(left_h, right_h + extra_h) <= Q_AVAIL_PT:
            break
    s = new_slide(prs, L_QUOTE)
    if left_rows:
        _q_table(prs, s, Q_X_LEFT, left_rows, left_h)
    _q_table(prs, s, Q_X_RIGHT, right_item_rows + extra_rows, right_h + extra_h)
    return total


def _quote_extras(q, subtotal):
    disc = q.get("discount") or 0
    return min(disc, subtotal) if disc > 0 else 0


def page_summary(prs, lines, cur, total_label="Total for all shows (excluding VAT)"):
    """One line per show, then a boxed total. lines: [(label, amount)]. Returns the grand total."""
    s = new_slide(prs, L_QUOTE)
    tbl_shape = clone_table(prs, T_QUOTE_SLIDE, 0, s)
    total_row_src = copy.deepcopy(template_shapes(T_QUOTE_SLIDE, prs)[1].table._tbl.tr_lst[3])
    tbl = tbl_shape.table
    tbl_el = tbl._tbl
    plain_row = copy.deepcopy(tbl_el.tr_lst[1])
    for tr in list(tbl_el.tr_lst):
        tbl_el.remove(tr)
    grand = sum(a for _, a in lines)
    rows = [(label, money(amount, cur), "item") for label, amount in lines] + [(total_label, money(grand, cur), "grand")]
    n = len(rows)
    rh = int(min(520000, (SLIDE_H - 1377280 - 400000) / n))
    for label, amt, kind in rows:
        tr = copy.deepcopy(total_row_src if kind == "grand" else plain_row)
        tbl_el.append(tr)
        row = tbl.rows[len(tbl.rows) - 1]
        row.height = Emu(rh)
        set_cell(row.cells[0], label, bold=(kind == "grand"), size=1300)
        set_cell(row.cells[1], amt, bold=(kind == "grand"), size=1300)
    tbl.columns[0].width = Emu(7600000)
    tbl.columns[1].width = Emu(2350000)
    tbl_shape.width = Emu(9950000)
    tbl_shape.height = Emu(rh * n)
    return grand


HERE = os.path.dirname(os.path.abspath(__file__))
PAGE_DIR = HERE


def fixed_page(prs, name, label=None):
    """A finished page supplied as a full-page picture (the designed Critical Dates, Payment Terms and End Card)."""
    path = os.path.join(PAGE_DIR, name)
    page_full_image(prs, path, label or name)
    return prs.slides[len(prs.slides) - 1]


# vertical position (points from the top of the page) of each date row's first line on the Critical Dates page
DATE_ROWS = [("show_start", 108.2), ("concept_signoff", 156.0), ("quote_signoff", 209.3), ("artwork_deadline", 262.5),
             ("rush_after", 315.8), ("preshow_prep", 361.9), ("dispatch", 416.6)]


def page_dates(prs, d):
    from pptx.dml.color import RGBColor
    s = fixed_page(prs, "page_critical_dates-1.png", "Critical Dates")
    # the page picture still says "Final quotation sign off & submit all information to organiser": cover it and write the shorter wording
    cover = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Emu(int(424 * 12700)), Emu(int(198 * 12700)), Emu(int(200 * 12700)), Emu(int(36 * 12700)))
    cover.fill.solid()
    cover.fill.fore_color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    cover.line.fill.background()
    cover.shadow.inherit = False
    lb = s.shapes.add_textbox(Emu(int(428.5 * 12700)), Emu(int((209.3 - 1.8) * 12700)), Emu(int(190 * 12700)), Emu(int(18 * 12700)))
    lt = lb.text_frame
    lt.word_wrap = True
    lt.margin_left = lt.margin_right = lt.margin_top = lt.margin_bottom = 0
    lr = lt.paragraphs[0].add_run()
    lr.text = "Stand submission to organiser"
    lr.font.size = Pt(11)
    lr.font.name = TITLE_FONT
    lr.font.bold = True          # "bold" is how apply_house_fonts knows a run is a heading: it becomes Europa Regular, matching the printed row headings
    lr.font.color.rgb = RGBColor(0x1B, 0x18, 0x4D)
    for key, top in DATE_ROWS:
        val = (d.get(key) or "").strip()
        if not val:
            continue
        tb = s.shapes.add_textbox(Emu(int(606 * 12700)), Emu(int((top - 1.8) * 12700)), Emu(int(200 * 12700)), Emu(int(18 * 12700)))
        tf = tb.text_frame
        tf.word_wrap = True
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
        from pptx.enum.text import PP_ALIGN
        tf.paragraphs[0].alignment = PP_ALIGN.RIGHT
        r = tf.paragraphs[0].add_run()
        r.text = val
        r.font.size = Pt(11)
        r.font.name = TITLE_FONT
        r.font.bold = True       # same weight as the row headings on the page picture (see above)
        r.font.color.rgb = RGBColor(0x1B, 0x18, 0x4D)


# ---------------------------------------------------------------- main
TITLE_FONT, BODY_FONT = "Europa-Regular", "Europa-Light"


def apply_house_fonts(prs):
    """Titles/headings (anything the template makes bold) -> Europa Regular, not bold.
    All other text -> Europa Light. Applies to the slides and to the layouts they inherit from."""
    A = NS["a"]
    parts = [s._element for s in prs.slides] + [l._element for l in prs.slide_layouts]
    after = [f"{{{A}}}{t}" for t in ("ea", "cs", "sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst")]
    for root in parts:
        for el in root.iter(f"{{{A}}}rPr", f"{{{A}}}defRPr", f"{{{A}}}endParaRPr"):
            heading = el.get("b") == "1"
            if heading:
                el.set("b", "0")
            lat = el.find(f"{{{A}}}latin")
            if lat is None and not heading:
                continue  # inherits; the layout it inherits from is handled
            if lat is None:
                lat = etree.Element(f"{{{A}}}latin")
                nxt = next((c for c in el if c.tag in after), None)
                if nxt is not None:
                    nxt.addprevious(lat)
                else:
                    el.append(lat)
            if heading:
                lat.set("typeface", TITLE_FONT)
            elif lat.get("typeface", "").startswith("Europa"):
                lat.set("typeface", BODY_FONT)
            for k in ("panose", "pitchFamily", "charset"):
                lat.attrib.pop(k, None)


def size_page_titles(prs, titles=("Design Vision & Inspiration", "Materials & Furnishings", "Booth Location"), pt=16):
    """These two page titles are 16 pt (the layouts they come from are changed, so every slide inherits it)."""
    A = NS["a"]
    for layout in prs.slide_layouts:
        for sh in layout.shapes:
            if sh.has_text_frame and sh.text_frame.text.strip() in titles:
                for el in sh._element.iter(f"{{{A}}}rPr", f"{{{A}}}defRPr", f"{{{A}}}endParaRPr"):
                    el.set("sz", str(pt * 100))


# ---------------------------------------------------------------- Terms pages built from text an admin can change (see terms.py)
T_SIZE, T_HEAD, T_PITCH = 9.5, 10.0, 12.0          # point sizes, and the exact line height
T_LEFT_X, T_LEFT_W, T_RIGHT_X, T_RIGHT_W, T_TOP = 28.2, 376.0, 431.0, 390.0, 56.0
T_GAP = {"p": 6.0, "sub": 11.5, "h": 24.0, "after_h": 16.0, "after_sub": 2.5, "table": 15.0, "after_table": 15.0}
NAVY = RGBColor(0x1B, 0x18, 0x4D)
_METRIC_FONT = None


def _text_width(text, size):
    """Roughly how wide a line of text is, in points (measured with a similar font when one is installed)."""
    global _METRIC_FONT
    if _METRIC_FONT is None:
        _METRIC_FONT = False
        for p in ("/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"):
            if os.path.exists(p):
                try:
                    from PIL import ImageFont
                    _METRIC_FONT = ImageFont.truetype(p, 100)
                    break
                except Exception:
                    pass
    if _METRIC_FONT:
        return _METRIC_FONT.getlength(text) / 100.0 * size * 1.06
    return len(text) * size * 0.5


def _lines(text, size, width):
    text = text.replace("^", "")
    n, cur = 1, ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if cur and _text_width(trial, size) > width:
            n += 1
            cur = word
        else:
            cur = trial
    return n


def _runs(par, text, font, size, rgb=None):
    """Add text to a paragraph; ^x^ becomes small raised letters."""
    parts = text.split("^")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = par.add_run()
        r.text = part
        r.font.name = font
        r.font.size = Pt(size)
        r.font.color.rgb = rgb or NAVY
        if i % 2 == 1:
            r.font._element.set("baseline", "30000")


def _gap(prev, kind):
    if prev is None:
        return 0.0
    if prev == "h":
        return T_GAP["after_h"]
    if prev == "sub":
        return T_GAP["after_sub"]
    if prev == "table":
        return T_GAP["after_table"]
    return T_GAP[kind]


def _table(slide, rows, x, y, w):
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    fr = [0.187, 0.391, 0.422] if ncols == 3 else [1.0 / ncols] * ncols
    widths = [w * f for f in fr]
    heights = [max(17.0, max(_lines(c, T_SIZE, widths[j] - 12) for j, c in enumerate(r)) * T_PITCH + 6) for r in rows]
    gf = slide.shapes.add_table(len(rows), ncols, Emu(int(x * 12700)), Emu(int(y * 12700)), Emu(int(w * 12700)), Emu(int(sum(heights) * 12700)))
    tbl = gf.table
    tblPr = tbl._tbl.tblPr
    for a in ("firstRow", "bandRow"):
        if a in tblPr.attrib:
            del tblPr.attrib[a]
    sid = tblPr.find(qn("a:tableStyleId"))
    if sid is None:
        sid = etree.SubElement(tblPr, qn("a:tableStyleId"))
    sid.text = "{2D5ABB26-0587-4C30-8999-92F81FD0307C}"   # "No style, no grid": the borders are drawn below
    for j, wd in enumerate(widths):
        tbl.columns[j].width = Emu(int(wd * 12700))
    for i, r in enumerate(rows):
        tbl.rows[i].height = Emu(int(heights[i] * 12700))
        for j, txt in enumerate(r):
            cell = tbl.cell(i, j)
            cell.margin_left = cell.margin_right = Pt(6)
            cell.margin_top = Pt(2)
            cell.margin_bottom = Pt(2)
            cell.fill.background()
            tf = cell.text_frame
            tf.word_wrap = True
            par = tf.paragraphs[0]
            par.line_spacing = Pt(T_PITCH)
            _runs(par, txt, TITLE_FONT if i == 0 else BODY_FONT, T_SIZE)
            tcPr = cell._tc.get_or_add_tcPr()
            for k, tag in enumerate(("a:lnL", "a:lnR", "a:lnT", "a:lnB")):
                ln = etree.Element(qn(tag), w="15875", cap="flat", cmpd="sng", algn="ctr")
                sf = etree.SubElement(ln, qn("a:solidFill"))
                etree.SubElement(sf, qn("a:srgbClr"), val="1B184D")
                etree.SubElement(ln, qn("a:prstDash"), val="solid")
                tcPr.insert(k, ln)
    return sum(heights)


def _flush(slide, run, x, y, w):
    """One text box for a run of headings and paragraphs. Returns how tall it is (estimated)."""
    if not run:
        return 0.0
    h_est, prev = 0.0, None
    for kind, text in run:
        size = T_HEAD if kind == "sub" else T_SIZE
        h_est += _gap(prev, kind) + _lines(text, size, w) * T_PITCH
        prev = kind
    box = slide.shapes.add_textbox(Emu(int(x * 12700)), Emu(int(y * 12700)), Emu(int(w * 12700)), Emu(int((h_est + 4) * 12700)))
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    prev = None
    for k, (kind, text) in enumerate(run):
        par = tf.paragraphs[0] if k == 0 else tf.add_paragraph()
        par.line_spacing = Pt(T_PITCH)
        par.space_before = Pt(0 if k == 0 else _gap(prev, kind))
        _runs(par, text, BODY_FONT if kind == "p" else TITLE_FONT, T_HEAD if kind == "sub" else T_SIZE)
        prev = kind
    return h_est


def _column(slide, text, x, w):
    import terms as terms_mod
    y, run, prev = T_TOP, [], None
    for kind, val in terms_mod.parse_column(text):
        if kind == "table":
            y += _flush(slide, run, x, y + _gap(None, "p"), w) if run else 0.0
            run = []
            y += T_GAP["table"] if prev else 0.0
            y += _table(slide, val, x + 2, y, w - 10) + 0.0
        else:
            if not run and prev == "table":
                y += T_GAP["after_table"]
            run.append((kind, val))
        prev = kind
    _flush(slide, run, x, y, w)


def page_terms(prs, layout_idx, left, right):
    s = new_slide(prs, layout_idx)
    strip_empty_placeholders(s)
    _column(s, left or "", T_LEFT_X, T_LEFT_W)
    _column(s, right or "", T_RIGHT_X, T_RIGHT_W)
    return s


def terms_pages(prs, pages):
    for i, pg in enumerate(pages):
        page_terms(prs, L_FULL, pg.get("left", ""), pg.get("right", ""))


def terms_deck(pages, out_path):
    """A little deck of just the terms pages (for the preview)."""
    prs = Presentation(TEMPLATE)
    terms_pages(prs, pages)
    lst = prs.slides._sldIdLst
    for sld in list(lst)[:N_TEMPLATE_SLIDES]:
        prs.part.drop_rel(sld.rId)
        lst.remove(sld)
    apply_house_fonts(prs)
    prs.save(out_path)


def generate(data, out_path):
    prs = Presentation(TEMPLATE)
    cur = data.get("currency", "£")

    page_cover(prs, data)
    if data.get("mood_board"):
        page_mood_board(prs, data["mood_board"], data.get("mood_view"))
    if data.get("materials"):
        page_materials(prs, data["materials"])

    totals = {}
    quoted = []  # (show name, venue, total) for the summary
    many = len(data["shows"]) > 1
    for i, show in enumerate(data["shows"]):
        if many:  # a section title page only helps when the proposal covers several shows
            page_divider(prs, show)
        if show.get("floorplan"):
            page_location(prs, show)
        rnotes = show.get("render_notes") or []
        for fi, f in enumerate(show.get("renders", [])):
            imgs = to_images(f)
            for img in imgs:
                page_full_image(prs, img, notes=rnotes[fi] if len(imgs) == 1 and fi < len(rnotes) else None)
        if show.get("quote"):  # this show's quotation sits after its renders
            t = page_quote(prs, show["quote"], cur)
            totals[f"show{i}"] = t
            quoted.append((show["name"], show.get("location", ""), t))
    if many and len(quoted) >= 2:  # a summary section closes the shows: its own title card, then one line per show and a total
        t = new_slide(prs, L_DIVIDER)
        ph(t, 10).text_frame.text = "Summary"
        ph(t, 11).text_frame.text = f"{len(quoted)} shows"
        strip_empty_placeholders(t)
        totals["grand"] = page_summary(prs, [(f"{n}" + (f", {v}" if v else ""), a) for n, v, a in quoted], cur)

    if data.get("include_critical_dates"):
        page_dates(prs, data.get("critical_dates") or {})
    if data.get("include_payment_terms", True):
        if data.get("terms"):   # the wording an admin keeps up to date
            terms_pages(prs, data["terms"])
        else:
            fixed_page(prs, "page_payment_terms-1.png", "Payment Terms (1)")
            fixed_page(prs, "page_payment_terms-2.png", "Payment Terms (2)")
    names = data.get("about_names", [])
    for k, p in enumerate(data.get("about_files", [])):  # "About the company" pages, in the order chosen, after the terms
        if os.path.exists(p):
            page_full_image(prs, p, "Company: " + (names[k] if k < len(names) else "page"))
    fixed_page(prs, "page_end_card-1.png", "Closing page")

    # drop the 14 original template slides
    lst = prs.slides._sldIdLst
    for sld in list(lst)[:N_TEMPLATE_SLIDES]:
        prs.part.drop_rel(sld.rId)
        lst.remove(sld)
    apply_house_fonts(prs)
    size_page_titles(prs)
    prs.save(out_path)
    return totals


if __name__ == "__main__":
    src, dst = sys.argv[1], sys.argv[2]
    base = os.path.dirname(os.path.abspath(src))
    data = json.load(open(src))

    def resolve(o):
        if isinstance(o, dict):
            return {k: resolve(v) for k, v in o.items()}
        if isinstance(o, list):
            return [resolve(v) for v in o]
        if isinstance(o, str) and o.lower().endswith((".png", ".jpg", ".jpeg", ".pdf")):
            return os.path.join(base, o)
        return o

    print(generate(resolve(data), dst))
