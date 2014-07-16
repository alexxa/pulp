#!/usr/bin/python

import argparse
import os
import sys
import time

from fabric.api import get, run, settings
import signal

import os1_utils
import setup_utils


# The distribution to use for the automated tests.
TESTER_DISTRIBUTION = 'fc20'

# Setup the CLI
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
parser.add_argument('--key-file', help='absolute path to the private key of the OS1 key pair', required=True)
parser.add_argument('--os1-key', help='the name of the key pair in OS1 to use', required=True)
parser.add_argument('--consumer-puppet', help='absolute path to the consumer puppet module', required=True)
parser.add_argument('--server-puppet', help='absolute path to the server puppet module', required=True)
parser.add_argument('--security-group', default=os1_utils.DEFAULT_SEC_GROUP, help='security group name to apply in OS1')
parser.add_argument('--flavor', default=os1_utils.DEFAULT_FLAVOR, help='instance flavor to use')
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
parser.add_argument('--no-teardown', action='store_true', help='setup and run the tests, but leave the VMs')
args = parser.parse_args()


def sigterm_handler(signal_number, stack_frame):
    print 'Received SIGTERM; Exiting...'
    sys.exit(1)

signal.signal(signal.SIGTERM, sigterm_handler)


# Validate that all the expected files actually exist
if not os.path.isfile(args.key_file):
    raise ValueError(args.key_file + ' is not a file')
if not os.path.isfile(args.consumer_puppet):
    raise ValueError(args.consumer_puppet + ' is not a file')
if not os.path.isfile(args.server_puppet):
    raise ValueError(args.server_puppet + ' is not a file')

# Authenticate with OS1
os1_clients = os1_utils.authenticate(args.os1_username, args.os1_password, args.os1_tenant_id,
                                     args.os1_tenant_name, args.os1_auth_url)
glance_instance, keystone_instance, nova_instance = os1_clients

# Find the image to build
pulp_image = None
for image in os1_utils.get_pulp_images(nova_instance):
    if image.metadata[os1_utils.META_DISTRIBUTION_KEYWORD] == args.distribution:
        pulp_image = image
        break
if not pulp_image:
    raise ValueError('Distribution [%s] does not exist' % args.distribution)

# Get the image to use for the test suite
test_suite_image = None
for image in os1_utils.get_pulp_images(nova_instance):
    if image.metadata[os1_utils.META_DISTRIBUTION_KEYWORD] == TESTER_DISTRIBUTION:
        test_suite_image = image
        break
if not test_suite_image:
    raise ValueError('Failed to find the image for the default test distribution')

# Gather the OS name and version
os_name = pulp_image.metadata['os_name']
os_version = pulp_image.metadata['os_version']

# Keep track of the instances we make so we can tear them down if something goes wrong
instances = []
try:
    # Build each instance
    build_time = str(time.time())
    metadata = {
        'pulp_instance': 'True',
        'build_time': build_time,
    }
    pulp_server = os1_utils.create_instance(nova_instance, pulp_image, args.server_hostname,
                                            args.security_group, args.flavor, args.os1_key, metadata)
    instances.append(pulp_server)
    pulp_consumer = os1_utils.create_instance(nova_instance, pulp_image.id, args.consumer_hostname,
                                              args.security_group, args.flavor, args.os1_key, metadata)
    instances.append(pulp_consumer)
    pulp_tester = os1_utils.create_instance(nova_instance, test_suite_image.id, args.tester_hostname,
                                            args.security_group, args.flavor, args.os1_key, metadata)
    instances.append(pulp_tester)

    # Get hostname information for Fabric
    pulp_server_ip = os1_utils.get_instance_ip(pulp_server)
    server_host_string = pulp_image.metadata['user'] + '@' + pulp_server_ip
    pulp_consumer_ip = os1_utils.get_instance_ip(pulp_consumer)
    consumer_host_string = pulp_image.metadata['user'] + '@' + pulp_consumer_ip
    tester_host_string = test_suite_image.metadata['user'] + '@' + os1_utils.get_instance_ip(pulp_tester)

    # Apply the necessary configuration to each instance
    setup_utils.configure_server(server_host_string, args.key_file, args.repository,
                                 args.server_puppet, args.server_hostname)
    setup_utils.configure_consumer(consumer_host_string, args.key_file, args.repository, args.consumer_puppet,
                                   pulp_server_ip, args.server_hostname, args.consumer_hostname)
    setup_utils.configure_tester(tester_host_string, pulp_server_ip, args.server_hostname, pulp_consumer_ip,
                                 args.consumer_hostname, args.key_file, os_name, os_version)

    # Run the tests and get the results
    result = None
    if not args.setup_only:
        with settings(host_string=tester_host_string, key_file=args.key_file):
            result = run('cd pulp-automation && nosetests -vs --with-xunit --logging-filter=-qpid,-pulp_auto',
                         warn_only=True)
            get('pulp-automation/nosetests.xml')
    if result:
        sys.exit(result.return_code)
    else:
        sys.exit(1)
except RuntimeError as e:
    sys.stderr.write(e.message + '\n')
    sys.exit(1)
finally:
    # Make sure to delete all the instances
    if not args.setup_only and not args.no_teardown:
        # Authentication usually times out by the time this block is reached
        os1_clients = os1_utils.authenticate(args.os1_username, args.os1_password, args.os1_tenant_id,
                                             args.os1_tenant_name, args.os1_auth_url)
        glance_instance, keystone_instance, nova_instance = os1_clients
        for instance in instances:
            nova_instance.servers.delete(instance)
