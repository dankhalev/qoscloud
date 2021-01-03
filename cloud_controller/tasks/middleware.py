import logging

from cloud_controller import API_ENDPOINT_IP, API_ENDPOINT_PORT
from cloud_controller.knowledge.knowledge import Knowledge
from cloud_controller.knowledge.model import Component, CompinPhase, ManagedCompin
from cloud_controller.middleware.middleware_pb2 import InstanceConfig, ProbeType, DependencyAddress, MongoParameters
from cloud_controller.middleware.middleware_pb2_grpc import MiddlewareAgentStub
from cloud_controller.task_executor.execution_context import KubernetesExecutionContext, ExecutionContext
from cloud_controller.tasks.preconditions import compin_exists, check_phase
from cloud_controller.tasks.task import Task


class FinalizeInstanceTask(Task):

    def __init__(self, component: Component, instance_id: str):
        super(FinalizeInstanceTask, self).__init__(
            task_id=self.generate_id()
        )
        self._component: Component = component
        self._instance_id: str = instance_id
        self.add_precondition(check_phase, (self._component.application.name, self._component.name, self._instance_id,
                                            CompinPhase.INIT))

    def execute(self, context: ExecutionContext) -> bool:
        """
        Sends a FinalizeExecution command to an instance.
        :return: True if successful or if the instance had already finished, false if the instance does not respond.
        """
        stub = context.get_instance_agent(self._component.application.name, self._component.name, self._instance_id)
        stub.FinalizeExecution(DependencyAddress())
        logging.info(f"Finalize call was sent to the instance {self._instance_id} of {self._component.name}")
        return True

    def update_model(self, knowledge: Knowledge) -> None:
        compin = knowledge.actual_state.get_compin(
            self._component.application.name,
            self._component.name,
            self._component.id
        )
        if compin.phase < CompinPhase.FINALIZING:
            compin.phase = CompinPhase.FINALIZING

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._component.application.name}_{self._component.name}_" \
               f"{self._instance_id}"


class SetMongoParametersTask(Task):

    def __init__(self, component: Component, instance_id: str, key: int, mongos_ip: str):
        super(SetMongoParametersTask, self).__init__(
            task_id=self.generate_id()
        )
        self._component: Component = component
        self._instance_id: str = instance_id
        self._key = key
        self._mongos_ip = mongos_ip
        self.add_precondition(check_phase, (self._component.application.name, self._component.name, self._instance_id,
                                            CompinPhase.INIT))

    def execute(self, context: ExecutionContext) -> bool:
        parameters = MongoParameters(
            db=self._component.application.name,
            collection=self._component.name,
            shardKey=self._key,
            mongosIp=self._mongos_ip
        )
        stub = context.get_instance_agent(self._component.application.name, self._component.name, self._instance_id)
        stub.SetMongoParameters(parameters)
        logging.info(f"New mongo parameters were sent to {self._component.name}:"
                     f"{self._component.application.name}")
        return True

    def update_model(self, knowledge: Knowledge) -> None:
        compin = knowledge.actual_state.get_compin(
            self._component.application.name,
            self._component.name,
            self._component.id
        )
        compin.mongo_init_completed = True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._component.application.name}_{self._component.name}_" \
               f"{self._instance_id}"


class SetMiddlewareAddressTask(Task):

    def __init__(self, providing_component: Component, providing_instance_id: str,
                 dependent_component: Component, dependent_instance_id: str):
        super(SetMiddlewareAddressTask, self).__init__(
            task_id=self.generate_id()
        )
        self._app_name: str = providing_component.application.name
        self._providing_component: Component = providing_component
        self._providing_instance_id: str = providing_instance_id
        self._dependent_component: Component = dependent_component
        self._dependent_instance_id: str = dependent_instance_id
        self.add_precondition(check_phase, (self._app_name, self._providing_component.name,
                                            self._providing_instance_id, CompinPhase.READY))
        self.add_precondition(check_phase, (self._app_name, self._dependent_component.name,
                                            self._dependent_instance_id, CompinPhase.INIT))

    def execute(self, context: ExecutionContext) -> bool:
        providing_compin = context.knowledge.actual_state.get_instance(
            self._providing_component,
            self._providing_instance_id
        )
        address = DependencyAddress(name=self._providing_component.name, ip=providing_compin.ip)
        stub = context.get_instance_agent(self._app_name, self._dependent_component.name, self._dependent_instance_id)
        stub.SetDependencyAddress(address)
        logging.info(f"Dependency address for instance at {providing_compin.ip} set to {providing_compin.ip}.")
        return True

    def update_model(self, knowledge: Knowledge) -> None:
        # Finally, change the record in the actual state model
        knowledge.actual_state.set_dependency(
            self._app_name,
            self._dependent_component.name,
            self._dependent_instance_id,
            self._providing_component.name,
            self._providing_instance_id
        )

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._app_name}_{self._dependent_component.name}_" \
               f"{self._dependent_instance_id}_{self._providing_component.name}"


class InitializeInstanceTask(Task):

    def __init__(self, component: Component, instance_id: str, run_count: int, access_token: str, production: bool):
        super(InitializeInstanceTask, self).__init__(
            task_id=self.generate_id()
        )
        self._component: Component = component
        self._app_name: str = self._component.application.name
        self._component_name: str = self._component.name
        self._instance_id: str = instance_id
        self._run_count: int = run_count
        self._access_token: str = access_token
        self._production: bool = production
        self.add_precondition(compin_exists, (self._app_name, self._component_name, self._instance_id))
        self.add_precondition(check_phase, (self._app_name, self._component_name, self._instance_id, CompinPhase.INIT))

    def execute(self, context: ExecutionContext) -> bool:
        init_data = InstanceConfig(
            instance_id=self._instance_id,
            api_endpoint_ip=API_ENDPOINT_IP,
            api_endpoint_port=API_ENDPOINT_PORT,
            access_token=self._access_token,
            production=self._production
        )
        for probe in self._component.probes:
            probe_pb = init_data.probes.add()
            probe_pb.name = probe.name
            probe_pb.signal_set = probe.signal_set
            probe_pb.execution_time_signal = probe.execution_time_signal
            probe_pb.run_count_signal = probe.run_count_signal
            probe_pb.run_count = self._run_count
            if probe.code != "":
                probe_pb.type = ProbeType.Value('CODE')
                probe_pb.code = probe.code
                probe_pb.config = probe.config
            else:
                probe_pb.type = ProbeType.Value('PROCEDURE')
        stub: MiddlewareAgentStub = context.get_instance_agent(self._app_name, self._component_name, self._instance_id)
        stub.InitializeInstance(init_data)
        logging.info(f"Instance {self._instance_id} was successfully initialized")
        return True

    def update_model(self, knowledge: Knowledge) -> None:
        instance = knowledge.actual_state.get_compin(self._app_name, self._component_name, self._instance_id)
        assert instance is not None and isinstance(instance, ManagedCompin)
        instance.init_completed = True
        KubernetesExecutionContext.update_instance_phase(instance)

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._app_name}_{self._component.name}_{self._instance_id}"
