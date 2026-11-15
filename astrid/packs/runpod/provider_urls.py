"""REST resources shared by release and the transported watchdog."""

REST_BASE_URL = "https://api.runpod.io/v2/"
PODS_URL = REST_BASE_URL + "pods"
NETWORK_VOLUMES_URL = REST_BASE_URL + "network-volumes"
POD_URL_TEMPLATE = PODS_URL + "/{pod_id}"
POD_TERMINATE_METHOD = "DELETE"
