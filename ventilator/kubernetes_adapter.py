import logging
import re
from ventilator.constants import OUTPUT_K8S_DEPLOYMENT_FILENAME

from kubernetes import client, config
from kubernetes.client.models.v1_deployment import V1Deployment
from kubernetes.client.models.v1_deployment_list import V1DeploymentList
from ventilator.exceptions import ActionNotSupported

import yaml

from ventilator.adapter import Adapter
from ventilator.configurator import ConfigFileConfigurator

try:
    from collections.abc import Mapping
except ImportError:
    from collections import Mapping


yaml.Dumper.ignore_aliases = lambda *args: True

config.load_kube_config()

core_api_instance = client.CoreV1Api()
api_instance = client.AppsV1Api()

logging = logging.getLogger(__name__)


class K8SInput(Adapter):
    type = 'kubernetes'

    def __init__(self, configfile_path):
        super().__init__()
        self.file_content = None
        self.configured_services = {}
        self.content_configured = {"deployments": [], "services": []}
        self.configurator = ConfigFileConfigurator(configfile_path)
        self.configurator.configure()
        self.configured_default_action = 'keep'
        self.deployment_list = {}

    def input(self):
        pass

    def output(self, output):
        data = camelize(self.deployment_list.to_dict())
        data = clean_null(data)
        file_path = "{}/{}".format(output, OUTPUT_K8S_DEPLOYMENT_FILENAME)
        with open(file_path, 'w') as fp:
            fp.write(yaml.dump(data))
            logging.info(
                f"Created {self.type} file in: %s",
                file_path)

    def configure(self):
        self.configured_services = yaml.load(self.configurator.configuration, Loader=yaml.Loader)
        self.configured_default_action = self.configured_services['default-action'] \
            if 'default-action' in self.configured_services else self.configured_default_action
        if self.configured_default_action not in ['keep', 'mock', 'drop']:
            raise ActionNotSupported(self.configured_default_action)
        services = [{'name': svc[0], 'namespace': svc[1]} for svc in [x.split('.') for x in list(self.configured_services['services'].keys())]]
        for service in services:
            try:
                svc = core_api_instance.read_namespaced_service(service['name'], service['namespace'])
                self.content_configured['services'].append(svc)
                selector = svc.spec.selector
                for selector_label in selector:
                    deployments = api_instance.list_namespaced_deployment(service['namespace'])
                    for deployment in deployments.items:
                        if deployment.spec.selector.match_labels[selector_label] == selector[selector_label]:
                            d = deployment.to_dict()
                            self.content_configured['deployments'].append(d)
            except Exception as e:
                logging.error(e)
        self.prepare_output()

    def prepare_output(self):
        deployment_list = {
            'api_version': 'v1',
            'kind': 'List',
            'items': []
        }
        action = self.configured_default_action
        for deployment in self.content_configured['deployments']:
            selector = deployment['spec']['selector']['match_labels']
            skipped = False
            for selector_label in selector:
                svc = deployment['spec']['selector']['match_labels'][selector_label]
                service_name = f"{svc}.{deployment['metadata']['namespace']}"
                if service_name in self.configured_services['services']:
                    if 'action' in self.configured_services['services'][service_name]:
                        action = self.configured_services['services'][service_name]['action']
                        if action == 'drop':
                            skipped = True
                            break
                    else:
                        if action == 'drop':
                            skipped = True
                            break
            if skipped:
                break
            d = self._patch_deployment(deployment, action)
            deployment_list['items'].append(
                V1Deployment(
                    api_version="apps/v1",
                    kind="Deployment",
                    metadata=d['metadata'],
                    spec=d['spec'],
                    status={}
                )
            )

        self.deployment_list = V1DeploymentList(api_version='v1', kind='List', items=deployment_list['items'])

    def _patch_deployment(self, deployment, action):
        if action == 'keep':
            del deployment['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration']
            del deployment['metadata']['annotations']['deployment.kubernetes.io/revision']
            deployment['metadata']['resource_version'] = None
            deployment['metadata']['self_link'] = None
            deployment['metadata']['uid'] = None
            deployment['metadata']['managed_fields'] = None
            deployment['metadata']['creation_timestamp'] = None
            return deployment

        name = deployment['metadata']['name']
        namespace = deployment['metadata']['namespace']
        deployment['metadata'] = self._patch_metadata(deployment)
        deployment['spec']['template']['spec']['volumes'] = self._add_volumes()
        deployment['spec']['template']['spec']['init_containers'] = self._add_init_container()
        deployment['spec']['template']['spec']['containers'][0]['image'] = "up9inc/mockintosh:latest"
        deployment['spec']['template']['spec']['containers'][0]['args'] = ["/config/mockintosh.yml", f"http://{name}.{namespace}"]
        deployment['spec']['template']['spec']['containers'][0]['env'] = [{'name': "MOCKINTOSH_FORCE_PORT", 'value': "80"}]
        deployment['spec']['template']['spec']['containers'][0]['volume_mounts'] = [{'mountPath': '/config', 'name': 'mockintosh'}]
        return deployment

    def _patch_metadata(self, deployment):
        deployment['metadata']['name'] = f"{deployment['metadata']['name']}-mock"
        deployment['metadata']['annotations']["up9-mocked-service"] = "true"
        deployment['metadata']['resource_version'] = None
        deployment['metadata']['self_link'] = None
        deployment['metadata']['uid'] = None
        deployment['metadata']['managed_fields'] = None
        deployment['metadata']['creation_timestamp'] = None
        del deployment['metadata']['annotations']['kubectl.kubernetes.io/last-applied-configuration']
        del deployment['metadata']['annotations']['deployment.kubernetes.io/revision']
        return deployment['metadata']

    def _add_volumes(self):
        return [
            {'name': 'mockintosh', 'empty_dir': {}},
            {'name': 'mockintosh-config-file', 'config_map': {'default_mode': 420, 'name': 'mockintosh-config-file'}},
            {'name': 'mockintosh-additional-files', 'config_map': {'default_mode': 420, 'name': 'mockintosh-additional-files'}
             }]

    def _add_init_container(self):
        return [{
                'name': 'load-mockintosh-files',
                'image': 'seltonfiuza/alpine-jq-bash',
                'volume_mounts': [
                    {
                        'mount_path': '/config',
                        'name': 'mockintosh'
                    },
                    {
                        'mount_path': '/mockintosh-config-file',
                        'name': 'mockintosh-config-file'
                    },
                    {
                        'mount_path': '/mockintosh-additional-files',
                        'name': 'mockintosh-additional-files'
                    }
                ]
                }]


def camelize(str_or_iter):
    # https://github.com/nficano/humps/blob/master/humps/main.py
    """Convert a string, dict, or list of dicts to camel case.
    :param str_or_iter:
      A string or iterable.
    :type str_or_iter: Union[list, dict, str]
    :rtype: Union[list, dict, str]
    :returns:
      camelized string, dictionary, or list of dictionaries.
    """
    UNDERSCORE_RE = re.compile(r"([^\_\s])[\_\s]+([^\_\s])")

    if isinstance(str_or_iter, (list, Mapping)):
        return _process_keys(str_or_iter, camelize)

    s = str(str_or_iter)
    if s.isnumeric():
        return str_or_iter

    if s.isupper():
        return str_or_iter

    return "".join(
        [
            s[0].lower() if not s[:2].isupper() else s[0],
            UNDERSCORE_RE.sub(
                lambda m: m.group(1) + m.group(2).upper(), s[1:]
            ),
        ]
    )


def _process_keys(str_or_iter, fn):
    # https://github.com/nficano/humps/blob/master/humps/main.py
    if isinstance(str_or_iter, list):
        return [_process_keys(k, fn) for k in str_or_iter]
    elif isinstance(str_or_iter, Mapping):
        return {fn(k): _process_keys(v, fn) for k, v in str_or_iter.items()}
    else:
        return str_or_iter


def clean_null_terms(d):
    clean = {}
    for k, v in d.items():
        if isinstance(v, dict):
            nested = clean_null_terms(v)
            if len(nested.keys()) > 0:
                clean[k] = nested
        elif v is not None:
            clean[k] = v
    return clean


def clean_null(data):
    for idx, d in enumerate(data['items']):
        data['items'][idx] = clean_null_terms(data['items'][idx])
        containers = data['items'][idx]['spec']['template']['spec']['containers']
        for container_idx, container in enumerate(containers):
            containers[container_idx] = clean_null_terms(containers[container_idx])
        data['items'][idx]['spec']['template']['spec']['containers'] = containers
        if 'volumes' in data['items'][idx]['spec']['template']['spec']:
            volumes = data['items'][idx]['spec']['template']['spec']['volumes']
            for volume_idx, volume in enumerate(volumes):
                volumes[volume_idx] = clean_null_terms(volumes[volume_idx])
            data['items'][idx]['spec']['template']['spec']['volumes'] = volumes
    return data
