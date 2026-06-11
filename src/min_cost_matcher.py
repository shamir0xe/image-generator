import logging

import numpy as np
from scipy.spatial import cKDTree

from src.utils.min_cost_flow import MinCostFlow

logger = logging.getLogger(__name__)


class MinCostMatcher:
    def __init__(self, image_rgbs, frame_rgbs, knn_ratio: float = 0.1):
        # Flatten the 2D grid of cell colors into (n, 3), row-major (y * w + x)
        # to match how construct_box indexes the result.
        cells = []
        for row in image_rgbs:
            cells.extend(row)
        self.cells = np.asarray(cells, dtype=np.float64)
        self.frames = np.asarray(frame_rgbs, dtype=np.float64)

        self.n = len(self.cells)
        self.m = len(self.frames)

        # Instead of connecting every cell to every frame (n * m edges), connect
        # each cell only to its k nearest frames in RGB space. A KD-tree makes
        # this exact and fast for 3D color points.
        k = max(1, min(self.m, round(self.m * knn_ratio)))
        self.k = k
        logger.info(
            f"kNN candidate edges: k={k} ({knn_ratio:.1%} of {self.m} frames), "
            f"cells={self.n}"
        )

        tree = cKDTree(self.frames)
        dist, idx = tree.query(self.cells, k=k)
        # query() returns 1-D arrays when k == 1; normalize to (n, k).
        self.cand_idx = idx.reshape(self.n, k)
        self.cand_dist = np.rint(dist.reshape(self.n, k)).astype(np.int64)

        # Endpoints of the cell -> frame candidate arcs are constant across the
        # capacity search, so precompute them once.
        self._cand_tails = np.repeat(np.arange(self.n), k)
        self._cand_heads = (self.n + self.cand_idx).reshape(-1)
        self._cand_weights = self.cand_dist.reshape(-1)

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
