import os
import time

from glanceclient import client as glance_client
from keystoneclient.v2_0 import client as keystone_client
from novaclient.v1_1 import client as nova_client


# Constants
OPENSTACK_ACTIVE_KEYWORD = 'ACTIVE'
OPENSTACK_BUILD_KEYWORD = 'BUILD'
DEFAULT_FLAVOR = 'm1.medium'
DEFAULT_SEC_GROUP = 'pulp'
META_USER_KEYWORD = 'user'
META_DISTRIBUTION_KEYWORD = 'pulp_distribution'
META_OS_NAME_KEYWORD = 'os_name'
META_OS_VERSION_KEYWOR = 'os_version'


class OS1Manager:

    def __init__(self, username=None, password=None, tenant_id=None, tenant_name=None, auth_url=None):
        if not username:
            username = os.environ.get('OS_USERNAME')
        if not password:
            password = os.environ.get('OS_PASSWORD')
        if not tenant_id:
            tenant_id = os.environ.get('OS_TENANT_ID')
        if not tenant_name:
            tenant_name = os.environ.get('OS_TENANT_NAME')
        if not auth_url:
            auth_url = os.environ.get('OS_AUTH_URL')

        self.username = username
        self.password = password
        self.tenant_id = tenant_id
        self.tenant_name = tenant_name
        self.auth_url = auth_url

        self.glance = None
        self.keystone = None
        self.nova = None

    def _authenticate(self):
        """
        Authenticates with keystone, nova, and glance.
        """
        self.keystone = keystone_client.Client(username=self.username, password=self.password, tenant_id=self.tenant_id,
                                               tenant_name=self.tenant_name, auth_url=self.auth_url)
        self.keystone.authenticate()
        self.nova = nova_client.Client(self.username, self.password, self.tenant_name,
                                       tenant_id=self.tenant_id, auth_url=self.auth_url)
        self.nova.authenticate()
        glance_url = self.keystone.service_catalog.get_endpoints()['image'][0]['adminURL']
        self.glance = glance_client.Client('1', endpoint=glance_url, token=self.keystone.auth_token)

    def get_pulp_images(self):
        """
        Return all images containing the META_DISTRIBUTION_KEYWORD

        :return: a list of of novaclient.images.Image
        :rtype:  list
        """
        self._authenticate()
        image_list = self.nova.images.list()
        pulp_images = []
        for image in image_list:
            meta = image.metadata
            if META_DISTRIBUTION_KEYWORD in meta:
                pulp_images.append(image)

        return pulp_images

    def create_instance(self, image_id, instance_name, security_groups, flavor_name, key_name, metadata=None,
                        cloud_init=None):
        """
        Builds an instance using the given nova client. This call will block until Openstack says the
        instance is 'active'. Note: this just means Openstack has successfully started the boot process.
        It has not successfully booted, nor has cloud-init run, so ssh via public key authentication will
        not work when this returns.

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
        :param metadata:        A dictionary to attach to the running instance. Maximum of entries.
        :type  metadata:        dict
        :param cloud_init:      the absolute path to a cloud-config file
        :type  cloud_init:      str

        :return: The instance
        :rtype:  nova.servers.Server
        """
        print 'Building [%s]...' % instance_name

        # Set up instance configuration
        self._authenticate()
        flavor = self.nova.flavors.find(name=flavor_name)
        if not isinstance(security_groups, list):
            security_groups = [security_groups]

        init_file = None
        if cloud_init:
            init_file = open(cloud_init)

        server = self.nova.servers.create(instance_name, image_id, flavor, security_groups=security_groups,
                                          key_name=key_name, userdata=init_file, meta=metadata)
        if init_file:
            init_file.close()

        return server

    def wait_for_active_instances(self, instance_list, timeout=10):
        """
        Wait for the given list of instances to become active. Raise an exception if any fail.
        It is the responsibility of the user to tear down the instances.

        :param instance_list:   List of instances to wait on
        :type  instance_list:   list of novaclient.v1_1.servers.Server
        :param timeout:         maximum time to wait in minutes
        :type  timeout:         int

        :raise: RuntimeError if not all the instances are in the active state by the timeout
        """
        # Wait until all the instances are built or times out
        for x in range(0, timeout * 60, 10):
            # Check to make sure each instance is out of the build state
            for server in instance_list:
                # Get the latest information about the image
                server = self.nova.servers.get(server.id)

                # An instance isn't done building yet
                if server.status == OPENSTACK_BUILD_KEYWORD:
                    time.sleep(10)
                    break
            else:
                # In this case every server was finished building
                for server in instance_list:
                    server = self.nova.servers.get(server.id)
                    if server.status != OPENSTACK_ACTIVE_KEYWORD:
                        raise RuntimeError('Failed to build the following instance: ' + server.name)
                break
        else:
            # In this case we never built all the instances
            raise RuntimeError('Build time exceeded timeout, please inspect the instances and clean up')

    def create_image(self, image_location):
        """
        Upload an image from image_location into glance

        :param image_location:  The path to image. This can be absolute or relative.
        :type  image_location:  str

        :return: A representation of the uploaded image
        :rtype:
        """
        self._authenticate()
        image_name = os.path.basename(image_location)
        image_attributes = {
            'name': 'automated-pulp-' + image_name,
            'container_format': 'bare',
            'disk_format': 'qcow2'
        }

        with open(image_location) as image_data:
            new_image = self.glance.images.create(**image_attributes)
            new_image.update(data=image_data)

        return new_image

    def delete_instance(self, instance):
        """
        Remove the specified instances

        :param instance: An instance on OS1 to delete
        :type  instance: novaclient.v1_1.servers.Server
        """
        self._authenticate()
        self.nova.servers.delete(instance)

    def take_snapshot(self, server, snapshot_name, metadata=None):
        """
        Take a snapshot of given server. This call will block until Openstack
        reports that the snapshot is active.

        :param server:          The active instance to take a snapshot of
        :type  server:          novaclient.v1_1.servers.Server
        :param snapshot_name:   The human-readable name to assign to the snapshot.
        :type  snapshot_name:   str
        :param metadata:        A dictionary to use as metadata for the image snapshot.
        :type  metadata:        dict

        :return: An Image instance representing the snapshot taken
        :rtype:  novaclient.v1_1.images.Image
        """
        self._authenticate()
        snapshot_id = server.create_image(snapshot_name)
        snapshot = self.nova.images.get(snapshot_id)

        # Wait for the snapshot to complete
        while snapshot.status != OPENSTACK_ACTIVE_KEYWORD:
            time.sleep(10)
            snapshot = self.nova.images.get(snapshot_id)

        self.nova.images.ImageManager.set_meta(snapshot_id, metadata)

        return snapshot

    def get_instance_ip(self, instance):
        """
        Get an OS1 Internal public ip address

        :param instance: a server instance with a public ip address
        :type  instance: nova.servers.Server

        :return: the public ip address
        :rtype:  str
        """
        # Authenticate and ensure we have the latest information about the instance
        self._authenticate()
        instance = self.nova.servers.get(instance.id)
        public_ip = instance.networks['os1-internal-1319'][1]
        return public_ip.encode('ascii')

    def reboot_instance(self, server):
        """
        Reboot an instance, and wait for it to return to the active state.
        If, after 2 minutes, it is not active, an exception is raised.

        :param server:  The active instance to reboot
        :type  server:  novaclient.v1_1.servers.Server

        :raise: RuntimeError if the reboot failed
        """
        self._authenticate()
        server.reboot()
        for x in range(0, 120, 10):
            time.sleep(10)
            server = self.nova.servers.get(server.id)
            if server.status == OPENSTACK_ACTIVE_KEYWORD:
                break
        else:
            raise RuntimeError('Reboot is hanging. Please fix it manually.')

    def get_distribution_image(self, distribution):
        """
        Retrieve an image of the given distribution. This method looks for images tagged
        with the META_DISTRIBUTION_KEYWORD and matched the given string to that value.
        If more than one image exists for the given distribution, the first in the list
        is returned.

        :param distribution: The image distribution to return. 'el6', 'fc20', etc.
        :type  distribution: str

        :return: An image that can be used to create an instance
        :rtype:  novaclient.v1_1.images.Image

        :raise: ValueError if the distribution doesn't exist
        """
        # Find the image to build
        pulp_image = None
        for image in self.get_pulp_images():
            if image.metadata[META_DISTRIBUTION_KEYWORD] == distribution:
                pulp_image = image
                break
        if not pulp_image:
            raise ValueError('Distribution [%s] does not exist' % distribution)

        return pulp_image
