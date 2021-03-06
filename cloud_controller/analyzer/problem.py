import time
from typing import List, Optional

import logging
from ortools.constraint_solver.pywrapcp import Solver, SolutionCollector, IntVar, OptimizeVar

from cloud_controller import CSP_DEFAULT_TIME_LIMIT
from cloud_controller.analyzer.constraint import Constraint
from cloud_controller.analyzer.objective_function import ObjectiveFunction
from cloud_controller.analyzer.variables import Variables


class CSPProblem:
    """
    Builds the constraint satisfaction problem, searches for the best solution until the time runs out (defined by
    time_limit), and returns the best solution found.

    For the description of the constraint model of the problem, refer to the Constraint classes docs.

    For the description of the objective function that determines which solution is the best, refer to the
    ObjectiveFunction docs.
    """

    count = 0
    NO_LIMIT = -1  # Search for the first solution without any time limits.

    def __init__(self, variables: Variables, constraints: List["Constraint"], obj_function: "ObjectiveFunction"):
        start_ = time.perf_counter()
        CSPProblem.count += 1
        self._time_limit_ms: int = CSP_DEFAULT_TIME_LIMIT * 1000
        self._solver: Solver = Solver(f"Solver {CSPProblem.count}")
        self._variables: Variables = variables
        self._objective_function: Optional[ObjectiveFunction] = obj_function

        start = time.perf_counter()
        self._variables.clear()
        self._variables.add(self._solver)
        logging.debug(f"Variables adding time: {(time.perf_counter() - start):.15f}")

        start = time.perf_counter()
        # Add all the registered constraints. For each type of constraint see the docs to the corresponding class
        for constraint in constraints:
            constraint.add(self._solver, self._variables)
        logging.debug(f"Constraints adding time: {(time.perf_counter() - start):.15f}")

        start = time.perf_counter()
        self._objective_function.add(self._solver, self._variables)
        logging.debug(f"Objective adding time: {(time.perf_counter() - start):.15f}")

        self._collector = self._create_solution_collector()
        logging.info(f"Problem creation time: {(time.perf_counter() - start_):.15f}")

    def set_time_limit(self, tl: int):
        self._time_limit_ms = tl

    def get_solver(self):
        return self._solver

    def get_solution_collector(self):
        return self._collector

    def solve(self) -> bool:
        start = time.perf_counter()
        success: bool = self._solve(self._collector, self._objective_function.objective)
        logging.info(f"Solution time: {(time.perf_counter() - start):.15f}")

        if success:
            logging.info("Solution found")
        else:
            logging.info("Solution not found")
        # self._log_variables(collector)
        # self._log_solver_stats()
        return success

    def _create_solution_collector(self) -> SolutionCollector:
        collector: SolutionCollector = self._solver.BestValueSolutionCollector(False)
        collector.Add(self._all_vars())
        collector.AddObjective(self._objective_function.cost_var)
        return collector

    def _solve(self, collector: SolutionCollector, objective_function: OptimizeVar) -> bool:
        """
        If time limit is set, tries to find the best possible solution in the given time. If there is no limit on the
        search, searches until finds the first solution (it cannot be guaranteed that the solution will be found).
        :return: True if solution was found, false otherwise.
        """
        decision_builder = self._solver.Phase(self._all_vars(), self._solver.CHOOSE_FIRST_UNBOUND,
                                              self._solver.ASSIGN_MIN_VALUE)
        if self._time_limit_ms != CSPProblem.NO_LIMIT:
            tl = self._solver.TimeLimit(self._time_limit_ms)
            self._solver.Solve(decision_builder, [collector, objective_function, tl])
        else:
            self._solver.NewSearch(decision_builder, [collector, objective_function])
            self._solver.NextSolution()
        return collector.SolutionCount() > 0

    def _all_vars(self) -> List[IntVar]:
        return [var.int_var for var in self._variables.all_vars] + [self._objective_function.cost_var]


