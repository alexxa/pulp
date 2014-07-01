#!/usr/bin/python

import argparse
import os
import time

from fabric import network as fabric_network
from fabric.api import env, run, settings
from fabric.context_managers import hide

from playpen.deploy.openstack import utils


OPENSTACK_ACTIVE_KEYWORD = 'ACTIVE'
DEFAULT_FLAVOR = 'm1.medium'
DEFAULT_SEC_GROUP = 'default'
DEFAULT_INSTANCE_PREFIX = 'pre-pulper-'
META_USER_KEYWORD = 'user'
META_IMAGE_TYPE_KEYWORD = 'pulp_image_type'
PULP_SERVER = 'pre-pulp-server'

description = 'Add a new pre-pulped image snapshot to OS1 based on the given image'

parser = argparse.ArgumentParser(description=description)
parser.add_argument('--image', dest='image_location', help='the full path to the image', required=True)
parser.add_argument('--cloud-config', dest='cloud_config', help='the full path to a cloud-config file')
parser.add_argument('--puppet-module', dest='puppet_module', help='the puppet module to apply on boot', required=True)
parser.add_argument('--os1-key', dest='os1_key', help='the name of the OS1 keypair to use', required=True)
parser.add_argument('--key-file', dest='key_file', help='the path to the private key of the OS1 keypair', required=True)
parser.add_argument('--sec-group', dest='sec_group', help='the security group to use. Defaults to \'default\'')
parser.add_argument('--flavor', help='the instance flavor to use. Defaults to m1.medium')
parser.add_argument('--user', help='the username to ssh into for the image. Might be \'cloud-user\' or \'fedora\'.')
args = parser.parse_args()


# Fabric configuration
env.connection_attempts = 4
env.timeout = 30
env.disable_known_hosts = True
env.abort_on_prompts = True


def fabric_configure(host_string, key_file, puppet_module):
    utils.fabric_confirm_ssh_key(host_string, key_file)

    utils.fabric_yum_update(host_string, key_file)
    utils.fabric_yum_install(host_string, key_file, 'puppet')

    with settings(hide('everything'), host_string=host_string, key_file=key_file):
        try:
            # Install module dependencies
            run('sudo puppet module install puppetlabs/stdlib', quiet=True)
            utils.fabric_apply_puppet(host_string, key_file, puppet_module, '/tmp/puppet.pp')

            # Mongo can take a few tries to start due to timeout issues
            mongo = run('sudo service mongod status', warn_only=True, quiet=True)
            for x in range(0, 5):
                if mongo.return_code != 0:
                    print 'Attempting to restart mongod because it was too slow...'
                    mongo = run('sudo service mongod restart', warn_only=True, quiet=True)
                    time.sleep(120)
                else:
                    break

            mongod = run('sudo service mongod status', warn_only=True, quiet=True)
            qpidd = run('sudo service qpidd status', warn_only=True, quiet=True)
            if qpidd.return_code != 0:
                msg = 'qpidd failed to start. The host string is %s. Remember to clean up!'
                raise SystemExit(msg)
            if mongod.return_code != 0:
                msg = 'mongod failed to start. The host string is %s. Remember to clean up!'
                raise SystemExit(msg)
        finally:
            fabric_network.disconnect_all()


def run_add_image():
    glance, keystone, nova = utils.os1_authenticate()

    print 'Creating image...'
    vanilla_image = utils.create_image(glance, args.image_location)

    print 'Creating instance...'
    flavors = nova.flavors.list()

    # If a flavor was given, grab it. Otherwise use medium.
    flavor = filter(lambda f: f.name == (args.flavor or DEFAULT_FLAVOR), flavors)
    security_group = args.sec_group or DEFAULT_SEC_GROUP
    instance_name = DEFAULT_INSTANCE_PREFIX + os.path.basename(args.image_location)

    instance = utils.create_instance(nova, vanilla_image.id, instance_name, security_group,
                                          flavor[0], args.os1_key, args.cloud_config)

    print 'Configuring instance...'
    host_string = args.user + '@' + instance.networks['os1-internal-1319'][1].encode('ascii', 'ignore')
    fabric_configure(host_string, args.key_file, args.puppet_module)

    utils.reboot_instance(instance, host_string, args.key_file)

    print 'Snapshotting...'
    metadata = {
        META_USER_KEYWORD: args.user,
        META_IMAGE_TYPE_KEYWORD: PULP_SERVER,
    }
    snapshot = utils.take_snapshot(nova, instance, 'pre-pulp-' + os.path.basename(args.image_location), metadata)
    print 'Done! Snapshot id: ' + snapshot.id

#    print 'Cleaning up...'
#    nova.images.delete(vanilla_image)
#    nova.servers.delete(instance)


def os1_run_update():
    glance, keystone, nova = utils.os1_authenticate()

    images = nova.images.list()
    pulp_images = []
    for image in images:
        if 'pulp_image_type' in image.metadata:
            pulp_images.append(image)

    for image in pulp_images:
        # If a flavor was given, grab it. Otherwise use medium.
        flavors = nova.flavors.list()
        flavor = filter(lambda f: f.name == (args.flavor or DEFAULT_FLAVOR), flavors)
        security_group = args.sec_group or DEFAULT_SEC_GROUP

        instance = utils.create_instance(nova, image.id, image.name + '-update', security_group,
                                              flavor, args.key_file)

        # Perform a yum update TODO Error check
        user = image.metadata['user_login']
        host_string = user + '@' + utils.get_instance_ip(instance)
        utils.fabric_confirm_ssh_key(host_string, args.key_file)
        utils.fabric_yum_update(host_string, args.key_file)

        # TODO Give the instance a reboot, then snapshot it and tear it down
        metadata = {
            META_USER_KEYWORD: args.user,
            META_IMAGE_TYPE_KEYWORD: PULP_SERVER,
        }
        utils.reboot_instance(instance, host_string, args.key_file)
        utils.take_snapshot(nova, instance, image.name, metadata)


run_add_image()
