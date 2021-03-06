import logging

import yaml
from kubernetes import client

from cloud_controller import DEFAULT_SECRET_NAME
from cloud_controller.knowledge.knowledge import Knowledge
from cloud_controller.task_executor.execution_context import KubernetesExecutionContext, call_k8s_api
from cloud_controller.tasks.preconditions import compin_exists, namespace_active, namespace_exists, application_deployed
from cloud_controller.tasks.task import Task


class CreateDeploymentTask(Task):

    def __init__(self, namespace: str, deployment_name: str, deployment: str):
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._deployment = yaml.load(deployment)
        super(CreateDeploymentTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        api_response = call_k8s_api(context.extensions_api.create_namespaced_deployment,
            body=self._deployment,
            namespace=self._namespace
        )
        logging.info(f"Deployment {self._deployment_name} created. Status={api_response.status}")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}_{self._deployment_name}"


class DeleteDeploymentTask(Task):

    def __init__(self, namespace: str, deployment_name: str):
        self._namespace = namespace
        self._deployment_name = deployment_name
        super(DeleteDeploymentTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        options = client.V1DeleteOptions()
        options.propagation_policy = 'Background'
        api_response = call_k8s_api(context.extensions_api.delete_namespaced_deployment,
            name=self._deployment_name,
            namespace=self._namespace,
            body=options,
            propagation_policy='Background',
            grace_period_seconds=0
        )
        logging.info(f"Deployment {self._deployment_name} deleted. Status={api_response.status}")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}_{self._deployment_name}"


class UpdateDeploymentTask(Task):

    def __init__(self, namespace: str, deployment_name: str, deployment: str):
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._deployment = yaml.load(deployment)
        super(UpdateDeploymentTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        api_response = call_k8s_api(context.extensions_api.patch_namespaced_deployment,
            name=self._deployment_name,
            namespace=self._namespace,
            body=self._deployment
        )
        logging.info(f"Deployment {self._deployment_name} updated. Status={api_response.status}")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}_{self._deployment_name}"


class CreateServiceTask(Task):

    def __init__(self, namespace: str, service: str, app_name: str, component_name: str, instance_id: str):
        self._namespace = namespace
        self._app_name = app_name
        self._component_name = component_name
        self._instance_id = instance_id
        self._service = yaml.load(service)
        self._ip: str = ""
        super(CreateServiceTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))
        self.add_precondition(compin_exists, (app_name, component_name, instance_id))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        api_response = call_k8s_api(context.basic_api.create_namespaced_service, namespace=self._namespace, body=self._service)
        self._ip = api_response.spec.cluster_ip
        logging.info(f"Service created. Status={api_response.status}")
        return True

    def update_model(self, knowledge: Knowledge) -> None:
        compin = knowledge.actual_state.get_compin(
            self._app_name,
            self._component_name,
            self._instance_id
        )
        compin.ip = self._ip

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}_{self._app_name}_{self._component_name}_{self._instance_id}"


class DeleteServiceTask(Task):

    def __init__(self, namespace: str, service_name: str):
        self._namespace: str = namespace
        self._service_name: str = service_name
        super(DeleteServiceTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        options = client.V1DeleteOptions()
        options.propagation_policy = 'Background'
        api_response = call_k8s_api(context.basic_api.delete_namespaced_service,
            name=self._service_name,
            namespace=self._namespace,
            body=options,
            propagation_policy='Background',
            grace_period_seconds=0
        )
        logging.info(f"Service {self._service_name} deleted. Status={api_response.status}")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}_{self._service_name}"


class CreateNamespaceTask(Task):
    """
    Creates a Kubernetes namespace in the cluster.
    """

    def __init__(self, namespace: str):
        self._namespace = namespace
        super(CreateNamespaceTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(lambda x, y: not namespace_exists(x, y), (self._namespace,))
        self.add_precondition(application_deployed, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        namespace = client.V1Namespace()
        namespace.metadata = client.V1ObjectMeta()
        namespace.metadata.name = self._namespace
        api_response = call_k8s_api(context.basic_api.create_namespace, body=namespace)
        logging.info(f'Namespace {self._namespace} created.  Status={api_response.status}')
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}"

    def update_model(self, knowledge: Knowledge) -> None:
        if self._namespace in knowledge.applications:
            knowledge.applications[self._namespace].namespace_created = True


class CreateDockersecretTask(Task):
    """
    Adds a docker secret to the namespace.
    """

    def __init__(self, namespace: str, dockersecret: str):
        self._namespace = namespace
        self._dockersecret = dockersecret
        super(CreateDockersecretTask, self).__init__(
            task_id=self.generate_id()
        )
        self.add_precondition(namespace_active, (self._namespace,))

    def execute(self, context: KubernetesExecutionContext) -> bool:
        secret = client.V1Secret(
            data={
                ".dockerconfigjson": self._dockersecret
            },
            metadata=client.V1ObjectMeta(name=DEFAULT_SECRET_NAME),
            type="kubernetes.io/dockerconfigjson"
        )
        call_k8s_api(context.basic_api.create_namespaced_secret,
            namespace=self._namespace,
            body=secret
        )
        logging.info(f"Docker secret for namespace {self._namespace} created.")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}"

    def update_model(self, knowledge: Knowledge) -> None:
        if self._namespace in knowledge.applications:
            knowledge.applications[self._namespace].secret_added = True


class DeleteDockersecretTask(Task):
    """
    Deletes a docker secret from the namespace.
    """

    def __init__(self, namespace: str):
        self._namespace = namespace
        super(DeleteDockersecretTask, self).__init__(
            task_id=self.generate_id()
        )

    def execute(self, context: KubernetesExecutionContext) -> bool:
        call_k8s_api(context.basic_api.delete_namespaced_secret,
            namespace=self._namespace,
            name=DEFAULT_SECRET_NAME
        )
        logging.info(f"Docker secret for namespace {self._namespace} deleted.")
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}"


class DeleteNamespaceTask(Task):
    """
    Deletes a Kubernetes namespace from the cluster. Deletion of a namespace also deletes all the resources
    associated with it (deployments, services, etc.). Deletion happens in background, thus it may take some time
    until all the resources are cleaned.
    """

    def __init__(self, namespace: str):
        self._namespace = namespace
        super(DeleteNamespaceTask, self).__init__(
            task_id=self.generate_id()
        )

    def execute(self, context: KubernetesExecutionContext) -> bool:
        options = client.V1DeleteOptions()
        options.propagation_policy = 'Background'
        api_response = call_k8s_api(context.basic_api.delete_namespace,
            name=self._namespace,
            body=options,
            propagation_policy='Background',
            grace_period_seconds=0
        )
        logging.info(f'Namespace {self._namespace} deleted. status={api_response.status}')
        return True

    def generate_id(self) -> str:
        return f"{self.__class__.__name__}_{self._namespace}"

    def update_model(self, knowledge: Knowledge) -> None:
        if self._namespace in knowledge.applications:
            knowledge.applications[self._namespace].namespace_deleted = True
