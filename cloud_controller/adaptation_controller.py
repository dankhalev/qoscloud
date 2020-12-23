"""
The module contains Adaptation Controller class that runs all the steps of the adaptation (MAPE-K) loop sequentially,
in a single process.
"""
from multiprocessing.pool import ThreadPool
from time import perf_counter
from typing import Iterable, Type

import logging

import threading
from kubernetes import config

from cloud_controller import PRODUCTION_KUBECONFIG, THREAD_COUNT, PRODUCTION_MONGOS_SERVER_IP, DEFAULT_PREDICTOR_CONFIG, \
    PARALLEL_EXECUTION
from cloud_controller.analysis.analyzer import Analyzer
from cloud_controller.analysis.csp_solver.solver import CSPSolver
from cloud_controller.analysis.predictor import Predictor, StraightforwardPredictorModel
from cloud_controller.analysis.predictor_interface.predictor_service import StatisticalPredictor
from cloud_controller.cleanup import ClusterCleaner
from cloud_controller.execution.executor import Executor
from cloud_controller.ivis.ivis_interface import IvisInterface
from cloud_controller.ivis.ivis_mock import IVIS_INTERFACE_HOST, IVIS_INTERFACE_PORT
from cloud_controller.knowledge.knowledge import Knowledge
from cloud_controller.ivis.ivis_pb2_grpc import add_IvisInterfaceServicer_to_server
from cloud_controller.monitoring.monitor import Monitor, TopLevelMonitor, ApplicationMonitor, KubernetesMonitor, \
    UEMonitor, ClientMonitor
from cloud_controller.planning.execution_planner import ExecutionPlanner
from cloud_controller.middleware.helpers import setup_logging, start_grpc_server
from cloud_controller.knowledge import knowledge_pb2 as protocols


def log_phase_duration():
    """
    Helper function that logs the duration of a phase (basically, the time passed since the previos call)
    """
    logging.info("Phase duration: %f" % (perf_counter() - log_phase_duration.last_measured_time))
    log_phase_duration.last_measured_time = perf_counter()


log_phase_duration.last_measured_time = perf_counter()


class AdaptationController:
    """
    Runs all four phases of the adaptation loop sequentially. Uses an instance of a specialized class responsible for
    each phase (Monitor, Analyzer, ExecutionPlanner, and Executor respectively). As this is the central class for the
    Avocado framework, it also serves as a main target for customization. This class can be customized with the
    ExtensionManager
    """

    def __init__(self,
                 kubeconfig_file: str = PRODUCTION_KUBECONFIG,
                 knowledge: Knowledge = Knowledge(),
                 monitor=None,
                 analyzer=None,
                 planner=None,
                 executor=None,
                 solver_class: Type = CSPSolver,
                 predictor: Predictor = None,
                 mongos_ip: str = PRODUCTION_MONGOS_SERVER_IP,
                 thread_count: int = THREAD_COUNT
                 ):
        """
        :param kubeconfig_file: A path to the kubeconfig file to use.
        :param knowledge:       A reference to the Knowledge object to use.
        :param monitor:         Monitor to use. If None, will create default Monitor.
        :param analyzer:        Analyzer to use. If None, will create default Analyzer.
        :param planner:         Planner to use. If None, will create default ExecutionPlanner.
        :param executor:        Executor to use. If None, will create default Executor.
        :param solver_class:    A class of the solver to use.
        :param predictor:       Predictor to use.
        :param mongos_ip:       IP of a Mongos instance for executing any commands on MongoDB.
        :param thread_count:    Number of threads to use for Executor ThreadPool.
        """
        assert isinstance(knowledge, Knowledge)
        config.load_kube_config(config_file=kubeconfig_file)
        self.mongos_ip = mongos_ip
        self.pool = ThreadPool(processes=thread_count)
        self.knowledge = knowledge
        if predictor is None:
            if self.knowledge.client_support:
                predictor = StatisticalPredictor(self.knowledge)
            else:
                predictor = StraightforwardPredictorModel(DEFAULT_PREDICTOR_CONFIG)
        self.monitor = monitor
        self.analyzer = analyzer
        self.planner = planner
        self.executor = executor
        if monitor is None:
            self.monitor = TopLevelMonitor(self.knowledge)
            self.monitor.add_monitor(ApplicationMonitor())
            self.monitor.add_monitor(ClientMonitor())
            self.monitor.add_monitor(UEMonitor())
            self.monitor.add_monitor(KubernetesMonitor())
        if self.analyzer is None:
            self.analyzer = Analyzer(self.knowledge, solver_class, predictor, self.pool)
        if self.planner is None:
            self.planner = ExecutionPlanner(self.knowledge)
        if self.executor is None:
            self.executor = Executor(self.knowledge, self.pool, self.mongos_ip)
        self.desired_state = None
        self.execution_plans: Iterable[protocols.ExecutionPlan] = []

    def clean_cluster(self) -> None:
        """
        Deletes all the namespaces and the database records that were left after the previous run of Avocado.
        """
        ClusterCleaner(self.mongos_ip).cleanup()

    def monitoring(self):
        """
        Collects all the data from monitor (for the description of all the data being collected refer to the Monitor
        documentation).
        """
        logging.info("--------------- MONITORING PHASE ---------------")
        self.monitor.monitor()

    def analysis(self):
        """
        Gets the desired state from the Analyzer.
        """
        logging.info("--------------- ANALYSIS PHASE   ---------------")
        self.desired_state = self.analyzer.find_new_assignment()

    def planning(self):
        """
        Receives execution plans from Planner based on the desired state
        """
        logging.info("--------------- PLANNING PHASE   ---------------")
        self.execution_plans = self.planner.plan_changes(self.desired_state)

    def execution(self) -> int:
        """
        Executes the plans produced by Planner.
        :return: number of executed plans
        """
        logging.info("--------------- EXECUTION PHASE  ---------------")
        if PARALLEL_EXECUTION:
            plan_count = self.executor.execute_plans_in_parallel(self.execution_plans)
        else:
            plan_count = 0
            for execution_plan in self.execution_plans:
                self.executor.execute_plan(execution_plan)
                plan_count += 1
        logging.info(f"Executed {plan_count} plans")
        return plan_count

    def run_one_cycle(self) -> None:
        """
        Runs all four phases of the adaptation loop.
        """
        for phase in self.monitoring, self.analysis, self.planning, self.execution:
            phase()
            log_phase_duration()

    def deploy(self):
        """
        Runs adaptation loop until the actual state fully matches desired state.
        """
        plan_count = 1
        while plan_count != 0:
            self.monitoring()
            self.analysis()
            self.planning()
            plan_count = self.execution()

    def run(self) -> None:
        """
        Runs the adaption loop until interruption. This method blocks indefinitely.
        """
        logging.info("MAPE-K loop started.")
        try:
            while True:
                self.run_one_cycle()
        except KeyboardInterrupt:
            logging.info('^C received, ending')


if __name__ == "__main__":
    # TONOWDO: move ivis interface start elsewhere
    setup_logging()
    adaptation_ctl = AdaptationController()
    adaptation_ctl.clean_cluster()
    ivis_interface = IvisInterface(adaptation_ctl.knowledge)
    ivis_interface_thread = threading.Thread(
        target=start_grpc_server,
        args=(ivis_interface, add_IvisInterfaceServicer_to_server, IVIS_INTERFACE_HOST, IVIS_INTERFACE_PORT, 10, True))
    ivis_interface_thread.start()
    adaptation_ctl.run()
