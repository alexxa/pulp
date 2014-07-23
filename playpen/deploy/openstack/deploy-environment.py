#!/usr/bin/python

import argparse
import time

from fabric.api import get, run, settings
import sys
import yaml

import os1_utils
import setup_utils


# This maps roles to setup functions
CONFIGURATION_FUNCTIONS = {
    'server': setup_utils.configure_pulp_server,
    'consumer': setup_utils.configure_consumer,
    'tester': setup_utils.configure_tester
}

DISTRIBUTION = 'distribution'
INSTANCE_NAME = 'instance_name'
HOSTNAME = 'hostname'
SECURITY_GROUP = 'security_group'
FLAVOR = 'flavor'
OS1_KEY = 'os1_key'
PRIVATE_KEY = 'private_key'
ROLE = 'role'
HOST_STRING = 'host_string'
SYSTEM_USER = 'user'
CLOUD_CONFIG = 'cloud_config'
NOVA_SERVER = 'server'
CHILDREN = 'children'

# This is the bare minimum an instance configuration can contain
INSTANCE_CONFIG_KEYWORDS = [DISTRIBUTION, INSTANCE_NAME, HOSTNAME, SECURITY_GROUP, FLAVOR, OS1_KEY,
                            PRIVATE_KEY, ROLE]


def build_instances(instance_structure, metadata=None):
    """
    Build a set of instances on Openstack using the given list of configurations.
    Each configuration is expected to contain the following keywords: DISTRIBUTION,
    INSTANCE_NAME, 'security_group', 'flavor', 'os1_key', and 'cloud_config'

    The configurations will have the 'user' and 'server' keys added, which will contain
    the user to SSH in as, and the novaclient.v1_1.server.Server created.

    :param instance_structure:  The structure dictionary produced by the configuration parser
    :type  instance_structure:  dict
    :param metadata:            The metadata to attach to the instances. Limit to 5 keys, 255 character values
    :type  metadata:            dict
    """
    # Wrap the original structure in a list so we can treat the root instance the same way as the children
    if isinstance(instance_structure, dict):
        instance_structure = [instance_structure]

    for instance in instance_structure:
        # Build the base instance
        image = os1.get_distribution_image(instance[DISTRIBUTION])
        cloud_config = instance.get(CLOUD_CONFIG)
        server = os1.create_instance(image.id, instance[INSTANCE_NAME], instance[SECURITY_GROUP],
                                     instance[FLAVOR], instance[OS1_KEY], metadata, cloud_config)
        instance[SYSTEM_USER] = image.metadata['user'].encode('ascii')
        instance[NOVA_SERVER] = server

        # Build any children
        if CHILDREN in instance:
            build_instances(instance[CHILDREN], metadata)


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
    instance_config['host_string'] = instance_config['user'] + '@' + os1.get_instance_ip(instance_config['server'])
    if args.repo:
        instance_config['repository'] = args.repo
    config_function = CONFIGURATION_FUNCTIONS[instance_config['role']]
    config_result = config_function(**instance_config)

    # Add the instance configuration to the configuration results
    if config_result is None:
        config_result = {}

    return config_result


def configure_instances(instance_structure):
    """
    Configure the root instance and each decedent. Any configuration results are written to
    the given dictionary

    :param instance_structure: The instance structure dictionary from the configuration parser
    :type  instance_structure: dict
    """
    # Wrap the original structure in a list so we can treat the root instance the same way as the children
    if isinstance(instance_structure, dict):
        instance_structure = [instance_structure]

    for instance in instance_structure:
        # Configure the instance
        config_result = configure_instance(instance)

        # Update the configuration dictionary with any changes or additions from the results
        instance = dict(instance.items() + config_result.items())

        # Deal with its children
        if CHILDREN in instance:
            children = instance[CHILDREN]
            for child in children:
                child['parent_config'] = instance
            configure_instances(children)


def _configure_child_instances(child_list):
    """
    A helper method to configure a list of child instances

    :param child_list: A list of configuration dictionaries,
    :return:
    """
    for instance_config in child_list:
        # Configure the instance
        config_result = configure_instance(instance_config)

        instance_config = dict(instance_config.items() + config_result.items())

        # Deal with any of its children
        if CHILDREN in instance_config:
            children = instance_config[CHILDREN]
            for child in children:
                child['parent_config'] = instance_config
            _configure_child_instances(children)


def parse_config_file(config_path):
    """
    Parse the given configuration file into a python dictionary

    :param config_path: the absolute path to the configuration file
    :type  config_path: str

    :return: a tuple in the format: (list of pulp configuration dicts, pulp tester dict, os1 credentials dict).
    The list of configuration dicts may contain further lists, and is essentially a tree.
    :rtype:  tuple

    """
    with open(config_path, 'r') as config_file:
        config = yaml.load(config_file)

    instance_structure = config['structure']
    pulp_tester = config['pulp_tester']
    os1_credentials = config['os1_credentials']

    # Validate some sane defaults for an instance
    _validate_base_instance_config(instance_structure)

    if CHILDREN in instance_structure:
        _validate_children(instance_structure[CHILDREN])

    return instance_structure, pulp_tester, os1_credentials


def _validate_base_instance_config(config):
    missing_keys = []
    for key in INSTANCE_CONFIG_KEYWORDS:
        if key not in config:
            missing_keys.append(key)

    if missing_keys:
        raise ValueError('Missing [%(key)s] in [%(config)s]' % {'key': repr(missing_keys), 'config': repr(config)})


def _validate_children(children):
    for child in children:
        _validate_base_instance_config(child)
        if 'children' in child:
            _validate_children(child['children'])


def flatten_structure(instance_structure):
    """
    Flatten the structure dictionary to a list of dictionaries, where each dictionary contains
    its an instance's configuration. This removes all parent/child relationships/

    :param instance_structure: the structure to flatten
    :type  instance_structure: dict

    :return: A list of dictionaries representing the instances
    :rtype:  list
    """
    # Make a copy so we don't destroy anything
    working_copy = instance_structure.copy()
    return _flatten_structure(working_copy)


def _flatten_structure(instance_structure):
    """
    Private helper for flatten_structure

    :param instance_structure: the structure to flatten
    :type  instance_structure: dict

    :return: A list of dictionaries representing the instances
    :rtype:  list
    """
    instance_list = []
    if isinstance(instance_structure, list):
        for instance in instance_structure:
            if 'children' in instance:
                # We haven't reached to bottom yet
                instance_list = instance_list + _flatten_structure(instance.pop('children'))

            instance_list.append(instance)
    else:
        # structure wasn't iterable, so it's not a set of children
        if 'children' in instance_structure:
            instance_list = instance_list + _flatten_structure(instance_structure.pop('children'))

        instance_list.append(instance_structure)

    return instance_list


def deploy_instances(instance_structure, metadata):
    """
    Deploy the given list of instances using the os1 manager instance.
    Each Openstack instance will have the given metadata attached to it.

    :param instance_structure:      A structure dictionary that has been validated by the parser
    :type  instance_structure:      dict
    :param metadata:                A dictionary of metadata to attach to the instance
    :type  metadata:                dict
    """
    # Step 1: Build all the instances, attach configuration to them
    # Step 2: Configure each instance, configuring the root node first,
    # and working down to the leaves.

    build_instances(instance_structure, metadata)

    # Grab all the nova instances and wait for them to become active
    flattened_list = flatten_structure(instance_structure)
    servers = [instance['server'] for instance in flattened_list]
    os1.wait_for_active_instances(servers)

    configure_instances(instance_structure)


def deploy_test_machine(instance_config, server_config, consumer_config, metadata=None):
    """
    Deploy the test machine, which does not fall into the pattern for deploying the other instances.
    Currently, the automated tests only use one server and one consumer.

    :param instance_config: The configuration information for the test machine
    :type  instance_config: dict
    :param server_config:   The configuration information from the server
    :type  server_config:   dict
    :param consumer_config: the configuration information from the consumer
    :type  consumer_config: dict
    :param metadata:        The metadata to attach to the test machine
    :type  metadata:        dict

    :return: The instance
    :rtype:  novaclient.v1_1.servers.Server
    """
    instance_config['server_config'] = server_config
    instance_config['consumer_config'] = consumer_config

    build_instances(instance_config, metadata=metadata)
    instance = instance_config['server']
    os1.wait_for_active_instances([instance])
    configure_instance(instance_config)


def run_deployment():
    try:
        # Deploy the non-test machine instances
        instance_metadata = {
            'pulp_instance': 'True',
            'build_time': str(time.time()),
        }
        print 'Deploying instances...'
        deploy_instances(structure, instance_metadata)

        # Right now the integration tests expect a single server and a single consumer
        test_results = None
        if args.integration_tests:
            config_list = flatten_structure(structure)
            test_server_config = filter(lambda config: config['role'] == 'server', config_list)
            test_consumer_config = filter(lambda config: config['role'] == 'consumer', config_list)

            if len(test_server_config) == 1 and len(test_consumer_config) == 1:
                deploy_test_machine(test_machine_config, test_server_config[0], test_consumer_config[0],
                                    instance_metadata)

                # If the setup_only flag isn't specified, run the tests
                if not args.setup_only:
                    with settings(host_string=test_machine_config['host_string'],
                                  key_file=test_machine_config['private_key']):
                        test_results = run('cd pulp-automation && nosetests -vs --with-xunit --nologcapture',
                                           warn_only=True)
                        # Get the results, which places them by default in a directory called *host string*
                        get('pulp-automation/nosetests.xml', test_machine_config['tests_destination'])

            else:
                print 'Skipping test machine; your configuration file does not specify a single server and consumer'

            sys.exit(test_results.return_code)
    except Exception, e:
        # Print exception message and quit
        print 'Error: %s' % e
        sys.exit(1)
    finally:
        if not args.no_teardown:
            # Find all the servers that got built
            server_list = [configuration['server'] for configuration in flatten_structure(structure)]
            server_list.append(test_machine_config['server'])
            for deployed_server in server_list:
                os1.delete_instance(deployed_server)


# Setup the CLI
description = 'Deploy a Pulp environment, and optionally run the integration suite against it'
parser = argparse.ArgumentParser(description=description)
parser.add_argument('--config', help='Path to the configuration file to use to deploy the environment', required=True)
parser.add_argument('--repo', help='Path the the repository; will override repositories set in the configuration')
parser.add_argument('--integration-tests', action='store_true', help='Run the integration tests')
parser.add_argument('--setup-only', action='store_true', help='setup, but do not run any tests')
parser.add_argument('--no-teardown', action='store_true', help='setup and run the tests, but leave the VMs')
args = parser.parse_args()


# Parse the configuration file
structure, test_machine_config, os1_auth = parse_config_file(args.config)
print 'Successfully parsed the configuration file!'
os1 = os1_utils.OS1Manager(**os1_auth)
print 'Successfully authenticated with OS1!'

run_deployment()
