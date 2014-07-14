#!/usr/bin/python

import argparse
import json
import os
import tempfile
import time
import sys
import yaml

from fabric import network as fabric_network
from fabric.api import env, get, put, run, settings
from fabric.context_managers import hide
from glanceclient import client as glance_client
from keystoneclient.v2_0 import client as keystone_client
from novaclient.v1_1 import client as nova_client


# Openstack-related constants
OPENSTACK_ACTIVE_KEYWORD = 'ACTIVE'
OPENSTACK_BUILD_KEYWORD = 'BUILD'
DEFAULT_FLAVOR = 'm1.medium'
DEFAULT_SEC_GROUP = 'jcline-pulp'
META_USER_KEYWORD = 'user'
META_DISTRIBUTION_KEYWORD = 'distribution'

# Fabric configuration
env.connection_attempts = 4
env.timeout = 30
env.disable_known_hosts = True
env.abort_on_prompts = True

# Constants for pulp-automation YAML configuration
CONSUMER_YAML_KEY = 'consumers'
SERVER_YAML_KEY = 'pulp'
ROLES_KEY = 'ROLES'

# The Puppet module dependencies
PUPPET_MODULES = [
    'puppetlabs-stdlib',
    'puppetlabs-mongodb',
    'dprince-qpid',
    'jcline-pulp'
]

# The dependencies for pulp-automation
PULP_AUTO_DEPS = [
    'gcc',
    'git',
    'm2crypto',
    'python-devel',
    'python-pip',
    'python-qpid'
]

# Configuration commands
AUTHORIZE_ROOT_SSH = 'sudo cp ~/.ssh/authorized_keys /root/.ssh/authorized_keys'
TEMPORARY_MANIFEST_LOCATION = '/tmp/manifest.pp'
REMOTE_PUPPET_APPLY = 'sudo puppet apply ' + TEMPORARY_MANIFEST_LOCATION
PUPPET_MODULE_INSTALL = 'sudo puppet module install --force '
YUM_INSTALL_TEMPLATE = 'sudo yum -y install %s'

# The version of gevent provided by Fedora/RHEL is too old, so force it to update here.
# It seems like setup.py needs to be run twice for now.
INSTALL_TEST_SUITE = 'git clone https://github.com/RedHatQE/pulp-automation.git \
&& sudo pip install -U gevent && cd pulp-automation && sudo python ./setup.py install \
&& sudo python ./setup.py install'


# OS1 image metadata keywords
OS_NAME_KEY = 'pulp_os_name'
OS_VERSION_KEY = 'pulp_os_version'

HOSTS_TEMPLATE = "echo '%(ip)s    %(hostname)s %(hostname)s.novalocal' | sudo tee -a /etc/hosts"


def os1_authenticate(username=None, password=None, tenant_id=None, tenant_name=None, auth_url=None):
    """
    Authenticates with keystone, nova, and glance using the given arguments, or using
    environment variables if an argument is None. These are the same environment variables
    used by the nova and glance clients.

    :param username:    The username to use when authenticating with keystone and nova
    :type  username:    str
    :param password:    The password to use when authenticating with keystone and nova
    :type  password:    str
    :param tenant_id:   The tenant id of the user
    :type  tenant_id:   str
    :param tenant_name: The tenant name of the user
    :type  tenant_name: str
    :param auth_url:    The location of the authentication server
    :type  auth_url:    str

    :return: A tuple of the clients in the following order: (glance, keystone, nova)
    :rtype:  tuple
    """
    if not username:
        username = os.environ.get('OS_USERNAME')
    if not password:
        password = os.environ.get('OS_PASSWORD')
    if not tenant_id:
        tenant_id = os.environ.get('OS_TENANT_ID')
    if not auth_url:
        auth_url = os.environ.get('OS_AUTH_URL')
    if not tenant_name:
        tenant_name = os.environ.get('OS_TENANT_NAME')

    keystone = keystone_client.Client(username=username, password=password, tenant_id=tenant_id,
                                      tenant_name=tenant_name, auth_url=auth_url)
    keystone.authenticate()
    nova = nova_client.Client(username, password, tenant_name, tenant_id=tenant_id, auth_url=auth_url)
    nova.authenticate()
    glance_url = keystone.service_catalog.get_endpoints()['image'][0]['adminURL']
    glance = glance_client.Client('1', endpoint=glance_url, token=keystone.auth_token)

    return glance, keystone, nova


def create_instance(nova, image_id, instance_name, security_groups, flavor_name, key_name, cloud_config=None):
    """
    Builds an instance using the given nova client. This call will block until Openstack says the
    instance is 'active'. Note: this just means Openstack has successfully started the boot process.
    It has not successfully booted, nor has cloud-init run, so ssh via public key authentication will
    not work when this returns.

    :param nova:            The authenticated nova client to use
    :type  nova:            novaclient.v1_1.client.Client
    :param image_id:        The id of the image in Glance to boot
    :type  image_id:        str
    :param instance_name:   The human-readable name of the instance
    :type  instance_name:   str
    :param security_groups: One or more security groups to apply to the instance. This should be a
                            list of the security group names
    :type  security_groups: list
    :param flavor_name:     The name of the flavor to use for this instance
    :type  flavor_name:     str
    :param key_name:        The name of the key pair to use for this instance. This should exist in
                            Openstack already.
    :type  key_name:        str
    :param cloud_config:    The path to a valid user data file to pass to cloud-init. This is optional.
    :type  cloud_config:    str

    :return: The instance
    :rtype:  nova.servers.Server
    """
    # Set up instance configuration
    flavor = nova.flavors.find(name=flavor_name)
    if not isinstance(security_groups, list):
        security_groups = [security_groups]
    user_data = None
    if cloud_config:
        user_data = open(cloud_config)

    server = nova.servers.create(instance_name, image_id, flavor, security_groups=security_groups,
                                 key_name=key_name, userdata=user_data)
    if user_data:
        user_data.close()

    # Hang out until Openstack says the VM is up or we give up
    while server.status == OPENSTACK_BUILD_KEYWORD:
        time.sleep(10)
        server = nova.servers.get(server.id)

    return server


def create_image(glance, image_location):
    """
    Upload an image from image_location into glance

    :param glance:          An instance of the glance client
    :type  glance:          glanceclient.client.Client
    :param image_location:  The path to image. This can be absolute or relative.
    :type  image_location:  str

    :return: A representation of the uploaded image
    :rtype:
    """
    image_name = os.path.basename(image_location)
    image_attributes = {
        'name': 'automated-pulp-' + image_name,
        'container_format': 'bare',
        'disk_format': 'qcow2'
    }

    with open(image_location) as image_data:
        new_image = glance.images.create(**image_attributes)
        new_image.update(data=image_data)

    return new_image


def take_snapshot(nova, server, snapshot_name, metadata=None):
    """
    Take a snapshot of given server. This call will hang until Openstack
    reports that the snapshot is active.

    :param nova:            An instance of the nova client
    :type  nova:            novaclient.v1_1.client.Client
    :param server:          The active instance to take a snapshot of
    :type  server:          novaclient.v1_1.servers.Server
    :param snapshot_name:   The human-readable name to assign to the snapshot.
    :type  snapshot_name:   str
    :param metadata:        A dictionary to use as metadata for the image snapshot.
    :type  metadata:        dict

    :return: An Image instance representing the snapshot taken
    :rtype:  novaclient.v1_1.images.Image
    """
    snapshot_id = server.create_image(snapshot_name)
    snapshot = nova.images.get(snapshot_id)

    # Wait for the snapshot to complete
    while snapshot.status != OPENSTACK_ACTIVE_KEYWORD:
        time.sleep(10)
        snapshot = nova.images.get(snapshot_id)

    nova.images.ImageManager.set_meta(snapshot_id, metadata)

    return snapshot


def fabric_yum_install(host_string, key_file, package_name):
    """
    Install a package on a host using yum

    :param host_string:     The host to connect to: in the form 'user@host'
    :type  host_string:     str
    :param key_file:        The absolute path to the private key to use when connecting as 'user'
    :type  key_file:        str
    :param package_name:    The name of the package to install
    :type  package_name:    str
    """
    try:
        with settings(hide('output'), host_string=host_string, key_file=key_file, quiet=True):
            run('sudo yum -y install ' + package_name)
    finally:
        fabric_network.disconnect_all()


def fabric_apply_puppet(host_string, key_file, local_module, remote_location=TEMPORARY_MANIFEST_LOCATION):
    """
    Apply a puppet manifest to the given host. It is your responsibility to install
    any puppet module dependencies, and to ensure puppet is installed.

    :param host_string:     The host to connect to: in the form 'user@host'
    :type  host_string:     str
    :param key_file:        The absolute path to the private key to use when connecting as 'user'
    :type  key_file:        str
    :param local_module:    The absolute path to the puppet module to put on the remote host
    :param remote_location: the location to put this puppet module on the remote host
    """
    try:
        with settings(host_string=host_string, key_file=key_file, quiet=True):
            put(local_module, remote_location)
            run('sudo puppet apply ' + remote_location)
    finally:
        fabric_network.disconnect_all()


def fabric_confirm_ssh_key(host_string, key_file):
    """
    This is a utility to make sure fabric can ssh into the host with the given key. This is useful
    when a remote host is being set up by cloud-init, which can bring the ssh server up before
    installing the public key. It will try for 300 seconds, after which it will raise a SystemExit.

    :param host_string:     The host to connect to: in the form 'user@host'
    :type  host_string:     str
    :param key_file:        The absolute path to the private key to use when connecting as 'user'
    :type  key_file:        str

    :raises RuntimeError: if it was unable to ssh in after 300 seconds
    """
    # It can take some time for the init scripts to insert the public key into an instance
    # Abort on prompt is set, so catch the SystemExit exception and sleep for a while.
    with settings(hide('everything'), host_string=host_string, key_file=key_file, quiet=True):
        for x in xrange(0, 30):
            try:
                run('whoami')
                break
            except SystemExit:
                time.sleep(10)
        else:
            raise RuntimeError('Unable to SSH into ' + host_string + ' using ' + key_file)


def get_instance_ip(instance):
    """
    Get an OS1 Internal public ip address

    :param instance: a server instance with a public ip address
    :type  instance: nova.servers.Server

    :return: the public ip address
    :rtype:  str
    """
    public_ip = instance.networks['os1-internal-1319'][1]
    return public_ip.encode('ascii')


def inject_external_fact(host_string, key_file, facts):
    """
    Make Puppet facts available to a remote host. Note that this will simply dump
    the file in /etc/facter/facts.d/facts.json which might overwrite other facts.

    :param host_string: remote host to add facts to; the expected format is 'user@ip'
    :param key_file: the absolute or relative path to the ssh key to use
    :param facts: a dictionary of facts; each key is a Puppet fact. The key
    names must follow the Puppet fact name rules.
    """
    with settings(host_string=host_string, key_file=key_file):
        fabric_confirm_ssh_key(host_string, key_file)

        # Write the temporary json file to dump
        file_descriptor, path = tempfile.mkstemp()
        os.write(file_descriptor, json.dumps(facts))
        os.close(file_descriptor)

        # Place it on the remote host and clean up
        put(path)
        temp_filename = os.path.basename(path)
        run('sudo mkdir -p /etc/facter/facts.d/')
        run('sudo mv ' + temp_filename + ' /etc/facter/facts.d/facts.json')
        os.remove(path)


def configure_server(host_string, key_file, repository, puppet_manifest, server_hostname):
    """
    Set up a Pulp server using Fabric and a puppet module. Fabric will apply the given
    host name, ensure puppet and any modules declared in PUPPET_MODULES are installed,
    and will then apply the puppet manifest.

    :param host_string:     The host string for the server. This should be in the format 'user@ip'
    :type  host_string:     str
    :param key_file:        The path to the private key to use when logging into the server.
    :type  key_file:        str
    :param repository:      The path to the repository to install from
    :type  repository:      str
    :param puppet_manifest: The absolute path to the puppet manifest to apply on the server
    :type  puppet_manifest: str
    :param server_hostname: The hostname to set on the server
    :type  server_hostname: str


    """
    with settings(host_string=host_string, key_file=key_file):
        # Confirm the server is available
        fabric_confirm_ssh_key(host_string, key_file)

        # Set the hostname
        run('sudo hostname ' + server_hostname)

        # Ensure puppet  modules are installed
        for module in PUPPET_MODULES:
            run(PUPPET_MODULE_INSTALL + module)

        # Add external facts to the server
        puppet_external_facts = {'pulp_repo': repository}
        inject_external_fact(host_string, key_file, puppet_external_facts)

        # Apply the manifest to the server
        fabric_apply_puppet(host_string, key_file, puppet_manifest)


def configure_consumer(host_string, key_file, repository, puppet_manifest, server_ip,
                       server_hostname, consumer_hostname):
    """
    Set up a Pulp consumer using Fabric and a puppet module. Fabric will apply the given consumer
    hostname, ensure root can ssh into the consumer, ensure puppet and all modules in PUPPET_MODULES
    are installed, then apply the puppet manifest. Finally, it will write an /etc/hosts entry for the
    server.

    :param host_string:         The host string for the server. This should be in the format 'user@ip'
    :type  host_string:         str
    :param key_file:            The absolute path to the private key to use when logging into the
    server.
    :type  key_file:            str
    :param repository:          The path to the repository to install from
    :type  repository:          str
    :param puppet_manifest:     The absolute path to the puppet manifest to apply on the server
    :type  puppet_manifest:     str
    :param server_ip:           The IP address of the Pulp server
    :type  server_ip:           str
    :param server_hostname:     The hostname of the Pulp server
    :type  server_hostname:     str
    :param consumer_hostname:   The hostname to set on this consumer
    :type  consumer_hostname:   str
    """
    with settings(host_string=host_string, key_file=key_file):
        fabric_confirm_ssh_key(host_string, key_file)

        # The test suite uses root when SSHing
        run(AUTHORIZE_ROOT_SSH)

        # Set the hostname
        run('sudo hostname ' + consumer_hostname)

        # Ensure puppet modules are installed
        for module in PUPPET_MODULES:
            run(PUPPET_MODULE_INSTALL + module)

        # Add external facts to the consumer so it can find the server
        puppet_external_facts = {
            'external_pulp_server': server_hostname,
            'pulp_repo': repository
        }
        inject_external_fact(host_string, key_file, puppet_external_facts)

        # Apply the puppet module and write the /etc/hosts file
        fabric_apply_puppet(host_string, key_file, puppet_manifest)
        run(HOSTS_TEMPLATE % {'ip': server_ip, 'hostname': server_hostname})


def configure_tester(host_string, server_ip, server_hostname, consumer_ip, consumer_hostname,
                     ssh_key, os_name, os_version):
    """
    Set up the server that runs the integration tests. The basic steps performed are to clone
    the pulp-automation repository, run setup.py, ensure there are entries in /etc/hosts,
    place the ssh key on the tester so it can SSH into the consumer, and write the .yml file
    for the tests.

    :param host_string:         The host string of the tester. This should be in the form user@ip
    :type  host_string:         str
    :param server_hostname:     The hostname of the Pulp server
    :type  server_hostname:     str
    :param consumer_hostname:   The hostname of the Pulp consumer
    :type  consumer_hostname:   str
    :param ssh_key:             The path the SSH key needed to get into the consumer (as root)
    :type  ssh_key:             str
    :param os_name:             The operating system name to be used in the inventory.yml file.
    :type  os_name:             str
    :param os_version:          The version of the operating system.
    :type  os_version:          str
    """
    with settings(host_string=host_string, key_file=ssh_key):
        # Install necessary dependencies.
        for dependency in PULP_AUTO_DEPS:
            run(YUM_INSTALL_TEMPLATE % dependency)

        # Install the test suite
        run(INSTALL_TEST_SUITE)

        # Write to /etc/hosts
        run(HOSTS_TEMPLATE % {'ip': server_ip, 'hostname': server_hostname})
        run(HOSTS_TEMPLATE % {'ip': consumer_ip, 'hostname': consumer_hostname})

        # Dump the ssh private key on the server
        key_path = '/home/' + host_string.split('@')[0] + '/.ssh/id_rsa'
        key_path = key_path.encode('ascii')
        put(ssh_key, key_path)
        run('chmod 600 ' + key_path)

        # Write the YAML configuration file
        get('~/pulp-automation/tests/inventory.yml', 'template_inventory.yml')
        with open('template_inventory.yml', 'r') as template_config:
            config_yaml = yaml.load(template_config)

            # Write the server configuration
            server = {
                'url': 'https://' + server_hostname + '/',
                'hostname': server_hostname
            }
            server_yaml = dict(config_yaml[ROLES_KEY][SERVER_YAML_KEY].items() + server.items())
            config_yaml[ROLES_KEY][SERVER_YAML_KEY] = server_yaml

            # Write the qpid configuration
            config_yaml[ROLES_KEY]['qpid'] = {'url': server_hostname}

            # Write the consumer configuration
            consumer = {
                'hostname': consumer_hostname,
                'ssh_key': key_path,
                'os': {'name': os_name.encode('ascii'), 'version': os_version.encode('ascii')},
                'pulp': server_yaml
            }
            consumer_yaml = dict(config_yaml[ROLES_KEY][CONSUMER_YAML_KEY][0].items() + consumer.items())
            config_yaml[ROLES_KEY][CONSUMER_YAML_KEY][0] = consumer_yaml

            with open('inventory.yml', 'w') as test_config:
                yaml.dump(config_yaml, test_config)

        # Place the config file on the server and clean up
        put('inventory.yml', '~/pulp-automation/inventory.yml')
        os.remove('inventory.yml')
        os.remove('template_inventory.yml')


def tear_down(servers):
    # TODO: Grab all relevant logs
    for server in servers:
        server.delete()


def run_tests(args):
    """
    Run the latest integration tests. This takes some command line arguments,
    sets up the instances in OS1, runs the tests, and retrieves the results.

    :param args:
    :return:
    """
    # Process CLI args
    # Try to set a unique hostname for the instances if one isn't provided
    pulp_server_hostname = args.server_hostname or 'pulp-server'
    pulp_consumer_hostname = args.consumer_hostname or 'pulp-consumer'
    pulp_tester_hostname = args.tester_hostname or 'pulp-tester'

    # Validate that all the expected files actually exist
    if not os.path.isfile(args.key_file):
        raise ValueError(args.key_file + ' is not a file')
    if not os.path.isfile(args.consumer_puppet):
        raise ValueError(args.consumer_puppet + ' is not a file')
    if not os.path.isfile(args.server_puppet):
        raise ValueError(args.server_puppet + ' is not a file')

    # Authenticate with OS1
    os1_details = {
        'username': args.os1_username,
        'password': args.os1_password,
        'tenant_id': args.os1_tenant_id,
        'tenant_name': args.os1_tenant_name,
        'auth_url': args.os1_auth_url,
    }
    glance_instance, keystone_instance, nova_instance = os1_authenticate(**os1_details)

    # Gather the security group, flavor, and image to build
    security_group = args.security_group or DEFAULT_SEC_GROUP
    instance_flavor = args.flavor or DEFAULT_FLAVOR
    images = nova_instance.images.list()
    pulp_server_images = []
    for image in images:
        meta = image.metadata
        if META_DISTRIBUTION_KEYWORD in meta and meta[META_DISTRIBUTION_KEYWORD] == args.distribution:
            pulp_server_images.append(image)
    if not pulp_server_images:
        raise ValueError('Distribution [%s] does not exist' % args.distribution)
    os_name = pulp_server_images[0].metadata['os_name']
    os_version = pulp_server_images[0].metadata['os_version']

    # Build each instance
    pulp_server = create_instance(nova_instance, pulp_server_images[0], pulp_server_hostname,
                                  security_group, instance_flavor, args.os1_key)
    pulp_consumer = create_instance(nova_instance, pulp_server_images[0].id, pulp_consumer_hostname,
                                    security_group, instance_flavor, args.os1_key)
    pulp_tester = create_instance(nova_instance, pulp_server_images[0].id, pulp_tester_hostname,
                                  security_group, instance_flavor, args.os1_key)

    # Get hostname information for Fabric
    pulp_server_ip = get_instance_ip(pulp_server)
    user_login = pulp_server_images[0].metadata['user']
    server_host_string = user_login + '@' + pulp_server_ip
    pulp_consumer_ip = get_instance_ip(pulp_consumer)
    consumer_host_string = user_login + '@' + pulp_consumer_ip
    tester_host_string = user_login + '@' + get_instance_ip(pulp_tester)

    # Apply the necessary configuration to each instance
    configure_server(server_host_string, args.key_file, args.repository, args.server_puppet, pulp_server_hostname)
    configure_consumer(consumer_host_string, args.key_file, args.repository, args.consumer_puppet, pulp_server_ip,
                       pulp_server_hostname, pulp_consumer_hostname)
    configure_tester(tester_host_string, pulp_server_ip, pulp_server_hostname, pulp_consumer_ip,
                     pulp_consumer_hostname, args.key_file, os_name, os_version)

    result = None
    if not args.setup_only:
        try:
            with settings(host_string=tester_host_string):
                result = run('cd pulp-automation && nosetests -vs --with-xunit', warn_only=True)
                get('pulp-automation/nosetests.xml')
        finally:
            tear_down([pulp_server, pulp_tester, pulp_consumer])

    if result:
        sys.exit(result.return_code)
    else:
        sys.exit(1)

description = 'Deploy a Pulp test environment'
consumer_hostname_help = 'The hostname to give the consumer; default is pulp-consumer'
server_hostname_help = 'The hostname to give the server; default is pulp-server'
tester_hostname_help = 'The hostname to give the tester; default is pulp-tester'
os1_username_help = 'username on OS1; this is not necessary if using OS_USERNAME environment variable'
os1_password_help = 'password on OS1; this is not necessary if using OS_PASSWORD environment variable'
os1_tenant_id_help = 'tenant ID on OS1; this is not necessary if using OS_TENANT_ID environment variable'
os1_tenant_name_help = 'tenant name on OS1; this is not necessary if using OS_TENANT_NAME environment variable'
os1_auth_url_help = 'authentication URL on OS1; this is not necessary if using OS_AUTH_URL environment variable'

parser = argparse.ArgumentParser(description=description)
parser.add_argument('--distribution', help="OS to test on; fc20, el6, etc.", required=True)
parser.add_argument('--key-file', help='the path to the private key of the OS1 key pair', required=True)
parser.add_argument('--os1-key', help='the name of the key pair in OS1 to use', required=True)
parser.add_argument('--consumer-puppet', help='path to the consumer puppet module', required=True)
parser.add_argument('--server-puppet', help='path to the server puppet module', required=True)
parser.add_argument('--security-group', default='pulp', help='security group name to apply in OS1')
parser.add_argument('--flavor', default='m1.medium', help='instance flavor to use')
parser.add_argument('--repository', default='http://satellite6.lab.eng.rdu2.redhat.com/pulp/testing/2.4/latest',
                    help='the repository install Pulp from')
parser.add_argument('--os1-username', help=os1_username_help)
parser.add_argument('--os1-password', help=os1_password_help)
parser.add_argument('--os1-tenant-id', help=os1_tenant_id_help)
parser.add_argument('--os1-tenant-name', help=os1_tenant_name_help)
parser.add_argument('--os1-auth-url', help=os1_auth_url_help)
parser.add_argument('--consumer-hostname', default='pulp-consumer', help=consumer_hostname_help)
parser.add_argument('--server-hostname', default='pulp-server', help=server_hostname_help)
parser.add_argument('--tester-hostname', default='pulp-tester', help=tester_hostname_help)
parser.add_argument('--setup-only', action='store_true', help='setup, but do not run any tests')
arguments = parser.parse_args()

run_tests(arguments)
