"""Graphiques PNG en Python pur (aucune dépendance), pour les envoyer sur Telegram.

Rendu en double résolution puis réduit de moitié : les courbes sont lissées.
"""
import struct
import zlib

# Police bitmap 5×7 : chaque caractère = 7 lignes de 5 bits
_FONT = {
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "€": "00111 01000 11110 01000 11110 01000 00111", ".": "00000 00000 00000 00000 00000 01100 01100",
    ",": "00000 00000 00000 00000 01100 00100 01000", "/": "00001 00010 00010 00100 01000 01000 10000",
    "-": "00000 00000 00000 11111 00000 00000 00000", "+": "00000 00100 00100 11111 00100 00100 00000",
    "%": "11001 11010 00010 00100 01000 01011 10011", ":": "00000 01100 01100 00000 01100 01100 00000",
    " ": "00000 00000 00000 00000 00000 00000 00000", "(": "00010 00100 01000 01000 01000 00100 00010",
    ")": "01000 00100 00010 00010 00010 00100 01000",
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 11001 10101 10011 10001 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
}
_ACCENTS = {"É": "E", "È": "E", "Ê": "E", "À": "A", "Â": "A", "Ô": "O", "Û": "U", "Ù": "U", "Î": "I", "Ç": "C"}

COLORS = {"bg": (255, 255, 255), "grid": (230, 232, 236), "text": (71, 84, 103), "muted": (122, 131, 148),
          "accent": (59, 91, 219), "series2": (152, 162, 179), "orange": (232, 89, 12)}


def _hex(c):
    return COLORS.get(c, c) if isinstance(c, str) else c


class Canvas:
    def __init__(self, w, h, bg="bg"):
        self.w, self.h = w, h
        self.px = bytearray(bytes(_hex(bg)) * (w * h))

    def set(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0, y0, x1, y1, c):
        c = bytes(_hex(c))
        x0, x1 = max(0, int(x0)), min(self.w, int(x1))
        for y in range(max(0, int(y0)), min(self.h, int(y1))):
            i = (y * self.w + x0) * 3
            self.px[i:i + (x1 - x0) * 3] = c * (x1 - x0)

    def dot(self, x, y, r, c):
        c = _hex(c)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= r * r:
                    self.set(x + dx, y + dy, c)

    def line(self, x0, y0, x1, y1, c, width=2, dash=0):
        c = _hex(c)
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err, n, r = dx + dy, 0, max(1, width // 2)
        while True:
            if not dash or (n // dash) % 2 == 0:
                self.dot(x0, y0, r, c)
            n += 1
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def text(self, x, y, s, c="text", scale=2, anchor="left"):
        s = "".join(_ACCENTS.get(ch, ch) for ch in s.upper())
        w = len(s) * 6 * scale
        if anchor == "right":
            x -= w
        elif anchor == "center":
            x -= w // 2
        c = _hex(c)
        for k, ch in enumerate(s):
            g = _FONT.get(ch, _FONT[" "]).split()
            for row, bits in enumerate(g):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(x + (k * 6 + col) * scale, y + row * scale,
                                  x + (k * 6 + col + 1) * scale, y + (row + 1) * scale, c)

    def downsample(self):
        """Réduit de moitié en moyennant les pixels (lissage)."""
        w2, h2 = self.w // 2, self.h // 2
        out = Canvas(w2, h2)
        src, dst, W = self.px, out.px, self.w
        for y in range(h2):
            r0 = (2 * y) * W * 3
            r1 = (2 * y + 1) * W * 3
            o = y * w2 * 3
            for x in range(w2):
                a = r0 + 6 * x
                b = r1 + 6 * x
                dst[o] = (src[a] + src[a + 3] + src[b] + src[b + 3]) >> 2
                dst[o + 1] = (src[a + 1] + src[a + 4] + src[b + 1] + src[b + 4]) >> 2
                dst[o + 2] = (src[a + 2] + src[a + 5] + src[b + 2] + src[b + 5]) >> 2
                o += 3
        return out

    def png(self):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(t, data):
            return struct.pack(">I", len(data)) + t + data + struct.pack(">I", zlib.crc32(t + data) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _money(v, whole=True):
    s = ("%.0f" % v) if whole else ("%.2f" % v).replace(".", ",")
    return s + "€"


def line_chart(series, title="", width=800, height=420, x_labels=None):
    """series : liste de dicts {label, color, points: [(x, y)], dash: bool}. Retourne des octets PNG."""
    S = 2
    W, H = width * S, height * S
    c = Canvas(W, H)
    L, R, T, B = 28 * S, 112 * S, (84 if title else 50) * S, 40 * S
    pts = [p for s in series for p in s["points"]]
    if len(pts) < 2:
        c.text(W // 2, H // 2, "PAS ENCORE DE DONNEES", "muted", scale=3 * S, anchor="center")
        return c.downsample().png()
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    pad = (y1 - y0) * 0.1 or abs(y1) * 0.1 or 1
    y0, y1 = (max(0.0, y0 - pad) if y0 >= 0 else y0 - pad), y1 + pad
    X = lambda x: L + (x - x0) / ((x1 - x0) or 1) * (W - L - R)
    Y = lambda y: T + (1 - (y - y0) / ((y1 - y0) or 1)) * (H - T - B)
    if title:
        c.text(L, 14 * S, title, "text", scale=3 * S)
    for f in (0, 0.5, 1):
        v = y0 + (y1 - y0) * f
        yy = int(Y(v))
        c.rect(L, yy, W - R, yy + S, "grid")
        c.text(W - R + 10 * S, yy - 7 * S, _money(v, (y1 - y0) >= 20), "muted", scale=2 * S)
    if x_labels:
        c.text(L, H - B + 14 * S, x_labels[0], "muted", scale=2 * S)
        c.text(W - R, H - B + 14 * S, x_labels[1], "muted", scale=2 * S, anchor="right")
    for s in series:
        p = s["points"]
        for a, b in zip(p, p[1:]):
            c.line(X(a[0]), Y(a[1]), X(b[0]), Y(b[1]), s["color"], width=(3 if s.get("dash") else 5) * S // 2,
                   dash=(7 * S if s.get("dash") else 0))
        if p:  # étiquette directe en bout de courbe
            c.dot(int(X(p[-1][0])), int(Y(p[-1][1])), 5 * S // 2 + 2, s["color"])
    # légende sous le titre, à gauche
    lx, yy = L, (48 if title else 14) * S
    for s in series:
        if s.get("dash"):
            c.line(lx, yy + 7 * S, lx + 24 * S, yy + 7 * S, s["color"], width=3 * S // 2, dash=6 * S)
        else:
            c.rect(lx, yy + 5 * S, lx + 24 * S, yy + 9 * S, s["color"])
        c.text(lx + 32 * S, yy, s["label"], "text", scale=2 * S)
        lx += 32 * S + len(s["label"]) * 12 * S + 30 * S
    return c.downsample().png()
