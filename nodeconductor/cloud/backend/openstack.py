from __future__ import unicode_literals

import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import six
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient import session
from keystoneclient.auth.identity import v2
from keystoneclient.v2_0 import client as keystone_client
from novaclient.v1_1 import client as nova_client

from nodeconductor.cloud.backend import CloudBackendError


logger = logging.getLogger(__name__)


# noinspection PyMethodMayBeStatic
class OpenStackBackend(object):
    # CloudAccount related methods
    def push_cloud_account(self, cloud_account):
        # There's nothing to push for OpenStack
        pass

    def push_membership(self, membership):
        # Fail fast if no corresponding OpenStack configured in settings
        keystone = self.get_keystone_client(membership.cloud)

        try:
            tenant = self._get_or_create_tenant(membership, keystone)

            username, password = self._get_or_create_user(membership, keystone)

            membership.username = username
            membership.password = password
            membership.tenant_id = tenant.id

            self._ensure_user_is_tenant_admin(membership.username, tenant, keystone)

            membership.save()

            logger.info('Successfully synchronized CloudProjectMembership with id %s', membership.id)
        except keystone_exceptions.ClientException:
            logger.exception('Failed to synchronize CloudProjectMembership with id %s', membership.id)
            six.reraise(CloudBackendError, CloudBackendError())

    def push_ssh_public_key(self, membership, public_key):
        # We want names to be more or less human readable in backend.
        # OpenStack only allows latin letters, digits, dashes, underscores and spaces
        # as key names, thus we mangle the original name.
        safe_name = re.sub(r'[^-a-zA-Z0-9 _]+', '_', public_key.name)
        key_name = '{0}-{1}'.format(public_key.uuid.hex, safe_name)
        key_fingerprint = public_key.fingerprint

        nova = self.get_nova_client(membership)

        try:
            # OpenStack ignores project boundaries when dealing with keys,
            # so the same key can be already there given it was propagated
            # via a different project
            logger.debug('Retrieving list of keys existing on backend')
            published_keys = set((k.name, k.fingerprint) for k in nova.keypairs.list())
            if (key_name, key_fingerprint) not in published_keys:
                logger.info('Propagating ssh public key %s (%s) to backend', key_fingerprint, key_name)
                nova.keypairs.create(name=key_name, public_key=public_key.public_key)
            else:
                logger.info('Not propagating ssh public key; key already exists on backend')
        except keystone_exceptions.AuthorizationFailure:
            logger.warning('Failed to propagate ssh public key; authorization failed', exc_info=1)
            six.reraise(CloudBackendError, CloudBackendError())

    def pull_flavors(self, membership):
        # TODO: Get list of flavors from DB
        # TODO: Get list of flavors from OpenStack
        # TODO: Remove non-matching from DB
        # TODO: Add missing to DB
        pass

    # Helper methods
    def get_credentials(self, keystone_url):
        nc_settings = getattr(settings, 'NODE_CONDUCTOR', {})
        openstacks = nc_settings.get('OPENSTACK_CREDENTIALS', ())

        try:
            return next(o for o in openstacks if o['keystone_url'] == keystone_url)
        except StopIteration:
            logger.exception('Failed to find OpenStack credentials for Keystone URL %s', keystone_url)
            six.reraise(CloudBackendError, CloudBackendError())

    def get_keystone_client(self, cloud):
        credentials = self.get_credentials(cloud.auth_url)
        try:
            return keystone_client.Client(
                username=credentials['username'],
                password=credentials['password'],
                tenant_name=credentials['tenant'],
                auth_url=credentials['keystone_url'],
            )
        except keystone_exceptions.Unauthorized:
            logger.exception('Failed to log into keystone at endpoint %s using admin account',
                             cloud.auth_url)
            six.reraise(CloudBackendError, CloudBackendError())

    def get_nova_client(self, membership):
        auth_plugin = v2.Password(
            auth_url=membership.cloud.auth_url,
            username=membership.username,
            password=membership.password,
            tenant_id=membership.tenant_id,
        )
        sess = session.Session(auth=auth_plugin)
        return nova_client.Client(session=sess)

    def _get_or_create_user(self, membership, keystone):
        # Try to sign in if credentials are already stored in membership
        if membership.username:
            logger.info('Signing in using stored membership credentials')
            keystone_client.Client(
                username=membership.username,
                password=membership.password,
                auth_url=membership.cloud.auth_url,
            )
            logger.info('Successfully logged in, using existing user %s', membership.username)
            return membership.username, membership.password

        # Try to create user in keystone
        username = '{0}-{1}'.format(
            get_user_model().objects.make_random_password(),
            membership.project.name,
        )
        password = get_user_model().objects.make_random_password()

        logger.info('Creating keystone user %s', username)
        keystone.users.create(
            name=username,
            password=password,
        )

        logger.info('Successfully created keystone user %s', username)
        return username, password

    def _get_or_create_tenant(self, membership, keystone):
        tenant_name = '{0}-{1}'.format(membership.project.uuid.hex, membership.project.name)

        # First try to create a tenant
        logger.info('Creating tenant %s', tenant_name)

        try:
            return keystone.tenants.create(
                tenant_name=tenant_name,
                description=membership.project.description,
            )
        except keystone_exceptions.Conflict:
            logger.info('Tenant %s already exists, using it instead', tenant_name)

        # Looks like there is a tenant already created, try to look it up
        logger.info('Looking up existing tenant %s', tenant_name)
        return keystone.tenants.find(name=tenant_name)

    def _ensure_user_is_tenant_admin(self, username, tenant, keystone):
        logger.info('Assigning admin role to user %s within tenant %s',
                    username, tenant.name)

        logger.debug('Looking up cloud admin user %s', username)
        admin_user = keystone.users.find(name=username)

        logger.debug('Looking up admin role')
        admin_role = keystone.roles.find(name='admin')

        try:
            keystone.users.role_manager.add_user_role(
                user=admin_user.id,
                role=admin_role.id,
                tenant=tenant.id,
            )
        except keystone_exceptions.Conflict:
            logger.info('User %s already has admin role within tenant %s',
                        username, tenant.name)
