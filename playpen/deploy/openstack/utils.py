import os
import time

from glanceclient import client as glance_client
from fabric import network as fabric_network
from fabric.api import env, run, put, settings
from keystoneclient.v2_0 import client as keystone_client
from novaclient.v1_1 import client as nova_client
from novaclient.v1_1.servers import REBOOT_SOFT


# Various constants
OPENSTACK_ACTIVE_KEYWORD = 'ACTIVE'
DEFAULT_FLAVOR = 'm1.medium'
DEFAULT_SEC_GROUP = 'default'
DEFAULT_INSTANCE_PREFIX = 'pre-pulp-'
META_USER_KEYWORD = 'user'
META_IMAGE_TYPE_KEYWORD = 'pulp_image_type'
PULP_SERVER = 'pre-pulp-server'

# Fabric configuration
env.connection_attempts = 4
env.timeout = 30
env.disable_known_hosts = True
env.abort_on_prompts = True


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

    server = nova.servers.create(instance_name, image_id, flavor,
                                 security_groups=security_groups, key_name=key_name, userdata=user_data)
    if user_data:
        user_data.close()

    # Hang out until Openstack says the VM is up
    while server.status != OPENSTACK_ACTIVE_KEYWORD:
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
        image = glance.images.create(**image_attributes)
        image.update(data=image_data)

    return image


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


def fabric_yum_update(host_string, key_file):
    """
    This will run 'yum -y update' on the given host

    :param host_string: The host to connect to: in the form 'user@host'
    :type  host_string: str
    :param key_file:    The absolute path to the private key to use when connecting as 'user'
    :type  key_file:    str
    """
    try:
        with settings(host_string=host_string, key_file=key_file, quiet=True):
            run('sudo yum -y update')
    finally:
        fabric_network.disconnect_all()


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
        with settings(host_string=host_string, key_file=key_file, quiet=True):
            run('sudo yum -y install ' + package_name)
    finally:
        fabric_network.disconnect_all()


def fabric_apply_puppet(host_string, key_file, local_module, remote_location):
    """
    Apply a puppet module to the given host. It is your responsibility to install
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


def reboot_instance(instance, host_string, key_file):
    instance.reboot(reboot_type=REBOOT_SOFT)
    fabric_confirm_ssh_key(host_string, key_file)


def fabric_confirm_ssh_key(host_string, key_file):
    """
    This is a utility to make sure fabric can ssh into the host with the given key. This is useful
    when a remote host is being set up by cloud-init, which can bring the ssh server up before
    installing the public key.

    :param host_string:
    :param key_file:
    :return:
    """
    # It can take some time for the init scripts to insert the public key into an instance
    # Abort on prompt is set, so catch the SystemExit exception and sleep for a while.
    with settings(host_string=host_string, key_file=key_file, quiet=True):
        for x in xrange(0, 10):
            try:
                run('whoami')
                break
            except SystemExit:
                time.sleep(30)


def get_instance_ip(instance):
    public_ip = instance.networks['os1-internal-1319'][1]
    return public_ip.encode('ascii', 'ignore')
