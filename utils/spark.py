blocks = ["▁","▂","▃","▄","▅","▆","▇","█"]

def to_sparkline(series):
    try:
        data = [float(x) for x in series]
        if not data:
            return ""
        mn, mx = min(data), max(data)
        rng = mx - mn
        if rng == 0:
            return blocks[0] * len(data)
        out = []
        for v in data:
            idx = int((v - mn) / rng * (len(blocks) - 1))
            out.append(blocks[idx])
        return "".join(out)
    except Exception:
        return ""


