""" Utilities for interacting with a DC/OS instance via REST API

Most DC/OS deployments will have auth enabled, so this module includes
DcosUser and DcosAuth to be attached to a DcosApiSession. Additionally,
it is sometimes necessary to query specific nodes within a DC/OS cluster,
so there is ARNodeApiClientMixin to allow querying nodes without boilerplate
to set the correct port and scheme.
"""
import copy
import logging
import os
from typing import List, Optional

import requests
import retrying

from dcos_test_utils import (
    diagnostics,
    jobs,
    marathon,
    package,
    helpers
)

log = logging.getLogger(__name__)


class DcosUser:
    """ Representation of a DC/OS user used for authentication

    :param credentials: representation of the JSON used to log in
    :type credentials: dict
    """
    def __init__(self, credentials: dict):
        self.credentials = credentials
        self.auth_token = None

    @property
    def auth_header(self) -> dict:
        """ Property for the auth header provided at authentication time

        :returns: representation of HTTP headers to use
        :rtype: dict
        """
        return {'Authorization': 'token={}'.format(self.auth_token)}


class DcosAuth(requests.auth.AuthBase):
    """ Child of AuthBase for specifying how to handle DC/OS auth per request

    :param auth_token: token generated by authenticating with access control
    :type auth_token: str
    """
    def __init__(self, auth_token: str):
        self.auth_token = auth_token

    def __call__(self, request):
        request.headers['Authorization'] = 'token={}'.format(self.auth_token)
        return request


class Exhibitor(helpers.RetryCommonHttpErrorsMixin, helpers.ApiClientSession):
    """ Exhibitor can have a password set, in which case a different auth model is needed

    :param default_url: Url object for the exhibitor instance
    :type default_url:  helpers.Url
    :param session: optional session for bootstrapping this session (a new one is created otherwise)
    :type session: requests.Session
    :param exhibitor_admin_password: password for exhibitor (not always set)
    :type exhibitor_admin_password: str
    """
    def __init__(self, default_url: helpers.Url, session: Optional[requests.Session]=None,
                 exhibitor_admin_password: Optional[str]=None):
        super().__init__(default_url)
        if session is not None:
            self.session = session
        if exhibitor_admin_password is not None:
            # Override auth to use HTTP basic auth with the provided admin password.
            self.session.auth = requests.auth.HTTPBasicAuth('admin', exhibitor_admin_password)


class DcosApiSession(helpers.ARNodeApiClientMixin, helpers.RetryCommonHttpErrorsMixin, helpers.ApiClientSession):
    """Proxy class for DC/OS clusters. If any of the host lists (masters,
    slaves, public_slaves) are provided, the wait_for_dcos function of this
    class will wait until provisioning is complete. If these lists are not
    provided, then there is no ground truth and the cluster will be assumed
    the be in a completed state.

    :param dcos_url: address for the DC/OS web UI.
    :type dcos_url: helpers.Url
    :param masters: list of Mesos master advertised IP addresses.
    :type masters: list
    :param slaves: list of Mesos slave/agent advertised IP addresses.
    :type slaves: list
    :param public_slaves: list of public Mesos slave/agent advertised IP addresses.
    :type public_slaves: list
    :param auth_user: use this user's auth for all requests.
        Note: user must be authenticated explicitly or call self.wait_for_dcos()
    :type auth_user: DcosUser
    """
    def __init__(
            self,
            dcos_url: str,
            masters: Optional[List[str]],
            slaves: Optional[List[str]],
            public_slaves: Optional[List[str]],
            auth_user: Optional[DcosUser],
            exhibitor_admin_password: Optional[str]=None):
        super().__init__(helpers.Url.from_string(dcos_url))
        self.master_list = masters
        self.slave_list = slaves
        self.public_slave_list = public_slaves
        self.auth_user = auth_user
        self.exhibitor_admin_password = exhibitor_admin_password

    @classmethod
    def create(cls):
        """ Uses environment variables defined in :func:`DcosApiSession.get_args_from_env`
        to create a new DcosApiSession instance
        """
        api = cls(**cls.get_args_from_env())
        api.login_default_user()
        return api

    @staticmethod
    def get_args_from_env() -> dict:
        """ This method will use environment variables to generate
        the arguments necessary to initialize a :class:`DcosApiSession`

        Environment Variables:

        * **DCOS_DNS_ADDRESS**: the URL for the DC/OS cluster to be used. If not set, leader.mesos will be used
        * **DCOS_ACS_TOKEN**: authentication token that can be taken from dcos-cli after login in order to authenticate
          If not given, a hard-coded dummy login token will be used to create the authentication token.
        * **MASTER_HOSTS**: a complete list of the expected master IPs (optional)
        * **SLAVE_HOSTS**: a complete list of the expected private slaves IPs (optional)
        * **PUBLIC_SLAVE_HOSTS**: a complete list of the public slave IPs (optional)

        :returns: arguments to initialize a DcosApiSesssion
        :rtype: dict
        """
        dcos_acs_token = os.getenv('DCOS_ACS_TOKEN')
        if dcos_acs_token is None:
            auth_user = DcosUser(helpers.CI_CREDENTIALS)
        else:
            auth_user = DcosUser({'token': ''})
            auth_user.auth_token = dcos_acs_token

        masters = os.getenv('MASTER_HOSTS')
        slaves = os.getenv('SLAVE_HOSTS')
        windows_slaves = os.getenv('WINDOWS_HOSTS')
        public_slaves = os.getenv('PUBLIC_SLAVE_HOSTS')
        windows_public_slaves = os.getenv('WINDOWS_PUBLIC_HOSTS')
        if windows_slaves:
            slaves = ",".join((slaves, windows_slaves))
        if windows_public_slaves:
            public_slaves = ",".join((public_slaves, windows_public_slaves))
        return {
            'auth_user': auth_user,
            'dcos_url': os.getenv('DCOS_DNS_ADDRESS', 'http://leader.mesos'),
            'masters': masters.split(',') if masters is not None else None,
            'slaves': slaves.split(',') if slaves is not None else [],
            'public_slaves': public_slaves.split(',') if public_slaves is not None else []}

    @property
    def masters(self) -> List[str]:
        """ Property which returns a sorted list of master IP strings for this cluster
        """
        return sorted(self.master_list)

    @property
    def slaves(self) -> List[str]:
        """ Property which returns a sorted list of private slave  IP strings for this cluster
        """
        return sorted(self.slave_list)

    @property
    def public_slaves(self) -> List[str]:
        """ Property which retruns a sorted list of public slave IP strings for this cluster
        """
        return sorted(self.public_slave_list)

    @property
    def all_slaves(self) -> List[str]:
        """ Property which returns a sorted list of all slave IP strings for this cluster
        """
        return sorted(self.slaves + self.public_slaves)

    def set_node_lists_if_unset(self):
        """ Sets the expected cluster topology to be the observed cluster
        topology from exhibitor and mesos. I.E. if masters, slave, or
        public_slaves were not provided, accept whatever is currently available
        """
        if self.master_list is None:
            log.debug('Master list not provided, setting from exhibitor...')
            r = self.get('/exhibitor/exhibitor/v1/cluster/list')
            r.raise_for_status()
            self.master_list = sorted(r.json()['servers'])
            log.info('Master list set as: {}'.format(self.masters))
        if self.slave_list is not None and self.public_slave_list is not None:
            return
        r = self.get('/mesos/slaves')
        r.raise_for_status()
        slaves_json = r.json()['slaves']
        if self.slave_list is None:
            log.debug('Private slave list not provided; fetching from mesos...')
            self.slave_list = sorted(
                [s['hostname'] for s in slaves_json if s['attributes'].get('public_ip') != 'true'])
            log.info('Private slave list set as: {}'.format(self.slaves))
        if self.public_slave_list is None:
            log.debug('Public slave list not provided; fetching from mesos...')
            self.public_slave_list = sorted(
                [s['hostname'] for s in slaves_json if s['attributes'].get('public_ip') == 'true'])
            log.info('Public slave list set as: {}'.format(self.public_slaves))

    @retrying.retry(wait_fixed=5000, stop_max_delay=120 * 1000)
    def login_default_user(self):
        """retry default user login because in some deployments,
        the login endpoint might not be routable immediately
        after Admin Router is up.
        We wait 5 seconds between retries to avoid DoS-ing the IAM.

        Raises:
            requests.HTTPException: In case the login fails due to wrong
                username or password of the default user.
        """
        if self.auth_user is None:
            log.info('No credentials are defined')
            return

        if self.auth_user.auth_token is not None:
            log.info('Already logged in as default user')
            self.session.auth = DcosAuth(self.auth_user.auth_token)
            return

        log.info('Attempting default user login')
        # Explicitly request the default user authentication token by logging in.
        r = self.post('/acs/api/v1/auth/login', json=self.auth_user.credentials, auth=None)
        r.raise_for_status()
        log.info('Received authentication token: {}'.format(r.json()))
        self.auth_user.auth_token = r.json()['token']
        log.info('Login successful')
        # Set requests auth
        self.session.auth = DcosAuth(self.auth_user.auth_token)

    @retrying.retry(wait_fixed=1000,
                    retry_on_result=lambda ret: ret is False,
                    retry_on_exception=lambda x: False)
    def _wait_for_marathon_up(self):
        r = self.get('/marathon/v2/info')
        # http://mesosphere.github.io/marathon/api-console/index.html
        # 200 at /marathon/v2/info indicates marathon is up.
        if r.status_code == 200:
            log.info("Marathon is up.")
            return True
        else:
            msg = "Waiting for Marathon, resp code is: {}"
            log.info(msg.format(r.status_code))
            return False

    @retrying.retry(wait_fixed=1000)
    def _wait_for_zk_quorum(self):
        """Queries exhibitor to ensure all master ZKs have joined
        """
        r = self.get('/exhibitor/exhibitor/v1/cluster/status')
        if not r.ok:
            log.warning('Exhibitor status not available')
            r.raise_for_status()
        status = r.json()
        log.info('Exhibitor cluster status: {}'.format(status))
        zk_nodes = sorted([n['hostname'] for n in status])
        # zk nodes will be private but masters can be public
        assert len(zk_nodes) == len(self.masters), 'ZooKeeper has not formed the expected quorum'

    @retrying.retry(wait_fixed=1000,
                    retry_on_result=lambda ret: ret is False,
                    retry_on_exception=lambda x: False)
    def _wait_for_slaves_to_join(self):
        r = self.get('/mesos/master/slaves')
        if r.status_code != 200:
            msg = "Mesos master returned status code {} != 200 "
            msg += "continuing to wait..."
            log.info(msg.format(r.status_code))
            return False
        data = r.json()
        # Check that there are all the slaves the test knows about. They are all
        # needed to pass the test.
        num_slaves = len(data['slaves'])
        if num_slaves >= len(self.all_slaves):
            msg = "Sufficient ({} >= {}) number of slaves have joined the cluster"
            log.info(msg.format(num_slaves, self.all_slaves))
            return True
        else:
            msg = "Current number of slaves: {} < {}, continuing to wait..."
            log.info(msg.format(num_slaves, self.all_slaves))
            return False

    @retrying.retry(wait_fixed=1000,
                    retry_on_result=lambda ret: ret is False,
                    retry_on_exception=lambda x: False)
    def _wait_for_adminrouter_up(self):
        try:
            # Yeah, we can also put it in retry_on_exception, but
            # this way we will loose debug messages
            self.get('/')
        except requests.ConnectionError as e:
            msg = "Cannot connect to nginx, error string: '{}', continuing to wait"
            log.info(msg.format(e))
            return False
        else:
            log.info("Nginx is UP!")
            return True

    # Retry if returncode is False, do not retry on exceptions.
    # We don't want to infinite retries while waiting for agent endpoints,
    # when we are retrying on both HTTP 502 and 404 statuses
    # Added a stop_max_attempt to 60.
    @retrying.retry(wait_fixed=2000,
                    retry_on_result=lambda r: r is False,
                    retry_on_exception=lambda _: False,
                    stop_max_attempt_number=60)
    def _wait_for_srouter_slaves_endpoints(self):
        # Get currently known agents. This request is served straight from
        # Mesos (no AdminRouter-based caching is involved).
        r = self.get('/mesos/master/slaves')

        # If the agent has restarted, the mesos endpoint can give 502
        # for a brief moment.
        if r.status_code == 502:
            return False

        assert r.status_code == 200

        data = r.json()
        # only check against the slaves we expect to be in the cluster
        # so we can check that cluster has returned after a failure
        # in which case will will have new slaves and dead slaves
        slaves_ids = sorted(x['id'] for x in data['slaves'] if x['hostname'] in self.all_slaves)

        for slave_id in slaves_ids:
            in_progress_status_codes = (
                # AdminRouter's slave endpoint internally uses cached Mesos
                # state data. That is, slave IDs of just recently joined
                # slaves can be unknown here. For those, this endpoint
                # returns a 404. Retry in this case, until this endpoint
                # is confirmed to work for all known agents.
                404,
                # During a node restart or a DC/OS upgrade, this
                # endpoint returns a 502 temporarily, until the agent has
                # started up and the Mesos agent HTTP server can be reached.
                502,
                # We have seen this endpoint return 503 with body
                # b'Agent has not finished recovery' on a cluster which
                # later became healthy.
                503,
            )
            uri = '/slave/{}/slave%281%29/state'.format(slave_id)
            r = self.get(uri)
            if r.status_code in in_progress_status_codes:
                return False
            assert r.status_code == 200, (
                'Expecting status code 200 for GET request to {uri} but got '
                '{status_code} with body {content}'
            ).format(uri=uri, status_code=r.status_code, content=r.content)
            data = r.json()
            assert "id" in data
            assert data["id"] == slave_id

    @retrying.retry(wait_fixed=2000,
                    retry_on_result=lambda r: r is False,
                    retry_on_exception=lambda _: False)
    def _wait_for_metronome(self):
        # Although this is named `wait_for_metronome`, some of the waiting
        # done in this function is, implicitly, for Admin Router.
        r = self.get('/service/metronome/v1/jobs')
        expected_error_codes = {
            404: ('It may be the case that Admin Router is returning a 404 '
                  'despite the Metronome service existing because it uses a cache. '
                  'This cache is updated periodically.'),
            504: ('Metronome is returning a Gateway Timeout Error.'
                  'It may be that the service is still starting up.')
        }
        log.info('Metronome status code:')
        log.info(r.status_code)
        log.info('Metronome response body:')
        log.info(r.text)

        if r.status_code in expected_error_codes or r.status_code >= 500:
            error_message = expected_error_codes.get(r.status_code)
            if error_message:
                log.info(error_message)
            log.info('Continuing to wait for Metronome')
            return False

        assert r.status_code == 200, "Expecting status code 200 for Metronome but got {} with body {}"\
            .format(r.status_code, r.content)

    @retrying.retry(wait_fixed=2000,
                    retry_on_result=lambda r: r is False,
                    retry_on_exception=lambda _: False)
    def _wait_for_all_healthy_services(self):
        r = self.health.get('/units')
        r.raise_for_status()

        all_healthy = True
        for unit in r.json()['units']:
            if unit['health'] != 0:
                log.info("{} service health: {}".format(unit['id'], unit['health']))
                all_healthy = False

        return all_healthy

    def wait_for_dcos(self):
        """ This method will wait for:
        * cluster endpoints to come up immediately after deployment has completed
        * authentication with DC/OS to be successful
        * all DC/OS services becoming healthy
        * all explicitly declared nodes register to register
        """
        self._wait_for_adminrouter_up()
        self.login_default_user()
        wait_for_hosts = os.getenv('WAIT_FOR_HOSTS', 'true') == 'true'
        master_list_set = self.master_list is not None
        slave_list_set = self.slave_list is not None
        public_slave_list_set = self.public_slave_list is not None
        node_lists_set = all([master_list_set, slave_list_set, public_slave_list_set])
        if wait_for_hosts and not node_lists_set:
            raise Exception(
                'This cluster is set to wait for hosts, however, not all host lists '
                'were supplied. Please set all three environment variables of MASTER_HOSTS, '
                'SLAVE_HOSTS, and PUBLIC_SLAVE_HOSTS to the appropriate cluster IPs (comma separated). '
                'Alternatively, set WAIT_FOR_HOSTS=false in the environment to use whichever hosts '
                'are currently registered.')
        self.set_node_lists_if_unset()
        self._wait_for_marathon_up()
        self._wait_for_zk_quorum()
        self._wait_for_slaves_to_join()
        self._wait_for_srouter_slaves_endpoints()
        self._wait_for_metronome()
        self._wait_for_all_healthy_services()

    def copy(self):
        """ Create a new client session from this one without cookies, with the authentication intact.
        """
        new = copy.deepcopy(self)
        new.session.cookies.clear()
        return new

    def get_user_session(self, user: DcosUser):
        """Returns a copy of this client session with a new user

        :param user: The user with which the new DcosApiSession will authenticate (can be None)
        :type user: DcosUser
        """
        new = self.copy()
        new.session.auth = None
        new.auth_user = None
        if user is not None:
            new.auth_user = user
            new.login_default_user()
        return new

    @property
    def exhibitor(self):
        """ Property which creates a new :class:`Exhibitor`
        """
        if self.exhibitor_admin_password is None:
            # No basic HTTP auth. Access Exhibitor via the adminrouter.
            default_url = self.default_url.copy(path='exhibitor')
        else:
            # Exhibitor is protected with HTTP basic auth, which conflicts with adminrouter's auth. We must bypass
            # the adminrouter and access Exhibitor directly.
            default_url = helpers.Url.from_string('http://{}:8181'.format(self.masters[0]))

        return Exhibitor(
            default_url=default_url,
            session=self.copy().session,
            exhibitor_admin_password=self.exhibitor_admin_password)

    @property
    def marathon(self):
        """ Property which returns a :class:`dcos_test_utils.marathon.Marathon`
        derived from this session
        """
        return marathon.Marathon(
            default_url=self.default_url.copy(path='marathon'),
            session=self.copy().session)

    @property
    def metronome(self):
        """ Property which returns a copy of this session where all requests are
        prefaced with /service/metronome
        """
        new = self.copy()
        new.default_url = self.default_url.copy(path='service/metronome')
        return new

    @property
    def jobs(self):
        """ Property which returns a :class:`dcos_test_utils.jobs.Jobs`
        derived from this session
        """
        return jobs.Jobs(
                default_url=self.default_url.copy(path='service/metronome'),
                session=self.copy().session)

    @property
    def cosmos(self):
        """ Property which returns a :class:`dcos_test_utils.package.Cosmos`
        derived from this session
        """
        return package.Cosmos(
            default_url=self.default_url.copy(path="package"),
            session=self.copy().session)

    @property
    def health(self):
        """ Property which returns a :class:`dcos_test_utils.diagnostics.Diagnostics`
        derived from this session
        """
        health_url = self.default_url.copy(query='cache=0', path='system/health/v1')
        return diagnostics.Diagnostics(
            health_url,
            self.masters,
            self.all_slaves,
            session=self.copy().session)

    @property
    def logs(self):
        """ Property which returns a copy of this session where all requests are
        prefaced with /system/v1/logs
        """
        new = self.copy()
        new.default_url = self.default_url.copy(path='system/v1/logs')
        return new

    @property
    def metrics(self):
        """ Property which returns a copy of this session where all requests are
        prefaced with /system/v1/metrics/v0
        """
        new = self.copy()
        new.default_url = self.default_url.copy(path='/system/v1/metrics/v0')
        return new

    def metronome_one_off(
            self,
            job_definition: dict,
            timeout: int=300,
            ignore_failures: bool=False) -> None:
        """Run a job on metronome and block until it returns success

        :param job_definition: metronome job JSON to be triggered once
        :type job_definition: dict
        :param timeout: how long to wait (in seconds) for the job to complete
        :type timeout: int
        :param ignore_failures: if True, failures will not block or raise an exception
        :type ignore_failures: bool
        """
        _jobs = self.jobs
        job_id = job_definition['id']

        log.info('Creating metronome job: ' + repr(job_definition))
        _jobs.create(job_definition)
        log.info('Starting metronome job')
        status, run, job = _jobs.run(job_id, timeout=timeout)
        if not status:
            log.info('Job failed, run info: {}'.format(run))
            if not ignore_failures:
                raise Exception('Metronome job failed!: ' + repr(job))
        else:
            log.info('Metronome one-off successful')
        log.info('Deleting metronome one-off')
        _jobs.destroy(job_id)

    def mesos_sandbox_directory(self, slave_id: str, framework_id: str, task_id: str) -> str:
        """ Gets the mesos sandbox directory for a specific task

        :param slave_id: slave ID to pull sandbox from
        :type slave_id: str
        :param framework_id: framework_id to pull sandbox from
        :type frameowork_id: str
        :param task_id: task ID to pull directory sandbox from
        :type task_id: str

        :returns: the directory of the sandbox
        :rtype: str
        """
        r = self.get('/agent/{}/state'.format(slave_id))
        r.raise_for_status()
        agent_state = r.json()

        try:
            framework = next(f for f in agent_state['frameworks'] if f['id'] == framework_id)
        except StopIteration:
            raise Exception('Framework {} not found on agent {}'.format(framework_id, slave_id))

        try:
            executor = next(e for e in framework['executors'] if e['id'] == task_id)
        except StopIteration:
            raise Exception('Executor {} not found on framework {} on agent {}'.format(task_id, framework_id, slave_id))

        return executor['directory']

    def mesos_sandbox_file(self, slave_id: str, framework_id: str, task_id: str, filename: str) -> str:
        """ Gets a specific file from a task sandbox and returns the text content

        :param slave_id: ID of the slave running the task
        :type slave_id: str
        :param framework_id: ID of the framework of the task
        :type framework_id: str
        :param task_id: ID of the task
        :type task_id: str
        :param filename: filename in the sandbox
        :type filename: str

        :returns: sandbox text contents
        """
        r = self.get(
            '/agent/{}/files/download'.format(slave_id),
            params={'path': self.mesos_sandbox_directory(slave_id, framework_id, task_id) + '/' + filename}
        )
        r.raise_for_status()
        return r.text

    def mesos_pod_sandbox_directory(self, slave_id: str, framework_id: str, executor_id: str, task_id: str) -> str:
        """ Gets the mesos sandbox directory for a specific task in a pod which is currently running

        :param slave_id: slave ID to pull sandbox from
        :type slave_id: str
        :param framework_id: framework_id to pull sandbox from
        :type frameowork_id: str
        :param executor_id: executor ID to pull directory sandbox from
        :type executor_id: str
        :param task_id: task ID to pull directory sandbox from
        :type task_id: str

        :returns: the directory of the sandbox
        :rtype: str
        """
        return '{}/tasks/{}'.format(self.mesos_sandbox_directory(slave_id, framework_id, executor_id), task_id)

    def mesos_pod_sandbox_file(
            self,
            slave_id: str,
            framework_id: str,
            executor_id: str,
            task_id: str,
            filename: str) -> str:
        """ Gets a specific file from a currently-running pod's task sandbox and returns the text content

        :param slave_id: ID of the slave running the task
        :type slave_id: str
        :param framework_id: ID of the framework of the task
        :type framework_id: str
        :param executor_id: ID of the executor
        :type executor_id: str
        :param task_id: ID of the task
        :type task_id: str
        :param filename: filename in the sandbox
        :type filename: str

        :returns: sandbox text contents
        """
        r = self.get(
            '/agent/{}/files/download'.format(slave_id),
            params={'path': self.mesos_pod_sandbox_directory(
                slave_id, framework_id, executor_id, task_id) + '/' + filename}
        )
        r.raise_for_status()
        return r.text

    def get_version(self) -> str:
        """ Queries the DC/OS version endpoint to get DC/OS version

        :returns: version for DC/OS
        """
        version_metadata = self.get('/dcos-metadata/dcos-version.json')
        version_metadata.raise_for_status()
        data = version_metadata.json()
        return data["version"]
