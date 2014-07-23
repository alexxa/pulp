#!/usr/bin/python

import argparse
import time

from fabric.api import get, run, settings
import yaml

import os1_utils
import setup_utils


# The distribution to use for the automated tests.
TESTER_DISTRIBUTION = 'fc20'

# Setup the CLI
description = 'Deploy a Pulp environment, and optionally run the integration suite against it'

parser = argparse.ArgumentParser(description=description)
parser.add_argument('--config', help='Path to the configuration file to use to deploy the environment', required=True)
parser.add_argument('--repo', help='Path the the repository; will override repositories set in the configuration')
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

# This is the bare minimum an instance configuration can contain
INSTANCE_CONFIG_KEYWORDS = ['distribution', 'instance_name', 'hostname', 'security_group', 'flavor', 'os1_key',
                            'private_key', 'role']


def build_instances(os1_manager, structure, metadata=None):
    """
    Build a set of instances on Openstack using the given list of configurations.
    Each configuration is expected to contain the following keywords: 'distribution',
    'instance_name', 'security_group', 'flavor', 'os1_key', and 'cloud_config'

    The configurations will have the 'user' and 'server' keys added, which will contain
    the user to SSH in as, and the novaclient.v1_1.server.Server created.

    :param os1_manager: An instance of os1_utils
    :type  os1_manager: os1_utils.OS1Manager
    :param structure:
    :param metadata:
    :return:
    """
    # Build the base instance
    image = os1_manager.get_distribution_image(structure['distribution'])
    cloud_config = structure.get('cloud_config')
    server = os1_manager.create_instance(image.id, structure['instance_name'], structure['security_group'],
                                         structure['flavor'], structure['os1_key'], metadata, cloud_config)
    structure['user'] = image.metadata['user'].encode('ascii')
    structure['server'] = server

    # Build any children
    if 'children' in structure:
        _build_child_instances(os1_manager, structure['children'], metadata)


def _build_child_instances(os1_manager, child_list, metadata=None):
    for child in child_list:
        # Build the base instance
        image = os1_manager.get_distribution_image(child['distribution'])
        cloud_config = child.get('cloud_config')
        server = os1_manager.create_instance(image.id, child['instance_name'], child['security_group'],
                                             child['flavor'], child['os1_key'], metadata, cloud_config)
        child['user'] = image.metadata['user'].encode('ascii')
        child['server'] = server

        # Build any children
        if 'children' in child:
            _build_child_instances(os1_manager, child['children'], metadata)


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


def configure_instances(config_dictionary):
    """
    Turn the dictionary configuration containing lists of dictionaries in the 'children'
    key into a list of lists of arbitrary depth.

    :param config_dictionary: A dictionary that has been validated by (at least) _validate_base_instance_config
    :type  config_dictionary: dict
    :return: A list where the first item should be the root instance dictionary, and the next should
    be a list of children instance dictionaries. This list might itself contain a list of children
    instances. This can should be thought of as a tree
    """
    # Configure the instance
    config_result = configure_instance(config_dictionary)

    # Update the configuration dictionary with any changes or additions from the results
    config_dictionary = dict(config_dictionary.items() + config_result.items())

    # Deal with its children
    if 'children' in config_dictionary:
        children = config_dictionary['children']
        for child in children:

            child['parent_config'] = config_dictionary
        _configure_child_instances(children)


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
        if 'children' in instance_config:
            children = instance_config['children']
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

    structure = config['structure']
    pulp_tester = config['pulp_tester']
    os1_credentials = config['os1_credentials']

    # Validate some sane defaults for an instance
    _validate_base_instance_config(structure)

    if 'children' in structure:
        _validate_children(structure['children'])

    return structure, pulp_tester, os1_credentials


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


def flatten_structure(structure):
    """
    Flatten the structure dictionary

    :param structure: the structure to flatten
    :type  structure: dict
    :return:
    """
    working_copy = structure.copy()
    return _flatten_structure(working_copy)


def _flatten_structure(structure):
    """
    Flatten the structure dictionary

    :param structure: the structure to flatten
    :type  structure: dict
    :return:
    """
    instance_list = []
    if isinstance(structure, list):
        for instance in structure:
            if 'children' in instance:
                # We haven't reached to bottom yet
                instance_list = instance_list + _flatten_structure(instance.pop('children'))

            instance_list.append(instance)
    else:
        # structure wasn't iterable, so it's not a set of children
        if 'children' in structure:
            instance_list = instance_list + _flatten_structure(structure.pop('children'))

        instance_list.append(structure)

    return instance_list


def deploy_instances(os1_manager, structure, metadata):
    """
    Deploy the given list of instances using the os1 manager instance.
    Each Openstack instance will have the given metadata attached to it.

    :param os1_manager:             The instance of the OS1 manager to use
    :type  os1_manager:             os1_utils.OS1Manager
    :param structure:               A structure dictionary that has been validated by the parser
    :type  structure:               dict
    :param metadata:                A dictionary of metadata to attach to the instance
    :type  metadata:                dict

    :return: a tuple of servers and their final configurations
    :rtype:  list of novaclient.v1_1.servers.Server
    """
    # Step 1: Build all the instances, attach configuration to them
    # Step 2: Configure each instance, configuring the root node first,
    # and working down to the leaves.

    build_instances(os1_manager, structure, metadata)

    # Grab all the nova instances and wait for them to become active
    flattened_list = flatten_structure(structure)
    servers = [instance['server'] for instance in flattened_list]
    os1_manager.wait_for_active_instances(servers)

    configure_instances(structure)
    return servers


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

    :return: The instance
    :rtype:  novaclient.v1_1.servers.Server
    """
    instance_config['server_config'] = server_config
    instance_config['consumer_config'] = consumer_config

    build_instances(os1_manager, instance_config, metadata=metadata)
    instance = instance_config['server']
    os1.wait_for_active_instances([instance])
    configure_instance(instance_config)

    return instance


# Parse the configuration file
print 'Parsing configuration file...'
instance_structure, test_machine_config, os1_auth = parse_config_file(args.config)
os1 = os1_utils.OS1Manager(**os1_auth)
print 'Successfully authenticated with OS1'


try:
    # Deploy the non-test machine instances
    instance_metadata = {
        'pulp_instance': 'True',
        'build_time': str(time.time()),
    }
    print 'Deploying instances...'
    server_list = deploy_instances(os1, instance_structure, instance_metadata)

    # Right now the integration tests expect a single server and a single consumer
    if args.integration_tests:
        config_list = flatten_structure(instance_structure)
        print repr(config_list)
        test_server_config = filter(lambda config: config['role'] == 'server', config_list)
        test_consumer_config = filter(lambda config: config['role'] == 'consumer', config_list)

        if len(test_server_config) == 1 and len(test_consumer_config) == 1:
            test_server = deploy_test_machine(os1, test_machine_config, test_server_config[0],
                                              test_consumer_config[0], instance_metadata)
            server_list.append(test_server)

            # If the setup_only flag isn't specified, run the tests
            if not args.setup_only:
                with settings(host_string=test_machine_config['host_string'], key_file=test_machine_config['private_key']):
                    result = run('cd pulp-automation && nosetests -vs --with-xunit --nologcapture', warn_only=True)
                    # Get the results, which places them by default in a directory called *host string*
                    get('pulp-automation/nosetests.xml', test_machine_config['tests_destination'])

        else:
            print 'Skipping test machine; your configuration file does not specify a single server and consumer'
except:
    # Print exception message
    pass
finally:
    if not args.no_teardown:
        # Find all the servers that got built
        server_list = [configuration['server'] for configuration in flatten_structure(instance_structure)]
        for deployed_server in server_list:
            os1.delete_instance(deployed_server)
