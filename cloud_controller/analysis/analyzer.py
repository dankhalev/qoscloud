"""
Contains Analyzer class responsible for the Analysis phase.
"""
import logging
from typing import Type, Optional

from multiprocessing.pool import ThreadPool, AsyncResult

from cloud_controller.analysis.predictor import Predictor
from cloud_controller.knowledge.knowledge import Knowledge
from cloud_controller.knowledge.model import CloudState, ManagedCompin


class Analyzer:
    """
    Determines the desired state of the cloud with the help of the solver and the predictor.
    """

    def __init__(self, knowledge: Knowledge, solver_class: Type, predictor: Predictor, pool: ThreadPool):
        """
        :param knowledge: Reference to the Knowledge
        :param solver_class: A class of the solver that has to be used. The solver is instantiated internally.
        :param predictor: Reference to the predictor that has to be supplied to the solver.
        :param pool: Thread pool for running long-term searches for the desired state.
        """
        self.knowledge: Knowledge = knowledge
        self.predictor: Predictor = predictor
        self._solver_class: Type = solver_class
        self._last_desired_state: CloudState = CloudState()
        self._pool: ThreadPool = pool
        self._longterm_result_future: Optional[AsyncResult] = None
        self._solver = None

    def set_solver_class(self, solver_class: Type) -> None:
        """
        :param solver_class: A class of the solver that has to be used for desired state search.
        """
        self._solver_class = solver_class

    def _mark_force_keep_compins(self, desired_state: CloudState) -> None:
        """
        Marks the dependencies of newly connected clients with force_keep, meaning that these dependencies should not be
        removed from the cloud until the client connects to them. This is important since we want the dependencies for
        newly connected clients to be instantiated as soon as possible, and thus cannot allow them to be deleted.
        """
        for compin in self.knowledge.list_new_clients():
            # Get the same compin from the desired state:
            compin = desired_state.get_compin(compin.component.application.name, compin.component.name, compin.id)
            if compin is not None:
                for dependency in compin.list_dependencies():
                    dependency.set_force_keep()

    def find_new_assignment(self) -> CloudState:
        """
        Gets current network distances, instantiates a solver, and runs the search for desired state. If the solver
        fails to find a desired state, quickly (default 5 seconds), returns the previous desired state, while starting
        an asynchronous long-term computation of the desired state. The result of that computation will be returned in
        one of the next calls to this method (when the computation is finished).
        :return: The new desired state of the cloud if found, last found desired state otherwise.
        """
        network_distances = self.knowledge.network_topology.get_network_distances(
            self.knowledge.actual_state.list_all_unmanaged_compins(),
            self.knowledge.nodes.values()
        )
        solver = self._solver_class(self.knowledge, network_distances, self.predictor)
        desired_state: CloudState = solver.find_assignment()

        if desired_state is None:
            # If solver could not find a result quickly, we can turn to long-term computation:
            logging.info("No desired state was found. Using long-term desired state computation.")
            if self._longterm_result_future is not None:
                # If a long-term computation of the desired state is already running, we can try to get its result:
                if self._longterm_result_future.ready():
                    logging.info("Using the result of a long-term desired state computation.")
                    desired_state = self._longterm_result_future.get()
                    self._longterm_result_future = None
            else:
                # Otherwise we start that long-term computation
                self._solver = self._solver_class(self.knowledge, network_distances, self.predictor)
                self._longterm_result_future = self._pool.apply_async(self._solver.find_assignment_longterm, ())

        if desired_state is None:
            # If the result is still None, we just return the last desired state (for now, until the new assignment is
            # found).
            logging.info("Using previous desired state.")
            desired_state = self._last_desired_state

        self._mark_force_keep_compins(desired_state)
        self._log_desired_state(desired_state)
        self._last_desired_state = desired_state
        return desired_state

    @staticmethod
    def _log_desired_state(desired_state: CloudState) -> None:
        """
        Prints out the node-compin assignment from the supplied CloudState, as well as the dependencies of each client.
        """
        logging.info(" --- Current assignment: --- ")
        for app in desired_state.list_applications():
            for component in desired_state.list_components(app):
                for instance in desired_state.list_instances(app, component):
                    compin = desired_state.get_compin(app, component, instance)
                    if isinstance(compin, ManagedCompin):
                        logging.info(f"DP: Component {app}:{component} has to be deployed on node {compin.node_name}")
                    else:
                        logging.info(f"Client {app}:{component}:{instance} has to be connected to components "
                                     f"{list(dependency.id for dependency in compin.list_dependencies())}")
        logging.info(" --------------------------- ")