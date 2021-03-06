"""
Contains a mock of some functionality of the IVIS framework used for testing the IVIS interface.
"""
import logging
import time

from threading import Thread

from cloud_controller import API_ENDPOINT_IP, API_ENDPOINT_PORT
from cloud_controller.ivis.ivis_data import SAMPLE_JOB_CODE, SAMPLE_JOB_INTERVAL, \
    SAMPLE_JOB_CONFIG, SAMPLE_JOB_CREATE_SS_RESPONSE
from cloud_controller.middleware import AGENT_HOST, AGENT_PORT
from cloud_controller.middleware.helpers import connect_to_grpc_server, setup_logging
from cloud_controller.ivis.ivis_pb2 import JobAdmissionStatus, JobStatus, JobID, RunParameters, IvisJobDescription
from cloud_controller.ivis.ivis_pb2_grpc import IvisInterfaceStub

from flask import Flask, request, g, json

from cloud_controller.middleware.middleware_pb2 import InstanceConfig, ProbeType
from cloud_controller.middleware.middleware_pb2_grpc import MiddlewareAgentStub

app = Flask(__name__)
state = None

IVIS_HOST = "0.0.0.0"
IVIS_PORT = 8082

IVIS_INTERFACE_HOST = "0.0.0.0"
IVIS_INTERFACE_PORT = 62533
testing_job_agent = False

state_ = None

with app.app_context() as ctx:
    g.state = None
    ctx.push()


def log_run_status(run_status):
    logging.info(f"Run {run_status['runId']} of the job {run_status['jobId']} was completed successfully at "
                 f"{run_status['endTime']} with return code {run_status['returnCode']}")
    logging.info("-----------------------------------------------------------------------------\nSTDOUT:")
    print(run_status['output'])
    logging.info("-----------------------------------------------------------------------------\nSTDERR:")
    print(run_status['error'])


@app.route("/ccapi/run-request", methods=['POST'])
def run_request():
    global state_
    request_ = request.json
    print(request_)
    request_json = json.loads(request_['request'])
    if 'type' in request_json and request_json['type'] == "sets":
        logging.info(f"Received signal set creation request from job {request_['jobId']}. Request:\n{request_['jobId']}")
        if state_ is None:
            state_ = json.loads(SAMPLE_JOB_CREATE_SS_RESPONSE)
        return json.jsonify({'response': json.dumps(state_)})
    elif 'type' in request_json and request_json['type'] == "store":
        logging.info(f"Received store state request from job {request_['jobId']}. Request:\n{request_['jobId']}")
        state_ = request_json['state']
        return json.jsonify({'response': json.dumps({"id": request_json['id']})})
    else:
        logging.info(f"Received incorrect runtime request from job {request_['jobId']}. Request:\n{request_['jobId']}")
        return json.jsonify({'response': json.dumps({"error": "Incorrect request format"})})


@app.route("/ccapi/on-success", methods=['POST'])
def on_success():
    run_status = request.json
    log_run_status(run_status)
    return "200 OK"


@app.route("/ccapi/on-fail", methods=['POST'])
def on_fail():
    run_status = request.json
    log_run_status(run_status)
    return "200 OK"

if __name__ == "__main__":
    setup_logging()
    ivis_core_thread = Thread(target=app.run, args=(IVIS_INTERFACE_HOST, IVIS_PORT), daemon=True)
    ivis_core_thread.start()
    if not testing_job_agent:
        ivis_interface: IvisInterfaceStub = connect_to_grpc_server(IvisInterfaceStub,
                                                                   IVIS_INTERFACE_HOST, IVIS_INTERFACE_PORT)
        # Submit the job
        ivis_interface.SubmitJob(
            IvisJobDescription(
                job_id="ivisjob",
                code=SAMPLE_JOB_CODE,
                config=json.dumps(json.loads(SAMPLE_JOB_CONFIG)),
                docker_container="d3srepo/qoscloud-default"
            )
        )
        logging.info("Job has been submitted")
        # Check the job status until DEPLOYED
        status = JobStatus()
        while status.status != JobAdmissionStatus.Value("DEPLOYED"):
            status = ivis_interface.GetJobStatus(JobID(job_id="ivisjob"))
            logging.info(f"Current job admission status: {JobAdmissionStatus.Name(status.status)}")
            time.sleep(1)
    else:
        init_config = InstanceConfig(
            instance_id="ivisjob",
            api_endpoint_ip=API_ENDPOINT_IP,
            api_endpoint_port=API_ENDPOINT_PORT,
            access_token="9ae62dcf4cb3f29f3c15cd946044162dfa60468b"
        )

        probe = init_config.probes.add()
        probe.name = init_config.instance_id
        probe.type = ProbeType.Value("CODE")
        probe.code = SAMPLE_JOB_CODE
        probe.config = json.dumps(json.loads(SAMPLE_JOB_CONFIG))
        ivis_interface: MiddlewareAgentStub = connect_to_grpc_server(MiddlewareAgentStub,
                                                                        AGENT_HOST, AGENT_PORT)
        ivis_interface.InitializeInstance(init_config)
        logging.info("Job has been initialized")
    # Run the job 4 times
    for i in range(4):
        logging.info(f"Executing run {i}")
        ivis_interface.RunJob(RunParameters(job_id="ivisjob", run_id=f"run{i}",
                                            state=json.dumps(state)))
        time.sleep(SAMPLE_JOB_INTERVAL)
    if not testing_job_agent:
        # Unschedule the job
        ivis_interface.UnscheduleJob(JobID(job_id="ivisjob"))
        logging.info("Job has been unscheduled")
        status = JobStatus()
        while status.status != JobAdmissionStatus.Value("NOT_PRESENT"):
            status = ivis_interface.GetJobStatus(JobID(job_id="ivisjob"))
            logging.info(f"Current job admission status: {JobAdmissionStatus.Name(status.status)}")
