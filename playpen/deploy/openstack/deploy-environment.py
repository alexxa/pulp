#!/usr/bin/python

import argparse
import time

from fabric.api import get, run, settings
import sys
import yaml

import os1_utils
import setup_utils


# The distribution to use for the automated tests.
TESTER_DISTRIBUTION = 'fc20'

# Setup the CLI
description = 'Deploy a Pulp environment, and optionally run the integration suite against it'
consumer_hostname_help = 'The hostname to give the consumer; default is pulp-consumer'
server_hostname_help = 'The hostname to give the server; default is pulp-server'
tester_hostname_help = 'The hostname to give the tester; default is pulp-tester'
os1_username_help = 'username on OS1; this is not necessary if using OS_USERNAME environment variable'
os1_password_help = 'password on OS1; this is not necessary if using OS_PASSWORD environment variable'
os1_tenant_id_help = 'tenant ID on OS1; this is not necessary if using OS_TENANT_ID environment variable'
os1_tenant_name_help = 'tenant name on OS1; this is not necessary if using OS_TENANT_NAME environment variable'
os1_auth_url_help = 'authentication URL on OS1; this is not necessary if using OS_AUTH_URL environment variable'

parser = argparse.ArgumentParser(description=description)
parser.add_argument('--config', help='Path to the configuration file to use to deploy the environment', required=True)
parser.add_argument('--integration-tests', action='store_true', help='Run the integration tests')
parser.add_argument('--setup-only', action='store_true', help='setup, but do not run any tests')
parser.add_argument('--no-teardown', action='store_true', help='setup and run the tests, but leave the VMs')
args = parser.parse_args()

# TODO: Change this to be very generic. Read some configuration files (like auth.json and instances.json)
# and, for each instance, build it with the appropriate handler using the instance configuration settings.
# A nice-to-have would be to do this in a semi-concurrent way that builds unrelated instances in parallel.
# Any single setup should have a root server node and some number of children (either servers or consumers).
# A good approach might be to build this root server first and work down the branches.

# This maps roles to setup functions
CONFIGURATION_FUNCTIONS = {
    'server': setup_utils.configure_pulp_server,
    'consumer': setup_utils.configure_consumer,
    'tester': setup_utils.configure_tester
}


def build_instances(os1_manager, instance_config_list, metadata=None):
    """
    Build a set of instances on Openstack using the given list of configurations.
    Each configuration is expected to contain the following keywords: 'distribution',
    'instance_name', 'security_group', 'flavor', 'os1_key', and 'cloud_config'

    The configurations will have the 'user' and 'host_string' keys added.

    :param os1_manager: An instance of os1_utils
    :type  os1_manager: os1_utils.OS1Manager
    :param instance_config_list:
    :param metadata:
    :return:
    """
    instance_list = []

    try:
        # Create all the build requests
        for instance_config in instance_config_list:
            image = os1_manager.get_distribution_image(instance_config['distribution'])
            instance_name = instance_config['instance_name']
            security_group = instance_config['security_group']
            flavor = instance_config['flavor']
            os1_key = instance_config['os1_key']
            cloud_config = instance_config.get('cloud_config')
            instance_config['user'] = image.metadata['user'].encode('ascii')

            # Create the server and save its ip to the instance config
            server = os1_manager.create_instance(image.id, instance_name, security_group, flavor, os1_key,
                                                 metadata, cloud_config)
            instance_list.append((server, instance_config))

        # Wait until all the instances are built or 10 minutes elapse
        os1_manager.wait_for_active_instances([server for server, conf in instance_list], timeout=10)
    except:
        # Clean up the instances because something exploded in Openstack (probably)
        print 'Error while building instance: %s' % sys.exc_info()[1]
        for instance, conf in instance_list:
            os1_manager.delete_instance(instance)


    # Set the host string
    for server, instance_config in instance_list:
        instance_config['host_string'] = instance_config['user'] + '@' + os1_manager.get_instance_ip(server)

    # Maybe don't need to return instance_config
    return instance_list


def configure_instance(instance_config):
    """
    Configure an instance using the function corresponding to the instance
    configuration's 'role' value as defined in get_config_function.

    :param instance_config: is the instance configuration to use. The
    required keywords in this dictionary vary by role.
    :type  instance_config: dict

    :return: The result, if any, combined with the original instance config
    :rtype:  dict
    """
    # Gather the necessary configuration arguments
    config_function = CONFIGURATION_FUNCTIONS[instance_config['role']]
    config_result = config_function(**instance_config)

    # Add the instance configuration to the configuration results
    if config_result is None:
        config_result = {}
    config_result = dict(config_result.items() + instance_config.items())
    return config_result


def parse_config_file(config_path):
    """
    Parse the given configuration file into a python dictionary

    :param config_path: the absolute path to the configuration file
    :type  config_path: str

    :return: a tuple in the format:
    (list of pulp configuration dicts, pulp tester dict, os1 credentials dict)
    :rtype:  tuple

    """
    with open(config_path, 'r') as config_file:
        config = yaml.load(config_file)
        structure = config['structure']
        pulp_tester = config['pulp_tester']
        os1_credentials = config['os1_credentials']

        # TODO add recursive support
        instances = [structure.pop('instance')]
        for instance in structure.pop('children'):
            instances.append(instance)

    return instances, pulp_tester, os1_credentials


def deploy_instances(os1_manager, instance_config_list, metadata):
    """
    Deploy the given list of instances using the os1 manager instance.
    Each Openstack instance will have the given metadata attached to it.

    :param os1_manager:             The instance of the OS1 manager to use
    :type  os1_manager:             os1_utils.OS1Manager
    :param instance_config_list:    a list of dictionaries that contain configuration dictionaries
    :type  instance_config_list:    list of dict
    :param metadata:                A dictionary of metadata to attach to the instance
    :type  metadata:                dict

    :return: a tuple of servers and their final configurations
    :rtype:  (list of novaclient.v1_1.servers.Server, list of dict)
    """
    # Step 1: Build all the instances, attach configuration to them
    # Step 2: Configure each instance, configuring the root node first,
    # and working down to the leaves.
    servers = []
    final_config_list = []

    instances = build_instances(os1_manager, instance_config_list, metadata)

    for instance, instance_config in instances:
        if 'parent' in instance_config:
            # Find the parent's post-build configuration
            filter_function = lambda conf: conf['instance_name'] == instance_config['parent']
            parent_config = filter(filter_function, final_config_list)
            instance_config['parent_config'] = parent_config[0]

        configured_instance = configure_instance(instance_config)

        final_config_list.append(configured_instance)
        servers.append(instance)

    return servers, final_config_list


def deploy_test_machine(os1_manager, instance_config, server_config, consumer_config, metadata=None):
    """
    Deploy the test machine, which does not fall into the pattern for deploying the other instances.
    Currently, the automated tests only use one server and one consumer.

    :param os1_manager:     The os1 manager to use when deploying the instance
    :type  os1_manager:     os1_util.OS1Manager
    :param instance_config: The configuration information for the test machine
    :type  instance_config: dict
    :param server_config:   The configuration information from the server
    :type  server_config:   dict
    :param consumer_config: the configuration information from the consumer
    :type  consumer_config: dict
    :param metadata:        The metadata to attach to the test machine
    :type  metadata:        dict

    :return: a tuple of the instance and its configuration
    :rtype:  dict
    """
    instance_config['server_config'] = server_config
    instance_config['consumer_config'] = consumer_config

    instance, instance_config = build_instances(os1_manager, [instance_config], metadata=metadata)[0]
    final_config = configure_instance(instance_config)
    return instance, final_config


# Parse the configuration file
print 'Parsing configuration file...'
instances_to_build, test_machine_config, os1_auth = parse_config_file(args.config)
print 'Done!\n'
os1 = os1_utils.OS1Manager(**os1_auth)
print 'Successfully authenticated with OS1'

# Deploy the non-test machine instances
instance_metadata = {
    'pulp_instance': 'True',
    'build_time': str(time.time()),
}
print 'Deploying instances...'
server_list, config_list = deploy_instances(os1, instances_to_build, instance_metadata)

# Right now the integration tests expect a single server and a single consumer
if args.integration_tests:
    test_server_config = filter(lambda config: config['role'] == 'server', config_list)
    test_consumer_config = filter(lambda config: config['role'] == 'consumer', config_list)

    if len(test_server_config) == 1 and len(test_consumer_config) == 1:
        test_server, test_config = deploy_test_machine(os1, test_machine_config, test_server_config[0],
                                                       test_consumer_config[0], instance_metadata)
        server_list.append(test_server)

        # If the setup_only flag isn't specified, run the tests
        if not args.setup_only:
            with settings(host_string=test_config['host_string'], key_file=test_config['private_key']):
                result = run('cd pulp-automation && nosetests -vs --with-xunit --nologcapture', warn_only=True)
                # Get the results, which places them by default in a directory called *host string*
                get('pulp-automation/nosetests.xml', test_config['tests_destination'])

    else:
        print 'Skipping test machine; your configuration file does not specify a single server and consumer'

if not args.no_teardown:
    for deployed_instance in server_list:
        os1.delete_instance(deployed_instance)
