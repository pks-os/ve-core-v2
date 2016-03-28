from __future__ import unicode_literals

import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import transaction

from nodeconductor.core.tasks import transition, retry_if_false, save_error_message, throttle
from nodeconductor.core.models import SshPublicKey, SynchronizationStates
from nodeconductor.iaas.backend import CloudBackendError
from nodeconductor.structure import (SupportedServices, ServiceBackendError,
                                     ServiceBackendNotImplemented, models)
from nodeconductor.structure.utils import deserialize_ssh_key, deserialize_user, GeoIpException

logger = logging.getLogger(__name__)


@shared_task(name='nodeconductor.structure.sync_service_settings', heavy_task=True)
@throttle(concurrency=2, key='service_settings_sync')
def sync_service_settings(settings_uuids=None):
    settings = models.ServiceSettings.objects.all()
    if settings_uuids:
        if not isinstance(settings_uuids, (list, tuple)):
            settings_uuids = [settings_uuids]
        settings = settings.filter(uuid__in=settings_uuids)
    else:
        settings = settings.filter(state=SynchronizationStates.IN_SYNC)

    for obj in settings:
        settings_uuid = obj.uuid.hex
        if obj.state == SynchronizationStates.IN_SYNC:
            obj.schedule_syncing()
            obj.save()

            begin_syncing_service_settings.apply_async(
                args=(settings_uuid,),
                link=sync_service_settings_succeeded.si(settings_uuid),
                link_error=sync_service_settings_failed.si(settings_uuid))
        elif obj.state == SynchronizationStates.CREATION_SCHEDULED:
            begin_creating_service_settings.apply_async(
                args=(settings_uuid,),
                link=sync_service_settings_succeeded.si(settings_uuid),
                link_error=sync_service_settings_failed.si(settings_uuid))
        else:
            logger.warning('Cannot sync service settings %s from state %s', obj.name, obj.state)


@shared_task
@transition(models.ServiceSettings, 'schedule_syncing')
@save_error_message
def begin_recovering_erred_service_settings(settings_uuid, transition_entity=None):
    settings = transition_entity
    try:
        backend = settings.get_backend()
        is_active = backend.ping()
    except ServiceBackendNotImplemented:
        is_active = False

    if is_active:
        # Recovered service settings should be synchronised
        begin_syncing_service_settings.apply_async(
                args=(settings_uuid,),
                link=sync_service_settings_succeeded.si(settings_uuid, recovering=True),
                link_error=sync_service_settings_failed.si(settings_uuid, recovering=True))
    else:
        settings.set_erred()
        settings.error_message = 'Failed to ping service settings %s' % settings.name
        settings.save()
        logger.info('Failed to recover service settings %s.' % settings.name)


@shared_task(name='nodeconductor.structure.recover_service_settings')
def recover_erred_service_settings(settings_uuids=None):
    settings_list = models.ServiceSettings.objects.all()
    if settings_uuids:
        if not isinstance(settings_uuids, (list, tuple)):
            settings_uuids = [settings_uuids]
        settings_list = settings_list.filter(uuid__in=settings_uuids)
    else:
        settings_list = settings_list.filter(state=SynchronizationStates.ERRED)

    for settings in settings_list:
        if settings.state == SynchronizationStates.ERRED:
            settings_uuid = settings.uuid.hex
            begin_recovering_erred_service_settings.delay(settings_uuid)
        else:
            logger.warning('Cannot recover service settings %s from state %s', settings.name, settings.state)


@shared_task
@transition(models.ServiceSettings, 'begin_syncing')
@save_error_message
def begin_syncing_service_settings(settings_uuid, transition_entity=None):
    settings = transition_entity
    try:
        backend = settings.get_backend()
        backend.sync()
    except ServiceBackendNotImplemented:
        pass


@shared_task
@transition(models.ServiceSettings, 'begin_creating')
@save_error_message
def begin_creating_service_settings(settings_uuid, transition_entity=None):
    settings = transition_entity
    try:
        backend = settings.get_backend()
        backend.sync()
    except ServiceBackendNotImplemented:
        pass


@shared_task
@transition(models.ServiceSettings, 'set_in_sync')
def sync_service_settings_succeeded(settings_uuid, transition_entity=None, recovering=False):
    if recovering:
        settings = transition_entity
        settings.error_message = ''
        settings.save()
        logger.info('Service settings %s successfully recovered.' % settings.name)


@shared_task
@transition(models.ServiceSettings, 'set_erred')
def sync_service_settings_failed(settings_uuid, transition_entity=None, recovering=False):
    if recovering:
        logger.info('Failed to recover service settings %s.' % transition_entity.name)


@shared_task
def recover_erred_service(service_project_link_str, is_iaas=False):
    try:
        spl = next(models.ServiceProjectLink.from_string(service_project_link_str))
    except StopIteration:
        logger.warning('Missing service project link %s.', service_project_link_str)
        return

    settings = spl.cloud if is_iaas else spl.service.settings

    try:
        backend = spl.get_backend()
        if is_iaas:
            try:
                if spl.state == SynchronizationStates.ERRED:
                    backend.create_session(membership=spl)
                if spl.cloud.state == SynchronizationStates.ERRED:
                    backend.create_session(keystone_url=spl.cloud.auth_url)
            except CloudBackendError:
                is_active = False
            else:
                is_active = True
        else:
            is_active = backend.ping()
    except (ServiceBackendError, ServiceBackendNotImplemented):
        is_active = False

    if is_active:
        for entity in (spl, settings):
            if entity.state == SynchronizationStates.ERRED:
                entity.set_in_sync_from_erred()
                entity.save()
    else:
        logger.info('Failed to recover service settings %s.' % settings)


@shared_task(name='nodeconductor.structure.push_ssh_public_keys')
def push_ssh_public_keys(service_project_links):
    link_objects = models.ServiceProjectLink.from_string(service_project_links)
    for link in link_objects:
        str_link = link.to_string()

        ssh_keys = SshPublicKey.objects.filter(user__groups__projectrole__project=link.project)
        if not ssh_keys.exists():
            logger.debug('There are no SSH public keys to push for link %s', str_link)
            continue

        for key in ssh_keys:
            push_ssh_public_key.delay(key.uuid.hex, str_link)


@shared_task(name='nodeconductor.structure.push_ssh_public_key', max_retries=120, default_retry_delay=30)
@retry_if_false
def push_ssh_public_key(ssh_public_key_uuid, service_project_link_str):
    try:
        public_key = SshPublicKey.objects.get(uuid=ssh_public_key_uuid)
    except SshPublicKey.DoesNotExist:
        logger.warning('Missing public key %s.', ssh_public_key_uuid)
        return True
    try:
        service_project_link = next(models.ServiceProjectLink.from_string(service_project_link_str))
    except StopIteration:
        logger.warning('Missing service project link %s.', service_project_link_str)
        return True

    if service_project_link.state != SynchronizationStates.IN_SYNC:
        logger.debug(
            'Not pushing public keys for service project link %s which is in state %s.',
            service_project_link_str, service_project_link.get_state_display())

        if service_project_link.state != SynchronizationStates.ERRED:
            logger.debug(
                'Rescheduling synchronisation of keys for link %s in state %s.',
                service_project_link_str, service_project_link.get_state_display())

            # retry a task if service project link is not in a sane state
            return False

    backend = service_project_link.get_backend()
    try:
        backend.add_ssh_key(public_key, service_project_link)
        logger.info(
            'SSH key %s has been pushed to service project link %s.',
            public_key.uuid, service_project_link_str)
    except ServiceBackendNotImplemented:
        pass
    except (ServiceBackendError, CloudBackendError):
        logger.warning(
            'Failed to push SSH key %s to service project link %s.',
            public_key.uuid, service_project_link_str,
            exc_info=1)

    return True


@shared_task(name='nodeconductor.structure.remove_ssh_public_key')
def remove_ssh_public_key(key_data, service_project_link_str):
    public_key = deserialize_ssh_key(key_data)
    try:
        service_project_link = next(models.ServiceProjectLink.from_string(service_project_link_str))
    except StopIteration:
        logger.warning('Missing service project link %s.', service_project_link_str)
        return True

    try:
        backend = service_project_link.get_backend()
        backend.remove_ssh_key(public_key, service_project_link)
        logger.info(
            'SSH key %s has been removed from service project link %s.',
            public_key.uuid, service_project_link_str)
    except ServiceBackendNotImplemented:
        pass
    except (ServiceBackendError, CloudBackendError):
        logger.warning(
            'Failed to remove SSH key %s from service project link %s.',
            public_key.uuid, service_project_link_str,
            exc_info=1)


@shared_task(name='nodeconductor.structure.add_user', max_retries=120, default_retry_delay=30)
@retry_if_false
def add_user(user_uuid, service_project_link_str):
    try:
        user = get_user_model().objects.get(uuid=user_uuid)
    except get_user_model().DoesNotExist:
        logger.warning('Missing user %s.', user_uuid)
        return True
    try:
        service_project_link = next(models.ServiceProjectLink.from_string(service_project_link_str))
    except StopIteration:
        logger.warning('Missing service project link %s.', service_project_link_str)
        return True

    if service_project_link.state != SynchronizationStates.IN_SYNC:
        logger.debug(
            'Not adding users for service project link %s which is in state %s.',
            service_project_link_str, service_project_link.get_state_display())

        if service_project_link.state != SynchronizationStates.ERRED:
            logger.debug(
                'Rescheduling synchronisation of users for link %s in state %s.',
                service_project_link_str, service_project_link.get_state_display())

            # retry a task if service project link is not in a sane state
            return False

    backend = service_project_link.get_backend()
    try:
        backend.add_user(user, service_project_link)
        logger.info(
            'User %s has been added to service project link %s.',
            user.uuid, service_project_link_str)
    except ServiceBackendNotImplemented:
        pass
    except (ServiceBackendError, CloudBackendError):
        logger.warning(
            'Failed to add user %s for service project link %s',
            user.uuid, service_project_link_str,
            exc_info=1)

    return True


@shared_task(name='nodeconductor.structure.remove_user')
def remove_user(user_data, service_project_link_str):
    user = deserialize_user(user_data)
    try:
        service_project_link = next(models.ServiceProjectLink.from_string(service_project_link_str))
    except StopIteration:
        logger.warning('Missing service project link %s.', service_project_link_str)
        return True

    try:
        backend = service_project_link.get_backend()
        backend.remove_user(user, service_project_link)
        logger.info(
            'User %s has been removed from service project link %s.',
            user.uuid, service_project_link_str)
    except ServiceBackendNotImplemented:
        pass


@shared_task(name='nodeconductor.structure.detect_vm_coordinates_batch')
def detect_vm_coordinates_batch(virtual_machines):
    for vm in models.Resource.from_string(virtual_machines):
        detect_vm_coordinates.delay(vm.to_string())


@shared_task(name='nodeconductor.structure.detect_vm_coordinates')
def detect_vm_coordinates(vm_str):
    try:
        vm = next(models.Resource.from_string(vm_str))
    except StopIteration:
        logger.warning('Missing virtual machine %s.', vm_str)
        return

    try:
        coordinates = vm.detect_coordinates()
    except GeoIpException as e:
        logger.warning('Unable to detect coordinates for virtual machines %s: %s.', vm_str, e)
        return

    if coordinates:
        vm.latitude = coordinates.latitude
        vm.longitude = coordinates.longitude
        vm.save(update_fields=['latitude', 'longitude'])


@shared_task(name='nodeconductor.structure.create_spls_and_services_for_shared_settings')
def create_spls_and_services_for_shared_settings(settings_uuids=None):
    shared_settings = models.ServiceSettings.objects.all()
    if settings_uuids:
        if not isinstance(settings_uuids, (list, tuple)):
            settings_uuids = [settings_uuids]
        shared_settings = shared_settings.filter(uuid__in=settings_uuids)
    else:
        shared_settings = shared_settings.filter(state=SynchronizationStates.IN_SYNC, shared=True)

    for settings in shared_settings:
        service_model = SupportedServices.get_service_models()[settings.type]['service']

        with transaction.atomic():
            for customer in models.Customer.objects.all():
                services = service_model.objects.filter(customer=customer, settings=settings)
                if not services.exists():
                    service = service_model.objects.create(
                        customer=customer, settings=settings, name=settings.name, available_for_all=True)
                else:
                    service = services.first()

                service_project_link_model = service.projects.through
                for project in service.customer.projects.all():
                    spl = service_project_link_model.objects.filter(project=project, service=service)
                    if not spl.exists():
                        service_project_link_model.objects.create(project=project, service=service)
