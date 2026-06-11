import numpy as np
from ortools.graph.python import min_cost_flow


class MinCostFlow:
    def __init__(self, n):
        self.n = n
        self.total_cost = 0
        self.__solver = min_cost_flow.SimpleMinCostFlow()

    def add_edges(self, tails, heads, capacities, weights):
        """Add many arcs in one call; returns their arc indices (np.ndarray)."""
        return self.__solver.add_arcs_with_capacity_and_unit_cost(
            np.asarray(tails, dtype=np.int64),
            np.asarray(heads, dtype=np.int64),
            np.asarray(capacities, dtype=np.int64),
            np.asarray(weights, dtype=np.int64),
        )

    def set_supplies(self, nodes, supplies):
        self.__solver.set_nodes_supplies(
            np.asarray(nodes, dtype=np.int64),
            np.asarray(supplies, dtype=np.int64),
        )

    def solve(self):
        if self.__solver.solve() == self.__solver.OPTIMAL:
            self.total_cost = self.__solver.optimal_cost()
            return True
        return False

    def flows(self, arcs):
        """Flow values for the given arc indices, as an np.ndarray."""
        return np.asarray(self.__solver.flows(arcs))
