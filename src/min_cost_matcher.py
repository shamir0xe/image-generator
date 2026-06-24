import logging

import numpy as np
from scipy.spatial import cKDTree

from src.utils.min_cost_flow import MinCostFlow

logger = logging.getLogger(__name__)


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """Convert an (..., 3) array of 0-255 sRGB to CIELAB (D65).

    Euclidean distance in Lab approximates perceived color difference ( delta E),
    which matches colors far better than raw RGB distance.
    """
    arr = np.asarray(rgb, dtype=np.float64) / 255.0
    # sRGB -> linear RGB
    lin = np.where(arr > 0.04045, ((arr + 0.055) / 1.055) ** 2.4, arr / 12.92)
    # linear RGB -> XYZ (sRGB/D65 matrix)
    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ]
    )
    xyz = lin @ m.T
    # normalize by the D65 reference white
    xyz /= np.array([0.95047, 1.0, 1.08883])
    eps, kappa = 0.008856, 903.3
    f = np.where(xyz > eps, np.cbrt(xyz), (kappa * xyz + 16.0) / 116.0)
    return np.stack(
        [
            116.0 * f[..., 1] - 16.0,
            500.0 * (f[..., 0] - f[..., 1]),
            200.0 * (f[..., 1] - f[..., 2]),
        ],
        axis=-1,
    )


class MinCostMatcher:
    def __init__(
        self,
        image_rgbs,
        frame_rgbs,
        knn_ratio: float = 0.1,
        metric: str = "lab-norm",
    ):
        # Flatten the 2D grid of cell colors into (n, 3), row-major (y * w + x)
        # to match how construct_box indexes the result.
        cells = []
        for row in image_rgbs:
            cells.extend(row)
        self.cells = np.asarray(cells, dtype=np.float64)
        self.frames = np.asarray(frame_rgbs, dtype=np.float64)

        self.n = len(self.cells)
        self.m = len(self.frames)

        # Choose the feature space the matcher measures distances in:
        #   rgb      -- raw mean RGB (legacy; not perceptual).
        #   lab      -- CIELAB, so distance ~ perceived color difference.
        #   lab-norm -- lab, then align the target cells' per-channel mean and
        #               std to the frame pool's (Reinhard transfer). This is the
        #               fix for targets whose exposure/contrast doesn't sit on
        #               the frame palette: mean aligns exposure, std aligns
        #               contrast, so the limited frame range is used fully.
        cell_feat, frame_feat = self._features(metric)
        logger.info(f"match metric: {metric}")

        # Instead of connecting every cell to every frame (n * m edges), connect
        # each cell only to its k nearest frames in feature space. A KD-tree
        # makes this exact and fast for 3D color points.
        k = max(1, min(self.m, round(self.m * knn_ratio)))
        self.k = k
        logger.info(
            f"kNN candidate edges: k={k} ({knn_ratio:.1%} of {self.m} frames), "
            f"cells={self.n}"
        )

        tree = cKDTree(frame_feat)
        dist, idx = tree.query(cell_feat, k=k)
        # query() returns 1-D arrays when k == 1; normalize to (n, k).
        self.cand_idx = idx.reshape(self.n, k)
        self.cand_dist = np.rint(dist.reshape(self.n, k)).astype(np.int64)

        # Endpoints of the cell -> frame candidate arcs are constant across the
        # capacity search, so precompute them once.
        self._cand_tails = np.repeat(np.arange(self.n), k)
        self._cand_heads = (self.n + self.cand_idx).reshape(-1)
        self._cand_weights = self.cand_dist.reshape(-1)

    def _features(self, metric: str):
        """Return (cell_features, frame_features) for the chosen metric."""
        if metric == "rgb":
            return self.cells, self.frames

        cell_lab = _srgb_to_lab(self.cells)
        frame_lab = _srgb_to_lab(self.frames)

        if metric == "lab-norm":
            # Reinhard transfer: shift+scale the cells so their per-channel mean
            # and spread match the frame pool's, in Lab.
            mc, sc = cell_lab.mean(axis=0), cell_lab.std(axis=0)
            mf, sf = frame_lab.mean(axis=0), frame_lab.std(axis=0)
            sc = np.where(sc < 1e-6, 1.0, sc)  # avoid divide-by-zero on flat targets
            cell_lab = (cell_lab - mc) / sc * sf + mf
        elif metric != "lab":
            raise ValueError(f"unknown match metric '{metric}' (rgb|lab|lab-norm)")

        return cell_lab, frame_lab

    def best_match(self):
        # Factor for the cost in order to determine the best matching
        factor = 1.1
        s, e = 0, 55
        best_cost = 1e20
        while e - s > 1:
            m = (e + s) >> 1
            logger.info(f"trying {m}...")
            try:
                _, cost = self.solve(m)
                logger.info(f"current cost: {cost}")
                if best_cost > cost:
                    best_cost = cost
                if cost > factor * best_cost:
                    # The cost is too much
                    raise Exception
                e = m
            except Exception:
                s = m
        logger.info(f"best capacity: {e}")
        return self.solve(e)[0]

    def solve(self, max_same_picture: int):
        assert max_same_picture > 0
        n, m, k = self.n, self.m, self.k
        flow = MinCostFlow(n + m + 2)
        source, sink = n + m, n + m + 1

        # Arcs: source -> cell (cap 1), cell -> candidate frame (cap 1, cost =
        # color distance), frame -> sink (cap max_same_picture). All added in a
        # single vectorized call.
        tails = np.concatenate(
            [np.full(n, source), self._cand_tails, n + np.arange(m)]
        )
        heads = np.concatenate(
            [np.arange(n), self._cand_heads, np.full(m, sink)]
        )
        caps = np.concatenate(
            [
                np.ones(n, np.int64),
                np.ones(n * k, np.int64),
                np.full(m, max_same_picture, np.int64),
            ]
        )
        weights = np.concatenate(
            [np.zeros(n, np.int64), self._cand_weights, np.zeros(m, np.int64)]
        )

        arcs = flow.add_edges(tails, heads, caps, weights)
        flow.set_supplies([source, sink], [n, -n])
        if not flow.solve():
            raise Exception("Invalid matching")

        # The candidate arcs are the middle block; for each cell exactly one of
        # its k arcs carries a unit of flow -> that's the chosen frame.
        cand_arcs = arcs[n : n + n * k]
        flows = flow.flows(cand_arcs).reshape(n, k)
        chosen = flows.argmax(axis=1)
        order = self.cand_idx[np.arange(n), chosen]
        return order.tolist(), flow.total_cost
