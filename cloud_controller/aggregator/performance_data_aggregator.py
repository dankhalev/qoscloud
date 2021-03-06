from threading import RLock
from typing import Dict, List, Tuple, Iterator, Set

import logging

import cloud_controller.aggregator.predictor_pb2 as predictor_pb
from cloud_controller import DEFAULT_HARDWARE_ID, PREDICTOR_HOST, PREDICTOR_PORT, STATISTICAL_PREDICTION_ENABLED
from cloud_controller.aggregator.multipredictor import MultiPredictor
from cloud_controller.aggregator.predictor_pb2_grpc import PredictorServicer, \
    add_PredictorServicer_to_server
from cloud_controller.aggregator.scenario_generator import ScenarioGenerator
from cloud_controller.architecture_pb2 import ApplicationTimingRequirements
from cloud_controller.assessment.model import Scenario
from cloud_controller.aggregator.measurement_aggregator import MeasurementAggregator
from cloud_controller.knowledge.model import Application, TimeContract, ThroughputContract, Probe
from cloud_controller.middleware.helpers import start_grpc_server, setup_logging


class PerformanceDataAggregator(PredictorServicer):
    """
    The interface of performance data aggregator that provides access to the functionality
    of the scenario generator, measurement aggregator, and predictor modules.

    The interface is accessible through gRPC. For the parameters of the individual methods,
    see the predictor.proto file.
    """

    def __init__(self):
        self._single_process_predictor = MeasurementAggregator()
        self._predictor = MultiPredictor()
        self.applications: Dict[str, Application] = {}
        self._probes_by_component: Dict[str, Set[str]] = {}
        self._probes_by_id: Dict[str, Probe] = {}
        self._scenario_generator = ScenarioGenerator(self._predictor)
        self._lock = RLock()
        for hw_id, probe_id, bg_probe_ids, filename in self._single_process_predictor.load_existing_measurements():
            self._scenario_generator.load_datafile(hw_id, probe_id, bg_probe_ids, filename)

    def assignment_from_pb(self, assignment_pb: predictor_pb.Assignment) -> Tuple[str, Dict[str, int]]:
        assignment: Dict[str, int] = {}
        for component_pb in assignment_pb.components:
            assert component_pb.component_id in self._probes_by_component
            assignment[component_pb.component_id] = component_pb.count
        return assignment_pb.hw_id, assignment

    def _generate_combinations(self, assignment: Dict[str, int]) -> Iterator[List[str]]:
        def generate_probe_combinations(probes: List[str], size: int, combination: List[str]) -> List[str]:
            if len(combination) == size:
                yield combination
                return
            if len(probes) == 0:
                return
            for i in range(size + 1):
                if len(combination) + i <= size:
                    for probe_combination in generate_probe_combinations(probes[1:], size, combination + [probes[0]] * i):
                        yield probe_combination

        def generate_component_combinations(
                components: List[Tuple[str, int]],
                combination: List[str],
                main_component: str
        ) -> List[str]:
            if len(components) == 0:
                yield combination
                return
            component, count = components[0]
            if component == main_component:
                count = count - 1
            probes = list(self._probes_by_component[component])
            for probe_combination in generate_probe_combinations(probes, count, []):
                for full_combination in generate_component_combinations(components[1:], combination + probe_combination,
                                                                        main_component):
                    yield full_combination
        assignment_tuples = [(component, assignment[component]) for component in assignment]
        for component in assignment:
            for full_combination in generate_component_combinations(assignment_tuples, [], component):
                for probe_id in self._probes_by_component[component]:
                    yield [probe_id] + full_combination

    def Predict(self, request: predictor_pb.Assignment, context):
        """
        Returns a prediction of whether all QoS requirements of the specified instances are going to be
        satisfied if they will run together on a node of the specified HW configuration.
        """
        if len(request.components) == 1 and request.components[0].count == 1 and request.hw_id == DEFAULT_HARDWARE_ID:
            assert request.components[0].component_id in self._probes_by_component
            return predictor_pb.Prediction(result=True)
        hw_id, assignment = self.assignment_from_pb(request)
        for combination in self._generate_combinations(assignment=assignment):
            measurement = MeasurementAggregator.compose_measurement_name(hw_id, combination)
            measured = self._single_process_predictor.has_measurement(measurement)
            if not STATISTICAL_PREDICTION_ENABLED and not measured:
                self._scenario_generator.increase_count(hw_id, combination[0], len(combination))
                return predictor_pb.Prediction(result=False)
            probe = self._probes_by_id[combination[0]]
            for requirement in probe.requirements:
                prediction: bool = False
                if isinstance(requirement, TimeContract):
                    if measured:
                        prediction = self._single_process_predictor.predict_time(
                            probe_name=measurement,
                            time_limit=requirement.time,
                            percentile=requirement.percentile
                        )
                    else:
                        prediction = self._predictor.predict_time(
                            hw_id=hw_id,
                            combination=combination,
                            time_limit=requirement.time,
                            percentile=requirement.percentile
                        )
                elif isinstance(requirement, ThroughputContract):
                    if measured:
                        prediction = self._single_process_predictor.predict_throughput(
                            probe_name=measurement,
                            max_mean_time=requirement.mean_request_time
                        )
                    else:
                        prediction = self._predictor.predict_throughput(
                            hw_id=hw_id,
                            combination=combination,
                            max_value=requirement.mean_request_time
                        )
                if not prediction:
                    self._scenario_generator.increase_count(hw_id, combination[0], len(combination))
                    return predictor_pb.Prediction(result=False)
        return predictor_pb.Prediction(result=True)

    def RegisterApp(self, request, context):
        """
        Registers the specified application architecture with the performance data aggregator.
        """
        app = Application.init_from_pb(request)
        with self._lock:
            self.applications[app.name] = app
            for component in app.components.values():
                for probe in component.probes:
                    self._register_probe(probe)
                    self._scenario_generator.register_probe(probe)
                    if probe.signal_set != "":
                        if self._single_process_predictor.has_measurement(
                                MeasurementAggregator.compose_measurement_name(DEFAULT_HARDWARE_ID, [probe.alias])
                        ):
                            self._single_process_predictor.report_measurements(
                                probe.alias,
                                probe.signal_set,
                                probe.execution_time_signal,
                                probe.run_count_signal
                            )

        return predictor_pb.RegistrationAck()

    def UnregisterApp(self, request, context):
        """
        Unregisters the specified application from the performance data aggregator.
        """
        return predictor_pb.RegistrationAck()

    def RegisterHwConfig(self, request, context):
        """
        Registers the specified HW configuration with the performance data aggregator.
        """
        hw_id: str = request.name
        self._predictor.add_hw_id(hw_id)
        return predictor_pb.RegistrationAck()

    def FetchScenario(self, request, context):
        """
        Returns the next scenario generated by the scenario generator module.
        """
        with self._lock:
            scenario = self._scenario_generator.next_scenario()
            if scenario is None:
                return predictor_pb.Scenario()
            logging.info(f"Sending scenario description for scenario {scenario.id_}")
            return scenario.pb_representation()

    def ReportPercentiles(self, request, context):
        """
        Returns the response times of the specified probe at the specified percentiles and its
        mean response time.
        """
        response = ApplicationTimingRequirements()
        measurement_name = MeasurementAggregator.compose_measurement_name(DEFAULT_HARDWARE_ID, [request.name])
        if not self._single_process_predictor.has_measurement(measurement_name):
            response.mean = -1
            return response
        response.name = request.name
        for percentile in request.contracts:
            time = self._single_process_predictor.running_time_at_percentile(measurement_name, percentile.percentile)
            contract = response.contracts.add()
            contract.time = time
            contract.percentile = percentile.percentile
        response.mean = self._single_process_predictor.mean_running_time(measurement_name)
        return response

    def JudgeApp(self, request, context):
        """
        Decides whether the specified application can be accepted (i.e. whether its QoS requirements
        are realistic) based on the available measurement data.
        """
        app = Application.init_from_pb(request)
        # The application has to be already registered before, otherwise we cannot judge it
        if not app.name in self.applications:
            return predictor_pb.JudgeReply(result=predictor_pb.JudgeResult.Value("NEEDS_DATA"))
        with self._lock:
            # All isolation measurements need to be finished before we can judge the app.
            # if self._measuring_phases[app.name] == MeasuringPhase.ISOLATION:
            # Check every QoS requirement one-by-one:
            for component in app.components.values():
                for probe in component.probes:
                    measurement_name = \
                        MeasurementAggregator.compose_measurement_name(DEFAULT_HARDWARE_ID, [probe.alias])
                    if not self._single_process_predictor.has_measurement(measurement_name):
                        return predictor_pb.JudgeReply(result=predictor_pb.JudgeResult.Value("NEEDS_DATA"))
                    for requirement in probe.requirements:
                        prediction = False
                        if isinstance(requirement, TimeContract):
                            prediction = self._single_process_predictor.predict_time(
                                probe_name=measurement_name,
                                time_limit=requirement.time,
                                percentile=requirement.percentile
                            )
                        elif isinstance(requirement, ThroughputContract):
                            prediction = self._single_process_predictor.predict_throughput(
                                probe_name=measurement_name,
                                max_mean_time=requirement.mean_request_time
                            )
                        if not prediction:
                            return predictor_pb.JudgeReply(result=predictor_pb.JudgeResult.Value("REJECTED"))
            if not app.is_complete:
                return predictor_pb.JudgeReply(result=predictor_pb.JudgeResult.Value("MEASURED"))
            # Application is accepted now
            # However, some QoS requirements may have been added between app registration and app evaluation.
            # Thus, we re-register all the probes to include these requirements
            self.applications[app.name] = app
            for component in app.components.values():
                self._probes_by_component[probe.component.id] = set()
                for probe in component.probes:
                    assert probe.alias in self._probes_by_id
                    self._register_probe(probe)
                    self._probes_by_component[probe.component.id].add(probe.alias)
        return predictor_pb.JudgeReply(result=predictor_pb.JudgeResult.Value("ACCEPTED"))

    def OnScenarioDone(self, request, context):
        """
        Reports scenario completion and provides a path to the corresponding measurement data file.
        """
        scenario: Scenario = Scenario.init_from_pb(request, self.applications)
        app_name = scenario.controlled_probe.component.application.name
        logging.info(f"Received ScenarioDone notification for scenario {scenario.id_} of app {app_name}")
        with self._lock:
            # Remove scenario from the list of to-be-done scenarios
            self._scenario_generator.scenario_completed(scenario)
            self._single_process_predictor.process_measurement_file(
                MeasurementAggregator.compose_measurement_name_from_scenario(scenario),
                scenario.filename_data
            )
        return predictor_pb.CallbackAck()

    def _register_probe(self, probe: Probe) -> None:
        self._probes_by_id[probe.alias] = probe


if __name__ == "__main__":
    setup_logging()
    start_grpc_server(PerformanceDataAggregator(), add_PredictorServicer_to_server, PREDICTOR_HOST, PREDICTOR_PORT, block=True)