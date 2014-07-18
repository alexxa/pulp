#!/usr/bin/python

import json
import os
import StringIO
import tempfile
import time
import yaml

from fabric import network as fabric_network
from fabric.api import env, get, put, run, settings
from fabric.context_managers import hide


# Fabric configuration
env.connection_attempts = 4
env.timeout = 30
env.disable_known_hosts = True
env.abort_on_prompts = True

SERVER_CA_CERT_LOCATION = '/etc/pki/pulp/ca.crt'

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

# Puppet fact names
PULP_SERVER_CA_FACT = 'pulp_server_ca_cert'
PULP_REPO_FACT = 'pulp_repo'

# Configuration commands
AUTHORIZE_ROOT_SSH = 'sudo cp ~/.ssh/authorized_keys /root/.ssh/authorized_keys'
TEMPORARY_MANIFEST_LOCATION = '/tmp/manifest.pp'
PUPPET_MODULE_INSTALL = 'sudo puppet module install --force %s'
YUM_INSTALL_TEMPLATE = 'sudo yum -y install %s'
YUM_UPDATE_COMMAND = 'sudo  yum -y update'

# The version of gevent provided by Fedora/RHEL is too old, so force it to update here.
# It seems like setup.py needs to be run twice for now.
INSTALL_TEST_SUITE = 'git clone https://github.com/RedHatQE/pulp-automation.git \
&& sudo pip install -U greenlet gevent requests && cd pulp-automation && sudo python ./setup.py install \
&& sudo python ./setup.py install'

HOSTS_TEMPLATE = "echo '%(ip)s    %(hostname)s %(hostname)s.novalocal' | sudo tee -a /etc/hosts"


def apply_puppet(host_string, key_file, local_module, remote_location=TEMPORARY_MANIFEST_LOCATION):
    """
    Apply a puppet manifest to the given host. It is your responsibility to install
    any puppet module dependencies, and to ensure puppet is installed.

    :param host_string:     The host to connect to: in the form 'user@host'
    :type  host_string:     str
    :param key_file:        The absolute path to the private key to use when connecting as 'user'
    :type  key_file:        str
    :param local_module:    The absolute path to the puppet module to put on the remote host
    :param remote_location: the location to put this puppet module on the remote host

    :raise SystemExit: if the applying the puppet module fails
    """
    with settings(host_string=host_string, key_file=key_file, ok_ret_codes=[0, 2]):
        put(local_module, remote_location)
        run('sudo puppet apply --verbose --detailed-exitcodes ' + remote_location)
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

    :raises SystemExit: if it was unable to ssh in after 300 seconds
    """
    # It can take some time for the init scripts to insert the public key into an instance
    # Abort on prompt is set, so catch the SystemExit exception and sleep for a while.
    print 'Waiting for ' + host_string + ' to become accessible via ssh...'
    with settings(hide('everything'), host_string=host_string, key_file=key_file, quiet=True):
        for x in xrange(0, 30):
            try:
                run('whoami')
                break
            except SystemExit:
                time.sleep(10)
        else:
            run('whoami')


def add_external_fact(host_string, key_file, facts):
    """
    Make Puppet facts available to a remote host. Note that this will simply dump
    the file in /etc/facter/facts.d/facts.json which might overwrite other facts.

    :param host_string: remote host to add facts to; the expected format is 'user@ip'
    :param key_file: the absolute or relative path to the ssh key to use
    :param facts: a dictionary of facts; each key is a Puppet fact. The key
    names must follow the Puppet fact name rules.

    :raise SystemExit: if adding the external fact fails
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


def configure_pulp_server(host_string, key_file, repository, puppet_manifest, server_hostname, server_ca=None):
    """
    Set up a Pulp server using Fabric and a puppet module. Fabric will apply the given
    host name, ensure puppet and any modules declared in PUPPET_MODULES are installed,
    and will then apply the puppet manifest.

    :param host_string:     The host string for the server. This should be in the format 'user@ip'
    :type  host_string:     str
    :param key_file:        The path to the private key to use when logging into the server.
    :type  key_file:        str
    :param repository:      The path to the repository to install from. This will be placed on
    the server as a puppet external fact with the name specified in the the constant PULP_REPO_FACT
    :type  repository:      str
    :param puppet_manifest: The absolute path to the puppet manifest to apply on the server
    :type  puppet_manifest: str
    :param server_hostname: The hostname to set on the server
    :type  server_hostname: str
    :param server_ca:       The server's parent CA cert. This is only necessary if the server
    you are configuring is a node. This CA cert will be placed on the server as a puppet
    external fact with the name in the constant PULP_SERVER_CA_FACT
    :type  server_ca:       str

    :return: A string containing the Pulp server's CA cert. See Pulp installation docs for information
    on how to install this
    :rtype:  str

    :raise SystemExit: if the server could not be successfully configured. This could be
    for any number of reasons. Currently fabric is set to be quite verbose, so see its output
    """
    with settings(host_string=host_string, key_file=key_file):
        # Confirm the server is available
        fabric_confirm_ssh_key(host_string, key_file)

        # Set the hostname
        run('sudo hostname ' + server_hostname)

        # Ensure puppet  modules are installed
        for module in PUPPET_MODULES:
            run(PUPPET_MODULE_INSTALL % module)

        # Add external facts to the server
        puppet_external_facts = {
            PULP_REPO_FACT: repository,
            PULP_SERVER_CA_FACT: server_ca
        }
        add_external_fact(host_string, key_file, puppet_external_facts)

        # Apply the manifest to the server
        apply_puppet(host_string, key_file, puppet_manifest)

        # Retrieve the server's CA cert for use with clients
        temporary_file = StringIO.StringIO()
        run('sudo cp ' + SERVER_CA_CERT_LOCATION + ' ~/ca.crt && sudo chmod 0777 ~/ca.crt')
        get('~/ca.crt', temporary_file)
        server_ca_cert = temporary_file.getvalue()
        temporary_file.close()

        fabric_network.disconnect_all()
        return server_ca_cert


def configure_consumer(host_string, key_file, repository, puppet_manifest, server_ip, server_ca_cert,
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

    :raise SystemExit: if the consumer could not be successfully configured. This could be
    for any number of reasons. Currently fabric is set to be quite verbose, so see its output
    """
    with settings(host_string=host_string, key_file=key_file):
        fabric_confirm_ssh_key(host_string, key_file)

        # The test suite uses root when SSHing
        run(AUTHORIZE_ROOT_SSH)

        # Set the hostname
        run('sudo hostname ' + consumer_hostname)

        # Ensure puppet modules are installed
        for module in PUPPET_MODULES:
            run(PUPPET_MODULE_INSTALL % module)

        # Add external facts to the consumer so it can find the server
        puppet_external_facts = {
            'external_pulp_server': server_hostname,
            'pulp_repo': repository,
            'pulp_server_ca_cert': server_ca_cert,
        }
        add_external_fact(host_string, key_file, puppet_external_facts)

        # Apply the puppet module and write the /etc/hosts file
        apply_puppet(host_string, key_file, puppet_manifest)
        run(HOSTS_TEMPLATE % {'ip': server_ip, 'hostname': server_hostname})
        fabric_network.disconnect_all()


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

    :raise SystemExit: if the tester could not be successfully configured. This could be
    for any number of reasons. Currently fabric is set to be quite verbose, so see its output.
    """
    with settings(host_string=host_string, key_file=ssh_key):
        # Install necessary dependencies.
        with hide('stdout'):
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
        fabric_network.disconnect_all()
