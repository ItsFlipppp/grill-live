"""Tiny QR code generator (byte mode, error correction M, versions 1-6, up to 106 bytes).

Enough for a phone link; follows the structure of Project Nayuki's QR Code generator.
"""

# version: (total codewords, EC codewords per block, number of blocks) at level M
_VERSIONS = {1: (26, 10, 1), 2: (44, 16, 1), 3: (70, 26, 1), 4: (100, 18, 2), 5: (134, 24, 2), 6: (172, 16, 4)}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_MASKS = [
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data, divisor):
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _codewords(data, version):
    total, ec_len, n_blocks = _VERSIONS[version]
    capacity = total - ec_len * n_blocks
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in reversed(range(8))]
    for b in data:
        bits += [(b >> i) & 1 for i in reversed(range(8))]
    bits += [0] * min(4, capacity * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < capacity:
        words.append(pad)
        pad ^= 0xEC ^ 0x11

    short_len = total // n_blocks
    n_short = n_blocks - total % n_blocks
    divisor = _rs_divisor(ec_len)
    blocks, k = [], 0
    for i in range(n_blocks):
        dat = words[k:k + short_len - ec_len + (0 if i < n_short else 1)]
        k += len(dat)
        ecc = _rs_remainder(dat, divisor)
        if i < n_short:
            dat.append(0)
        blocks.append(dat + ecc)
    out = []
    for i in range(len(blocks[0])):
        for j, blk in enumerate(blocks):
            if i != short_len - ec_len or j >= n_short:
                out.append(blk[i])
    return out


class _Grid:
    def __init__(self, version):
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set_func(self, x, y, dark):
        self.dark[y][x] = dark
        self.func[y][x] = True


def _draw_function_patterns(g, version):
    n = g.size
    for i in range(n):
        g.set_func(6, i, i % 2 == 0)
        g.set_func(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                x, y = cx + dx, cy + dy
                if 0 <= x < n and 0 <= y < n:
                    g.set_func(x, y, max(abs(dx), abs(dy)) not in (2, 4))
    pos = _ALIGN[version]
    last = len(pos) - 1
    for i, ax in enumerate(pos):
        for j, ay in enumerate(pos):
            if (i, j) in ((0, 0), (0, last), (last, 0)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    g.set_func(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    _draw_format(g, 0)


def _draw_format(g, mask):
    data = (0 << 3) | mask  # level M has format bits 00
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | rem) ^ 0x5412
    bit = lambda i: (bits >> i) & 1 == 1
    n = g.size
    for i in range(6):
        g.set_func(8, i, bit(i))
    g.set_func(8, 7, bit(6))
    g.set_func(8, 8, bit(7))
    g.set_func(7, 8, bit(8))
    for i in range(9, 15):
        g.set_func(14 - i, 8, bit(i))
    for i in range(8):
        g.set_func(n - 1 - i, 8, bit(i))
    for i in range(8, 15):
        g.set_func(8, n - 15 + i, bit(i))
    g.set_func(8, n - 8, True)


def _place_data(g, words):
    n, i = g.size, 0
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                upward = ((right + 1) & 2) == 0
                y = n - 1 - vert if upward else vert
                if not g.func[y][x] and i < len(words) * 8:
                    g.dark[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                    i += 1
        right -= 2


def _apply_mask(g, mask):
    f = _MASKS[mask]
    for y in range(g.size):
        for x in range(g.size):
            if not g.func[y][x] and f(x, y):
                g.dark[y][x] = not g.dark[y][x]


def _penalty(g):
    n, score = g.size, 0
    for lines in (g.dark, list(zip(*g.dark))):
        for line in lines:
            run = 1
            for a, b in zip(line, line[1:]):
                if a == b:
                    run += 1
                else:
                    score += run - 2 if run >= 5 else 0
                    run = 1
            score += run - 2 if run >= 5 else 0
    for y in range(n - 1):
        for x in range(n - 1):
            c = g.dark[y][x]
            if c == g.dark[y][x + 1] == g.dark[y + 1][x] == g.dark[y + 1][x + 1]:
                score += 3
    dark = sum(map(sum, g.dark))
    total = n * n
    score += ((abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10
    return score


def make(text):
    """Return the QR code for text as a list of rows of booleans (True = dark), no quiet zone."""
    data = text.encode("utf-8")
    version = next((v for v, (t, e, b) in _VERSIONS.items() if 12 + 8 * len(data) <= (t - e * b) * 8), None)
    if version is None:
        raise ValueError("text too long for this QR generator")
    words = _codewords(data, version)
    best = None
    for mask in range(8):
        g = _Grid(version)
        _draw_function_patterns(g, version)
        _place_data(g, words)
        _apply_mask(g, mask)
        _draw_format(g, mask)
        score = _penalty(g)
        if best is None or score < best[0]:
            best = (score, g)
    return best[1].dark


def to_svg(text, border=4, dark="#141110", light="#ffffff"):
    rows = make(text)
    n = len(rows) + border * 2
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(rows) for x, d in enumerate(row) if d)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="{light}"/><path d="{path}" fill="{dark}"/></svg>')


def to_terminal(text, border=2):
    """Two QR rows per text line using half blocks, with explicit ANSI colours so it scans on dark or light terminals."""
    rows = make(text)
    n = len(rows)
    grid = [[False] * (n + border * 2) for _ in range(border)]
    grid += [[False] * border + row + [False] * border for row in rows]
    grid += [[False] * (n + border * 2) for _ in range(border + (len(grid) + border) % 2)]
    out = []
    for y in range(0, len(grid), 2):
        line = ""
        for top, bottom in zip(grid[y], grid[y + 1]):
            line += f"\033[{30 if top else 97};{40 if bottom else 107}m▀"
        out.append(line + "\033[0m")
    return "\n".join(out)
