"""Train the boards' learned model from labelled seconds (Activity page,
"Teach the boards"): each row is one board's 8 features (mlfeat.FEATURES)
with what you said was happening (empty, still, moving).

Boards differ in their quiet level, so the first two features (the shape's
change) are shifted per board onto a common quiet level first: the same shift
each board makes live (csi_core.h ml_quiet_t), so the weights fit every board.

Three outputs (empty / still / moving) when there are enough of each, else
two (moving or not). Softmax / logistic regression, small L2: a model the
size ESPectre's runs, with weights sent to the boards over the air (MLW).
Its score is measured honestly: whole recordings held out, never single
seconds of a recording that also trained it.
"""
import json
import math

import numpy as np

from mlfeat import FEATURES

LABELS = ('empty', 'still', 'moving')
MIN_ROWS = 20                      # per label, before it counts
F = len(FEATURES)


def read_rows(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(r, dict) and r.get('label') in LABELS and isinstance(r.get('f'), list)
                        and len(r['f']) == F and all(isinstance(x, (int, float)) and math.isfinite(x) for x in r['f'])):
                    rows.append(r)
    except OSError:
        pass
    return rows


def counts(rows):
    out = {}
    for r in rows:
        b = out.setdefault(r['board'], {k: 0 for k in LABELS})
        b[r['label']] += 1
    return out


def _quiet(X):
    """A board's quiet level of f0, f1: their 20th percentile (as the board does)."""
    return np.percentile(X[:, :2], 20, axis=0)


def _fit(X, y, k, l2=1e-2, it=2500, lr=0.5):
    W, b = np.zeros((X.shape[1], k)), np.zeros(k)
    Y = np.eye(k)[y]
    for _ in range(it):
        z = X @ W + b
        z -= z.max(1, keepdims=True)
        P = np.exp(z)
        P /= P.sum(1, keepdims=True)
        G = (P - Y) / len(y)
        W -= lr * (X.T @ G + l2 * W)
        b -= lr * G.sum(0)
    return W, b


def train(rows):
    """dict(params, nh, no, labels, score, n) or dict(error=...)."""
    if not rows:
        return dict(error='nothing recorded yet')
    n = {k: sum(r['label'] == k for r in rows) for k in LABELS}
    if all(n[k] >= MIN_ROWS for k in LABELS):
        labels = LABELS
    elif n['moving'] >= MIN_ROWS and n['empty'] + n['still'] >= MIN_ROWS:
        labels = ('not moving', 'moving')
    else:
        return dict(error=f"need at least {MIN_ROWS} s of 'moving' and {MIN_ROWS} s of 'empty' or 'still' "
                          f"(have: {n['empty']} empty, {n['still']} still, {n['moving']} moving)")
    X = np.array([r['f'] for r in rows], float)
    lab = [r['label'] for r in rows]
    y = np.array([labels.index(l) if l in labels else 0 for l in lab]) if len(labels) == 3 else \
        np.array([1 if l == 'moving' else 0 for l in lab])
    # each board's quiet onto a common one (the median of the boards')
    boards = sorted({r['board'] for r in rows})
    qs = {}
    for b in boards:
        m = np.array([r['board'] == b and r['label'] != 'moving' for r in rows])
        qs[b] = _quiet(X[m]) if m.sum() >= 5 else _quiet(X[np.array([r['board'] == b for r in rows])])
    ref = np.median(np.array(list(qs.values())), axis=0)
    Xs = X.copy()
    for i, r in enumerate(rows):
        Xs[i, :2] += ref - qs[r['board']]
    mu, sd = Xs.mean(0), Xs.std(0) + 1e-6
    Z = (Xs - mu) / sd
    k = len(labels) if len(labels) == 3 else 2
    # honest score: recordings (a board, a label, within a minute) held out in turn
    groups = np.array([f"{r['board']}|{r['label']}|{int(r['t'] // 60)}" for r in rows])
    ug = np.unique(groups)
    rng = np.random.default_rng(0)
    rng.shuffle(ug)
    pred = np.zeros(len(y), int)
    for fold in np.array_split(ug, min(5, len(ug))):
        te = np.isin(groups, fold)
        if te.all() or not te.any():
            continue
        W, b = _fit(Z[~te], y[~te], k)
        pred[te] = np.argmax(Z[te] @ W + b, 1)
    score = {labels[c]: round(float((pred[y == c] == c).mean()), 2) for c in range(k) if (y == c).any()}
    W, b = _fit(Z, y, k)
    if k == 2:                          # one output: moving's log-odds
        w1, b1 = W[:, 1] - W[:, 0], b[1] - b[0]
        params = list(mu) + list(sd) + list(ref) + list(w1) + [b1]
        nh, no = 0, 1
    else:
        params = list(mu) + list(sd) + list(ref) + list(W.ravel()) + list(b)
        nh, no = 0, 3
    return dict(params=[float(v) for v in params], nh=nh, no=no, labels=list(labels), score=score,
                n=len(rows), boards=boards)


def mlw_commands(model, per_line=8):
    """The MLW lines that load a model onto a board."""
    p = model['params']
    out = [f"MLW,begin,{model['nh']},{model['no']}"]
    for i in range(0, len(p), per_line):
        out.append(f"MLW,{i}," + ','.join(f'{v:.6g}' for v in p[i:i + per_line]))
    out.append(f"MLW,commit,{len(p)}")
    return out
