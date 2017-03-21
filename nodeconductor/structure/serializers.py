from __future__ import unicode_literals

import json
import logging

from collections import defaultdict

import pyvat
from django.conf import settings
from django.contrib import auth
import django.core.exceptions as django_exceptions
from django.core.validators import RegexValidator, MaxLengthValidator
from django.db import models as django_models, transaction
from django.utils import six, timezone
from django.utils.functional import cached_property
from rest_framework import exceptions, serializers
from rest_framework.reverse import reverse

from nodeconductor.core import (models as core_models, serializers as core_serializers, utils as core_utils)
from nodeconductor.core.fields import MappedChoiceField
from nodeconductor.monitoring.serializers import MonitoringSerializerMixin
from nodeconductor.quotas import serializers as quotas_serializers
from nodeconductor.structure import (models, SupportedServices, ServiceBackendError, ServiceBackendNotImplemented,
                                     executors)
from nodeconductor.structure.managers import filter_queryset_for_user
from nodeconductor.structure.models import ServiceProjectLink

User = auth.get_user_model()
logger = logging.getLogger(__name__)


class IpCountValidator(MaxLengthValidator):
    message = 'Only %(limit_value)s ip address is supported.'


class PermissionFieldFilteringMixin(object):
    """
    Mixin allowing to filter related fields.

    In order to constrain the list of entities that can be used
    as a value for the field:

    1. Make sure that the entity in question has corresponding
       Permission class defined.

    2. Implement `get_filtered_field_names()` method
       in the class that this mixin is mixed into and return
       the field in question from that method.
    """

    def get_fields(self):
        fields = super(PermissionFieldFilteringMixin, self).get_fields()

        try:
            request = self.context['request']
            user = request.user
        except (KeyError, AttributeError):
            return fields

        for field_name in self.get_filtered_field_names():
            if field_name not in fields:  # field could be not required by user
                continue
            field = fields[field_name]
            field.queryset = filter_queryset_for_user(field.queryset, user)

        return fields

    def get_filtered_field_names(self):
        raise NotImplementedError(
            'Implement get_filtered_field_names() '
            'to return list of filtered fields')


class PermissionListSerializer(serializers.ListSerializer):
    """
    Allows to filter related queryset by user.
    Counterpart of PermissionFieldFilteringMixin.

    In order to use it set Meta.list_serializer_class. Example:

    >>> class PermissionProjectSerializer(BasicProjectSerializer):
    >>>     class Meta(BasicProjectSerializer.Meta):
    >>>         list_serializer_class = PermissionListSerializer
    >>>
    >>> class CustomerSerializer(serializers.HyperlinkedModelSerializer):
    >>>     projects = PermissionProjectSerializer(many=True, read_only=True)
    """
    def to_representation(self, data):
        try:
            request = self.context['request']
            user = request.user
        except (KeyError, AttributeError):
            pass
        else:
            if isinstance(data, (django_models.Manager, django_models.query.QuerySet)):
                data = filter_queryset_for_user(data.all(), user)

        return super(PermissionListSerializer, self).to_representation(data)


class BasicUserSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = User
        fields = ('url', 'uuid', 'username', 'full_name', 'native_name',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class BasicProjectSerializer(core_serializers.BasicInfoSerializer):
    class Meta(core_serializers.BasicInfoSerializer.Meta):
        model = models.Project


class PermissionProjectSerializer(BasicProjectSerializer):
    class Meta(BasicProjectSerializer.Meta):
        list_serializer_class = PermissionListSerializer


class NestedServiceProjectLinkSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField(source='service.uuid')
    url = serializers.SerializerMethodField()
    service_project_link_url = serializers.SerializerMethodField()
    name = serializers.ReadOnlyField(source='service.settings.name')
    type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    shared = serializers.SerializerMethodField()
    settings_uuid = serializers.ReadOnlyField(source='service.settings.uuid')
    settings = serializers.SerializerMethodField()
    validation_state = serializers.ChoiceField(
        choices=models.ServiceProjectLink.States.CHOICES,
        read_only=True,
        help_text='A state of service compliance with project requirements.')
    validation_message = serializers.ReadOnlyField(
        help_text='An error message for a service that is non-compliant with project requirements.')

    def get_settings(self, link):
        """
        URL of service settings
        """
        return reverse(
            'servicesettings-detail', kwargs={'uuid': link.service.settings.uuid}, request=self.context['request'])

    def get_url(self, link):
        """
        URL of service
        """
        view_name = SupportedServices.get_detail_view_for_model(link.service)
        return reverse(view_name, kwargs={'uuid': link.service.uuid.hex}, request=self.context['request'])

    def get_service_project_link_url(self, link):
        view_name = SupportedServices.get_detail_view_for_model(link)
        return reverse(view_name, kwargs={'pk': link.id}, request=self.context['request'])

    def get_type(self, link):
        return SupportedServices.get_name_for_model(link.service)

    # XXX: SPL is intended to become stateless. For backward compatiblity we are returning here state from connected
    # service settings. To be removed once SPL becomes stateless.
    def get_state(self, link):
        return link.service.settings.get_state_display()

    def get_resources_count(self, link):
        """
        Count total number of all resources connected to link
        """
        total = 0
        for model in SupportedServices.get_service_resources(link.service):
            # Format query path from resource to service project link
            query = {model.Permissions.project_path.split('__')[0]: link}
            total += model.objects.filter(**query).count()
        return total

    def get_shared(self, link):
        return link.service.settings.shared


class NestedServiceCertificationSerializer(core_serializers.AugmentedSerializerMixin,
                                           core_serializers.HyperlinkedRelatedModelSerializer):
    class Meta(object):
        model = models.ServiceCertification
        fields = ('uuid', 'url', 'name', 'description', 'link')
        read_only_fields = ('name', 'description', 'link')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }


class ProjectSerializer(core_serializers.RestrictedSerializerMixin,
                        PermissionFieldFilteringMixin,
                        core_serializers.AugmentedSerializerMixin,
                        serializers.HyperlinkedModelSerializer):
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)
    services = serializers.SerializerMethodField()
    certifications = NestedServiceCertificationSerializer(
        queryset=models.ServiceCertification.objects.all(),
        many=True, required=False)

    class Meta(object):
        model = models.Project
        fields = (
            'url', 'uuid',
            'name',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'customer_abbreviation',
            'description',
            'quotas',
            'services',
            'created',
            'certifications',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'certifications': {'lookup_field': 'uuid'},
        }
        related_paths = {
            'customer': ('uuid', 'name', 'native_name', 'abbreviation')
        }

    @staticmethod
    def eager_load(queryset):
        related_fields = (
            'uuid',
            'name',
            'created',
            'description',
            'customer__uuid',
            'customer__name',
            'customer__native_name',
            'customer__abbreviation',
        )
        return queryset.select_related('customer').only(*related_fields)\
            .prefetch_related('quotas', 'certifications')

    def create(self, validated_data):
        certifications = validated_data.pop('certifications', [])
        project = super(ProjectSerializer, self).create(validated_data)
        project.certifications.add(*certifications)

        return project

    def get_filtered_field_names(self):
        return 'customer',

    def get_services(self, project):
        if 'services' not in self.context:
            self.context['services'] = self.get_services_map()
        services = self.context['services'][project.pk]

        serializer = NestedServiceProjectLinkSerializer(
            services,
            many=True,
            read_only=True,
            context={'request': self.context['request']})
        return serializer.data

    def get_services_map(self):
        services = defaultdict(list)
        related_fields = (
            'id',
            'service__settings__state',
            'project_id',
            'service__uuid',
            'service__settings__uuid',
            'service__settings__shared',
            'service__settings__name',
        )
        for link_model in ServiceProjectLink.get_all_models():
            links = (link_model.objects.all()
                     .select_related('service', 'service__settings')
                     .only(*related_fields)
                     .prefetch_related('service__settings__certifications'))
            if isinstance(self.instance, list):
                links = links.filter(project__in=self.instance)
            else:
                links = links.filter(project=self.instance)
            for link in links:
                services[link.project_id].append(link)
        return services


class CustomerImageSerializer(serializers.ModelSerializer):
    image = serializers.ImageField()

    class Meta:
        model = models.Customer
        fields = ['image']


class CustomerSerializer(core_serializers.RestrictedSerializerMixin,
                         core_serializers.AugmentedSerializerMixin,
                         serializers.HyperlinkedModelSerializer,):
    projects = PermissionProjectSerializer(many=True, read_only=True)
    owners = BasicUserSerializer(source='get_owners', many=True, read_only=True)
    support_users = BasicUserSerializer(source='get_support_users', many=True, read_only=True)
    image = serializers.SerializerMethodField()
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    class Meta(object):
        model = models.Customer
        fields = (
            'url',
            'uuid',
            'name', 'native_name', 'abbreviation', 'contact_details',
            'projects',
            'owners', 'support_users', 'balance',
            'registration_code',
            'quotas',
            'image',
            'country', 'vat_code', 'is_company'
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }
        # Balance should be modified by nodeconductor_paypal app
        read_only_fields = ('balance',)

    def get_image(self, customer):
        if not customer.image:
            return settings.NODECONDUCTOR.get('DEFAULT_CUSTOMER_LOGO')
        return reverse('customer_image', kwargs={'uuid': customer.uuid}, request=self.context['request'])

    @staticmethod
    def eager_load(queryset):
        return queryset.prefetch_related('quotas', 'projects')

    def validate(self, attrs):
        country = attrs.get('country')
        vat_code = attrs.get('vat_code')
        is_company = attrs.get('is_company')

        if vat_code:
            if not is_company:
                raise serializers.ValidationError({
                    'vat_code': 'VAT number is not supported for private persons.'})

            # Check VAT format
            if not pyvat.is_vat_number_format_valid(vat_code, country):
                raise serializers.ValidationError({'vat_code': 'VAT number has invalid format.'})

            # Check VAT number in EU VAT Information Exchange System
            # if customer is new or either VAT number or country of the customer has changed
            if not self.instance or self.instance.vat_code != vat_code or self.instance.country != country:
                check_result = pyvat.check_vat_number(vat_code, country)
                if check_result.is_valid:
                    attrs['vat_name'] = check_result.business_name
                    attrs['vat_address'] = check_result.business_address
                    if not attrs.get('contact_details'):
                        attrs['contact_details'] = attrs['vat_address']
                elif check_result.is_valid is False:
                    raise serializers.ValidationError({'vat_code': 'VAT number is invalid.'})
                else:
                    logger.debug('Unable to check VAT number %s for country %s. Error message: %s',
                                 vat_code, country, check_result.log_lines)
                    raise serializers.ValidationError({'vat_code': 'Unable to check VAT number.'})
        return attrs


class NestedProjectPermissionSerializer(serializers.ModelSerializer):
    url = serializers.HyperlinkedRelatedField(
        source='project',
        lookup_field='uuid',
        view_name='project-detail',
        queryset=models.Project.objects.all(),
    )
    uuid = serializers.ReadOnlyField(source='project.uuid')
    name = serializers.ReadOnlyField(source='project.name')
    permission = serializers.HyperlinkedRelatedField(
        source='pk',
        view_name='project_permission-detail',
        queryset=models.ProjectPermission.objects.all(),
    )

    class Meta:
        model = models.ProjectPermission
        fields = ['url', 'uuid', 'name', 'role', 'permission', 'expiration_time']


class CustomerUserSerializer(serializers.ModelSerializer):
    role = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField(source='perm.expiration_time')
    permission = serializers.HyperlinkedRelatedField(
        source='perm.pk',
        view_name='customer_permission-detail',
        queryset=models.CustomerPermission.objects.all(),
    )
    projects = NestedProjectPermissionSerializer(many=True, read_only=True)

    class Meta:
        model = User
        fields = ['url', 'uuid', 'username', 'full_name', 'email', 'role', 'permission', 'projects',
                  'expiration_time']
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        customer = self.context['customer']
        permission = models.CustomerPermission.objects.filter(
            customer=customer, user=user, is_active=True).first()
        projects = models.ProjectPermission.objects.filter(
            project__customer=customer, user=user, is_active=True)
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        setattr(user, 'projects', projects)
        return super(CustomerUserSerializer, self).to_representation(user)


class ProjectUserSerializer(serializers.ModelSerializer):
    role = serializers.ReadOnlyField()
    expiration_time = serializers.ReadOnlyField(source='perm.expiration_time')
    permission = serializers.HyperlinkedRelatedField(
        source='perm.pk',
        view_name='project_permission-detail',
        queryset=models.ProjectPermission.objects.all(),
    )

    class Meta:
        model = User
        fields = ['url', 'uuid', 'username', 'full_name', 'email', 'role', 'permission',
                  'expiration_time']
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def to_representation(self, user):
        project = self.context['project']
        permission = models.ProjectPermission.objects.filter(
            project=project, user=user, is_active=True).first()
        setattr(user, 'perm', permission)
        setattr(user, 'role', permission and permission.role)
        return super(ProjectUserSerializer, self).to_representation(user)


class BalanceHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = models.BalanceHistory
        fields = ['created', 'amount']


class BasePermissionSerializer(core_serializers.AugmentedSerializerMixin, serializers.HyperlinkedModelSerializer):
    class Meta(object):
        fields = ('user', 'user_full_name', 'user_native_name', 'user_username', 'user_uuid', 'user_email')
        related_paths = {
            'user': ('username', 'full_name', 'native_name', 'uuid', 'email'),
        }

    def validate_user(self, user):
        if self.context['request'].user == user:
            raise serializers.ValidationError('It is impossible to edit permissions for yourself.')
        return user


class CustomerPermissionSerializer(PermissionFieldFilteringMixin, BasePermissionSerializer):
    class Meta(BasePermissionSerializer.Meta):
        model = models.CustomerPermission
        fields = (
            'url', 'pk', 'role', 'created', 'expiration_time', 'created_by',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'customer_abbreviation',
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            customer=('name', 'native_name', 'abbreviation', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = (
            'customer', 'role', 'user', 'created_by', 'created'
        )
        extra_kwargs = {
            'user': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'queryset': User.objects.all(),
            },
            'created_by': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'read_only': True,
            },
            'customer': {
                'view_name': 'customer-detail',
                'lookup_field': 'uuid',
                'queryset': models.Customer.objects.all(),
            }
        }

    def validate(self, data):
        if not self.instance:
            customer = data['customer']
            user = data['user']

            if customer.has_user(user):
                raise serializers.ValidationError('The fields customer and user must make a unique set.')

        return data

    def create(self, validated_data):
        customer = validated_data['customer']
        user = validated_data['user']
        role = validated_data['role']
        expiration_time = validated_data.get('expiration_time')

        created_by = self.context['request'].user
        permission, _ = customer.add_user(user, role, created_by, expiration_time)

        return permission

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise serializers.ValidationError('Expiration time should be greater than current time')
        return value

    def get_filtered_field_names(self):
        return ('customer',)


class CustomerPermissionLogSerializer(CustomerPermissionSerializer):
    class Meta(CustomerPermissionSerializer.Meta):
        view_name = 'customer_permission_log-detail'


class ProjectPermissionSerializer(PermissionFieldFilteringMixin, BasePermissionSerializer):
    customer_name = serializers.ReadOnlyField(source='project.customer.name')

    class Meta(BasePermissionSerializer.Meta):
        model = models.ProjectPermission
        fields = (
            'url', 'pk', 'role', 'created', 'expiration_time', 'created_by',
            'project', 'project_uuid', 'project_name', 'customer_name'
        ) + BasePermissionSerializer.Meta.fields
        related_paths = dict(
            project=('name', 'uuid'),
            **BasePermissionSerializer.Meta.related_paths
        )
        protected_fields = (
            'project', 'role', 'user', 'created_by', 'created'
        )
        extra_kwargs = {
            'user': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'queryset': User.objects.all(),
            },
            'created_by': {
                'view_name': 'user-detail',
                'lookup_field': 'uuid',
                'read_only': True,
            },
            'project': {
                'view_name': 'project-detail',
                'lookup_field': 'uuid',
                'queryset': models.Project.objects.all(),
            }
        }

    def validate(self, data):
        if not self.instance:
            project = data['project']
            user = data['user']

            if project.has_user(user):
                raise serializers.ValidationError('The fields project and user must make a unique set.')

        return data

    def create(self, validated_data):
        project = validated_data['project']
        user = validated_data['user']
        role = validated_data['role']
        expiration_time = validated_data.get('expiration_time')

        created_by = self.context['request'].user
        permission, _ = project.add_user(user, role, created_by, expiration_time)

        return permission

    def validate_expiration_time(self, value):
        if value is not None and value < timezone.now():
            raise serializers.ValidationError('Expiration time should be greater than current time')
        return value

    def get_filtered_field_names(self):
        return ('project',)


class ProjectPermissionLogSerializer(ProjectPermissionSerializer):
    class Meta(ProjectPermissionSerializer.Meta):
        view_name = 'project_permission_log-detail'


class UserOrganizationSerializer(serializers.Serializer):
    organization = serializers.CharField(max_length=80)


class UserSerializer(serializers.HyperlinkedModelSerializer):
    email = serializers.EmailField()
    agree_with_policy = serializers.BooleanField(write_only=True, required=False,
                                                 help_text='User must agree with the policy to register.')
    preferred_language = serializers.ChoiceField(choices=settings.LANGUAGES, allow_blank=True, required=False)
    competence = serializers.ChoiceField(choices=settings.NODECONDUCTOR.get('USER_COMPETENCE_LIST', []),
                                         allow_blank=True,
                                         required=False)
    token = serializers.ReadOnlyField(source='auth_token.key')

    class Meta(object):
        model = User
        fields = (
            'url',
            'uuid', 'username',
            'full_name', 'native_name',
            'job_title', 'email', 'phone_number',
            'organization', 'organization_approved',
            'civil_number',
            'description',
            'is_staff', 'is_active', 'is_support',
            'token', 'token_lifetime',
            'registration_method',
            'date_joined',
            'agree_with_policy',
            'agreement_date',
            'preferred_language',
            'competence'
        )
        read_only_fields = (
            'uuid',
            'civil_number',
            'organization_approved',
            'registration_method',
            'date_joined',
            'agreement_date',
        )
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_fields(self):
        fields = super(UserSerializer, self).get_fields()

        try:
            request = self.context['view'].request
            user = request.user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff and not user.is_support:
            del fields['is_active']
            del fields['is_staff']
            del fields['description']

        if not user.is_staff and self.instance != user:
            del fields['token']
            del fields['token_lifetime']

        if request.method in ('PUT', 'PATCH'):
            fields['username'].read_only = True

        return fields

    def validate(self, attrs):
        agree_with_policy = attrs.pop('agree_with_policy', False)
        if self.instance and not self.instance.agreement_date:
            if not agree_with_policy:
                raise serializers.ValidationError({'agree_with_policy': 'User must agree with the policy.'})
            else:
                attrs['agreement_date'] = timezone.now()

        # Convert validation error from Django to DRF
        # https://github.com/tomchristie/django-rest-framework/issues/2145
        try:
            user = User(id=getattr(self.instance, 'id', None), **attrs)
            user.clean()
        except django_exceptions.ValidationError as error:
            raise exceptions.ValidationError(error.message_dict)
        return attrs


class CreationTimeStatsSerializer(serializers.Serializer):
    MODEL_NAME_CHOICES = (('project', 'project'), ('customer', 'customer'),)
    MODEL_CLASSES = {'project': models.Project, 'customer': models.Customer}

    model_name = serializers.ChoiceField(choices=MODEL_NAME_CHOICES)
    start_timestamp = serializers.IntegerField(min_value=0)
    end_timestamp = serializers.IntegerField(min_value=0)
    segments_count = serializers.IntegerField(min_value=0)

    def get_stats(self, user):
        start_datetime = core_utils.timestamp_to_datetime(self.data['start_timestamp'])
        end_datetime = core_utils.timestamp_to_datetime(self.data['end_timestamp'])

        model = self.MODEL_CLASSES[self.data['model_name']]
        filtered_queryset = filter_queryset_for_user(model.objects.all(), user)
        created_datetimes = (
            filtered_queryset
            .filter(created__gte=start_datetime, created__lte=end_datetime)
            .values('created')
            .annotate(count=django_models.Count('id', distinct=True)))

        time_and_value_list = [
            (core_utils.datetime_to_timestamp(dt['created']), dt['count']) for dt in created_datetimes]

        return core_utils.format_time_and_value_to_segment_list(
            time_and_value_list, self.data['segments_count'],
            self.data['start_timestamp'], self.data['end_timestamp'])


class PasswordSerializer(serializers.Serializer):
    password = serializers.CharField(min_length=7, validators=[
        RegexValidator(
            regex='\d',
            message='Ensure this field has at least one digit.',
        ),
        RegexValidator(
            regex='[a-zA-Z]',
            message='Ensure this field has at least one latin letter.',
        ),
    ])


class SshKeySerializer(serializers.HyperlinkedModelSerializer):
    user_uuid = serializers.ReadOnlyField(source='user.uuid')

    class Meta(object):
        model = core_models.SshPublicKey
        fields = ('url', 'uuid', 'name', 'public_key', 'fingerprint', 'user_uuid')
        read_only_fields = ('fingerprint',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def validate(self, attrs):
        try:
            fingerprint = core_models.get_ssh_key_fingerprint(attrs['public_key'])
        except (IndexError, TypeError):
            raise serializers.ValidationError('Key is not valid: cannot generate fingerprint from it.')
        if core_models.SshPublicKey.objects.filter(fingerprint=fingerprint).exists():
            raise serializers.ValidationError('Key with same fingerprint already exists')
        return attrs

    def get_fields(self):
        fields = super(SshKeySerializer, self).get_fields()

        try:
            user = self.context['request'].user
        except (KeyError, AttributeError):
            return fields

        if not user.is_staff:
            del fields['user_uuid']

        return fields


class ServiceCertificationsUpdateSerializer(serializers.Serializer):
    certifications = NestedServiceCertificationSerializer(
        queryset=models.ServiceCertification.objects.all(),
        required=True,
        many=True)

    @transaction.atomic
    def update(self, instance, validated_data):
        certifications = validated_data.pop('certifications', None)
        instance.certifications.clear()
        instance.certifications.add(*certifications)
        return instance


class ServiceCertificationSerializer(serializers.HyperlinkedModelSerializer):
    class Meta(object):
        model = models.ServiceCertification
        fields = ('uuid', 'url', 'name', 'description', 'link')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid', 'view_name': 'service-certification-detail'},
        }


class ServiceSettingsSerializer(PermissionFieldFilteringMixin,
                                core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer):
    customer_native_name = serializers.ReadOnlyField(source='customer.native_name')
    state = MappedChoiceField(
        choices=[(v, k) for k, v in core_models.SynchronizationStates.CHOICES],
        choice_mappings={v: k for k, v in core_models.SynchronizationStates.CHOICES},
        read_only=True)
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)
    scope = core_serializers.GenericRelatedField(related_models=models.ResourceMixin.get_all_models(), required=False)
    certifications = NestedServiceCertificationSerializer(many=True, read_only=True)
    geolocations = core_serializers.JSONField(read_only=True)

    class Meta(object):
        model = models.ServiceSettings
        fields = (
            'url', 'uuid', 'name', 'type', 'state', 'error_message', 'shared',
            'backend_url', 'username', 'password', 'token', 'certificate',
            'customer', 'customer_name', 'customer_native_name',
            'homepage', 'terms_of_services', 'certifications',
            'quotas', 'scope', 'geolocations',
        )
        protected_fields = ('type', 'customer')
        read_only_fields = ('shared', 'state', 'error_message')
        related_paths = ('customer',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'certifications': {'lookup_field': 'uuid'},
        }
        write_only_fields = ('backend_url', 'username', 'token', 'password', 'certificate')
        for field in write_only_fields:
            field_params = extra_kwargs.setdefault(field, {})
            field_params['write_only'] = True

    def get_filtered_field_names(self):
        return 'customer',

    @staticmethod
    def eager_load(queryset):
        return queryset.select_related('customer').prefetch_related('quotas', 'certifications')

    def get_fields(self):
        fields = super(ServiceSettingsSerializer, self).get_fields()
        request = self.context['request']

        if isinstance(self.instance, self.Meta.model):
            perm = 'structure.change_%s' % self.Meta.model._meta.model_name
            if request.user.has_perms([perm], self.instance):
                # If user can change settings he should be able to see value
                for field in self.Meta.write_only_fields:
                    fields[field].write_only = False

                serializer = self.get_service_serializer()

                # Remove fields if they are not needed for service
                filter_fields = serializer.SERVICE_ACCOUNT_FIELDS
                if filter_fields is not NotImplemented:
                    for field in self.Meta.write_only_fields:
                        if field in filter_fields:
                            fields[field].help_text = filter_fields[field]
                        elif field in fields:
                            del fields[field]

                # Add extra fields stored in options dictionary
                extra_fields = serializer.SERVICE_ACCOUNT_EXTRA_FIELDS
                if extra_fields is not NotImplemented:
                    for field in extra_fields:
                        fields[field] = serializers.CharField(required=False,
                                                              source='options.' + field,
                                                              allow_blank=True,
                                                              help_text=extra_fields[field])

        if request.method == 'GET':
            fields['type'] = serializers.ReadOnlyField(source='get_type_display')

        return fields

    def get_service_serializer(self):
        service = SupportedServices.get_service_models()[self.instance.type]['service']
        # Find service serializer by service type of settings object
        return next(cls for cls in BaseServiceSerializer.__subclasses__()
                    if cls.Meta.model == service)


class ServiceSerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported services via serializers definition.
        See SupportedServices for details.
    """

    def __new__(cls, name, bases, args):
        SupportedServices.register_service(args['Meta'].model)
        serializer = super(ServiceSerializerMetaclass, cls).__new__(cls, name, bases, args)
        SupportedServices.register_service_serializer(args['Meta'].model, serializer)
        return serializer


class BaseServiceSerializer(six.with_metaclass(ServiceSerializerMetaclass,
                            PermissionFieldFilteringMixin,
                            core_serializers.RestrictedSerializerMixin,
                            core_serializers.AugmentedSerializerMixin,
                            serializers.HyperlinkedModelSerializer)):

    SERVICE_ACCOUNT_FIELDS = NotImplemented
    SERVICE_ACCOUNT_EXTRA_FIELDS = NotImplemented

    projects = BasicProjectSerializer(many=True, read_only=True)
    customer_native_name = serializers.ReadOnlyField(source='customer.native_name')
    settings = serializers.HyperlinkedRelatedField(
        queryset=models.ServiceSettings.objects.filter(shared=True),
        view_name='servicesettings-detail',
        lookup_field='uuid',
        allow_null=True)
    # if project is defined service will be automatically connected to projects customer
    # and SPL between service and project will be created
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all().select_related('customer'),
        view_name='project-detail',
        lookup_field='uuid',
        allow_null=True,
        required=False,
        write_only=True)

    backend_url = serializers.URLField(max_length=200, allow_null=True, write_only=True, required=False)
    username = serializers.CharField(max_length=100, allow_null=True, write_only=True, required=False)
    password = serializers.CharField(max_length=100, allow_null=True, write_only=True, required=False)
    domain = serializers.CharField(max_length=200, allow_null=True, write_only=True, required=False)
    token = serializers.CharField(allow_null=True, write_only=True, required=False)
    certificate = serializers.FileField(allow_null=True, write_only=True, required=False)
    resources_count = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    state = serializers.SerializerMethodField()
    scope = core_serializers.GenericRelatedField(related_models=models.ResourceMixin.get_all_models(), required=False)
    tags = serializers.SerializerMethodField()
    quotas = quotas_serializers.BasicQuotaSerializer(many=True, read_only=True)

    shared = serializers.ReadOnlyField(source='settings.shared')
    error_message = serializers.ReadOnlyField(source='settings.error_message')
    terms_of_services = serializers.ReadOnlyField(source='settings.terms_of_services')
    homepage = serializers.ReadOnlyField(source='settings.homepage')
    geolocations = core_serializers.JSONField(source='settings.geolocations', read_only=True)
    certifications = NestedServiceCertificationSerializer(many=True, read_only=True, source='settings.certifications')
    name = serializers.ReadOnlyField(source='settings.name')

    class Meta(object):
        model = NotImplemented
        fields = (
            'uuid', 'url', 'name', 'state', 'service_type', 'shared',
            'projects', 'project',
            'customer', 'customer_uuid', 'customer_name', 'customer_native_name', 'resources_count',
            'settings', 'settings_uuid', 'backend_url', 'username', 'password',
            'token', 'certificate', 'domain', 'terms_of_services', 'homepage',
            'certifications', 'geolocations', 'available_for_all', 'scope', 'tags', 'quotas',
        )
        settings_fields = ('backend_url', 'username', 'password', 'token', 'certificate', 'scope', 'domain')
        protected_fields = ('customer', 'settings', 'project') + settings_fields
        related_paths = ('customer', 'settings')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
            'customer': {'lookup_field': 'uuid'},
            'settings': {'lookup_field': 'uuid'},
        }

    def __new__(cls, *args, **kwargs):
        if cls.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
            cls.Meta.fields += tuple(cls.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())
            cls.Meta.protected_fields += tuple(cls.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())
        return super(BaseServiceSerializer, cls).__new__(cls, *args, **kwargs)

    @staticmethod
    def eager_load(queryset):
        related_fields = (
            'uuid',
            'available_for_all',
            'customer__uuid',
            'customer__name',
            'customer__native_name',
            'settings__state',
            'settings__uuid',
            'settings__name',
            'settings__type',
            'settings__shared',
            'settings__error_message',
            'settings__options',
            'settings__domain',
            'settings__terms_of_services',
            'settings__homepage',
        )
        queryset = queryset.select_related('customer', 'settings').only(*related_fields)
        projects = models.Project.objects.all().only('uuid', 'name')
        return queryset.prefetch_related(django_models.Prefetch('projects', queryset=projects), 'quotas')

    def get_tags(self, service):
        return service.settings.get_tags()

    def get_filtered_field_names(self):
        return 'customer',

    def get_fields(self):
        fields = super(BaseServiceSerializer, self).get_fields()

        if self.Meta.model is not NotImplemented and 'settings' in fields:
            key = SupportedServices.get_model_key(self.Meta.model)
            fields['settings'].queryset = fields['settings'].queryset.filter(type=key)

        if self.SERVICE_ACCOUNT_FIELDS is not NotImplemented:
            # each service settings could be connected to scope
            self.SERVICE_ACCOUNT_FIELDS['scope'] = 'VM that contains service'
            for field in self.Meta.settings_fields:
                if field not in fields:
                    continue
                if field in self.SERVICE_ACCOUNT_FIELDS:
                    fields[field].help_text = self.SERVICE_ACCOUNT_FIELDS[field]
                else:
                    del fields[field]

        return fields

    def build_unknown_field(self, field_name, model_class):
        if self.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
            if field_name in self.SERVICE_ACCOUNT_EXTRA_FIELDS:
                backend = SupportedServices.get_service_backend(self.Meta.model)
                kwargs = {
                    'write_only': True,
                    'required': False,
                    'allow_blank': True,
                    'help_text': self.SERVICE_ACCOUNT_EXTRA_FIELDS[field_name],
                }
                if hasattr(backend, 'DEFAULTS') and field_name in backend.DEFAULTS:
                    kwargs['help_text'] += ' (default: %s)' % json.dumps(backend.DEFAULTS[field_name])
                    kwargs['initial'] = backend.DEFAULTS[field_name]
                return serializers.CharField, kwargs

        return super(BaseServiceSerializer, self).build_unknown_field(field_name, model_class)

    def validate_empty_values(self, data):
        # required=False is ignored for settings FK, deal with it here
        if 'settings' not in data:
            data['settings'] = None
        return super(BaseServiceSerializer, self).validate_empty_values(data)

    def validate(self, attrs):
        user = self.context['request'].user
        customer = attrs.get('customer') or self.instance.customer
        project = attrs.get('project')
        if project and project.customer != customer:
            raise serializers.ValidationError(
                'Service cannot be connected to project that does not belong to services customer.')

        settings = attrs.get('settings')
        if not user.is_staff:
            if not customer.has_user(user, models.CustomerRole.OWNER):
                raise exceptions.PermissionDenied()
            if not self.instance and settings and not settings.shared:
                if attrs.get('customer') != settings.customer:
                    raise serializers.ValidationError('Customer must match settings customer.')

        if self.context['request'].method == 'POST':
            name = self.initial_data.get('name')
            if not name or not name.strip():
                raise serializers.ValidationError({'name': 'Name cannot be empty'})
            # Make shallow copy to protect from mutations
            settings_fields = self.Meta.settings_fields[:]
            create_settings = any([attrs.get(f) for f in settings_fields])
            if not settings and not create_settings:
                raise serializers.ValidationError(
                    "Either service settings or credentials must be supplied.")

            extra_fields = tuple()
            if self.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
                extra_fields += tuple(self.SERVICE_ACCOUNT_EXTRA_FIELDS.keys())

            if create_settings:
                required = getattr(self.Meta, 'required_fields', tuple())
                for field in settings_fields:
                    if field in required and field not in attrs:
                        error = self.fields[field].error_messages['required']
                        raise serializers.ValidationError({field: unicode(error)})

                args = {f: attrs.get(f) for f in settings_fields if f in attrs}
                if extra_fields:
                    args['options'] = {f: attrs[f] for f in extra_fields if f in attrs}

                name = self.initial_data.get('name')
                if name is None:
                    raise serializers.ValidationError({'name': 'Name field is required.'})

                settings = models.ServiceSettings(
                    type=SupportedServices.get_model_key(self.Meta.model),
                    name=name,
                    customer=customer,
                    **args)

                try:
                    backend = settings.get_backend()
                    backend.ping(raise_exception=True)
                except ServiceBackendError as e:
                    raise serializers.ValidationError("Wrong settings: %s" % e)
                except ServiceBackendNotImplemented:
                    pass

                self._validate_settings(settings)

                settings.save()
                executors.ServiceSettingsCreateExecutor.execute(settings)
                attrs['settings'] = settings

            for f in settings_fields + extra_fields:
                if f in attrs:
                    del attrs[f]

        return attrs

    def _validate_settings(self, settings):
        pass

    def get_resources_count(self, service):
        return self.get_resources_count_map[service.pk]

    @cached_property
    def get_resources_count_map(self):
        resource_models = SupportedServices.get_service_resources(self.Meta.model)
        counts = defaultdict(lambda: 0)
        user = self.context['request'].user
        for model in resource_models:
            service_path = model.Permissions.service_path
            if isinstance(self.instance, list):
                query = {service_path + '__in': self.instance}
            else:
                query = {service_path: self.instance}
            queryset = filter_queryset_for_user(model.objects.all(), user)
            rows = queryset.filter(**query).values(service_path)\
                .annotate(count=django_models.Count('id'))
            for row in rows:
                service_id = row[service_path]
                counts[service_id] += row['count']
        return counts

    def get_service_type(self, obj):
        return SupportedServices.get_name_for_model(obj)

    def get_state(self, obj):
        return obj.settings.get_state_display()

    def create(self, attrs):
        project = attrs.pop('project', None)
        service = super(BaseServiceSerializer, self).create(attrs)
        spl_model = service.projects.through
        if project and not spl_model.objects.filter(project=project, service=service).exists():
            spl_model.objects.create(project=project, service=service)
        return service


class BaseServiceProjectLinkSerializer(PermissionFieldFilteringMixin,
                                       core_serializers.AugmentedSerializerMixin,
                                       serializers.HyperlinkedModelSerializer):
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid')

    state = MappedChoiceField(
        choices=[(v, k) for k, v in core_models.SynchronizationStates.CHOICES],
        choice_mappings={v: k for k, v in core_models.SynchronizationStates.CHOICES},
        read_only=True)

    service_name = serializers.ReadOnlyField(source='service.settings.name')

    class Meta(object):
        model = NotImplemented
        fields = (
            'url',
            'project', 'project_name', 'project_uuid',
            'service', 'service_uuid', 'service_name',
        )
        related_paths = ('project', 'service')
        extra_kwargs = {
            'service': {'lookup_field': 'uuid', 'view_name': NotImplemented},
        }

    def get_filtered_field_names(self):
        return 'project', 'service'

    def validate(self, attrs):
        if attrs['service'].customer != attrs['project'].customer:
            raise serializers.ValidationError("Service customer doesn't match project customer")

        # XXX: Consider adding unique key (service, project) to the model instead
        if self.Meta.model.objects.filter(service=attrs['service'], project=attrs['project']).exists():
            raise serializers.ValidationError("This service project link already exists")

        return attrs


class ResourceSerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported resource via serializers definition.
        See SupportedServices for details.
    """
    def __new__(cls, name, bases, args):
        serializer = super(ResourceSerializerMetaclass, cls).__new__(cls, name, bases, args)
        SupportedServices.register_resource_serializer(args['Meta'].model, serializer)
        return serializer


class BasicResourceSerializer(serializers.Serializer):
    uuid = serializers.ReadOnlyField()
    name = serializers.ReadOnlyField()
    resource_type = serializers.SerializerMethodField()

    def get_resource_type(self, resource):
        return SupportedServices.get_name_for_model(resource)


class ManagedResourceSerializer(BasicResourceSerializer):
    project_name = serializers.ReadOnlyField(source='service_project_link.project.name')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')

    customer_uuid = serializers.ReadOnlyField(source='service_project_link.project.customer.uuid')
    customer_name = serializers.ReadOnlyField(source='service_project_link.project.customer.name')


class BaseResourceSerializer(six.with_metaclass(ResourceSerializerMetaclass,
                             core_serializers.RestrictedSerializerMixin,
                             MonitoringSerializerMixin,
                             PermissionFieldFilteringMixin,
                             core_serializers.AugmentedSerializerMixin,
                             serializers.HyperlinkedModelSerializer)):

    state = serializers.ReadOnlyField(source='get_state_display')

    project = serializers.HyperlinkedRelatedField(
        source='service_project_link.project',
        view_name='project-detail',
        read_only=True,
        lookup_field='uuid')

    project_name = serializers.ReadOnlyField(source='service_project_link.project.name')
    project_uuid = serializers.ReadOnlyField(source='service_project_link.project.uuid')

    service_project_link = serializers.HyperlinkedRelatedField(
        view_name=NotImplemented,
        queryset=NotImplemented)

    service = serializers.HyperlinkedRelatedField(
        source='service_project_link.service',
        view_name=NotImplemented,
        read_only=True,
        lookup_field='uuid')

    service_name = serializers.ReadOnlyField(source='service_project_link.service.settings.name')
    service_uuid = serializers.ReadOnlyField(source='service_project_link.service.uuid')

    service_settings = serializers.HyperlinkedRelatedField(
        source='service_project_link.service.settings',
        view_name='servicesettings-detail',
        read_only=True,
        lookup_field='uuid')
    service_settings_uuid = serializers.ReadOnlyField(source='service_project_link.service.settings.uuid')
    service_settings_state = serializers.ReadOnlyField(
        source='service_project_link.service.settings.human_readable_state')
    service_settings_error_message = serializers.ReadOnlyField(
        source='service_project_link.service.settings.error_message')

    customer = serializers.HyperlinkedRelatedField(
        source='service_project_link.project.customer',
        view_name='customer-detail',
        read_only=True,
        lookup_field='uuid')

    customer_name = serializers.ReadOnlyField(source='service_project_link.project.customer.name')
    customer_abbreviation = serializers.ReadOnlyField(source='service_project_link.project.customer.abbreviation')
    customer_native_name = serializers.ReadOnlyField(source='service_project_link.project.customer.native_name')

    created = serializers.DateTimeField(read_only=True)
    resource_type = serializers.SerializerMethodField()

    tags = serializers.ReadOnlyField(source='get_tags')
    access_url = serializers.SerializerMethodField()
    is_link_valid = serializers.BooleanField(
        source='service_project_link.is_valid',
        read_only=True,
        help_text='True if resource is originated from a service that satisfies an associated project requirements.')

    class Meta(object):
        model = NotImplemented
        fields = MonitoringSerializerMixin.Meta.fields + (
            'url', 'uuid', 'name', 'description', 'start_time',
            'service', 'service_name', 'service_uuid',
            'service_settings', 'service_settings_uuid',
            'service_settings_state', 'service_settings_error_message',
            'project', 'project_name', 'project_uuid',
            'customer', 'customer_name', 'customer_native_name', 'customer_abbreviation',
            'tags', 'error_message',
            'resource_type', 'state', 'created', 'service_project_link', 'backend_id',
            'access_url', 'is_link_valid',
        )
        protected_fields = ('service', 'service_project_link')
        read_only_fields = ('start_time', 'error_message', 'backend_id')
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'service_project_link',

    def get_resource_type(self, obj):
        return SupportedServices.get_name_for_model(obj)

    def get_resource_fields(self):
        return self.Meta.model._meta.get_all_field_names()

    # an optional generic URL for accessing a resource
    def get_access_url(self, obj):
        return obj.get_access_url()

    @staticmethod
    def eager_load(queryset):
        return (
            queryset
            .select_related(
                'service_project_link',
                'service_project_link__service',
                'service_project_link__service__settings',
                'service_project_link__project',
                'service_project_link__project__customer',
            ).prefetch_related('service_project_link__service__settings__certifications',
                               'service_project_link__project__certifications')
        )

    def validate_service_project_link(self, service_project_link):
        if not service_project_link.is_valid:
            raise serializers.ValidationError(service_project_link.validation_message)

        return service_project_link

    @transaction.atomic
    def create(self, validated_data):
        data = validated_data.copy()
        fields = self.get_resource_fields()
        # Remove `virtual` properties which ain't actually belong to the model
        for prop in data.keys():
            if prop not in fields:
                del data[prop]

        resource = super(BaseResourceSerializer, self).create(data)
        resource.increase_backend_quotas_usage()
        return resource


class PublishableResourceSerializer(BaseResourceSerializer):
    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + ('publishing_state',)
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + ('publishing_state',)


class SummaryResourceSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        return SupportedServices.get_resource_serializer(model)


class SummaryServiceSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        return SupportedServices.get_service_serializer(model)


class BaseResourceImportSerializer(PermissionFieldFilteringMixin,
                                   core_serializers.AugmentedSerializerMixin,
                                   serializers.HyperlinkedModelSerializer):
    backend_id = serializers.CharField(write_only=True)
    project = serializers.HyperlinkedRelatedField(
        queryset=models.Project.objects.all(),
        view_name='project-detail',
        lookup_field='uuid',
        write_only=True)

    state = serializers.ReadOnlyField(source='get_state_display')
    created = serializers.DateTimeField(read_only=True)
    import_history = serializers.BooleanField(
        default=True, write_only=True, help_text='Import historical resource usage')

    class Meta(object):
        model = NotImplemented
        fields = (
            'url', 'uuid', 'name', 'state', 'created',
            'backend_id', 'project', 'import_history'
        )
        read_only_fields = ('name',)
        extra_kwargs = {
            'url': {'lookup_field': 'uuid'},
        }

    def get_filtered_field_names(self):
        return 'project',

    def get_fields(self):
        fields = super(BaseResourceImportSerializer, self).get_fields()
        # Context doesn't have service during schema generation
        if 'service' in self.context:
            fields['project'].queryset = self.context['service'].projects.all()

        return fields

    def validate(self, attrs):
        if self.Meta.model.objects.filter(backend_id=attrs['backend_id']).exists():
            raise serializers.ValidationError(
                {'backend_id': "This resource is already linked to NodeConductor"})

        spl_class = SupportedServices.get_related_models(self.Meta.model)['service_project_link']
        spl = spl_class.objects.get(service=self.context['service'], project=attrs['project'])
        attrs['service_project_link'] = spl

        return attrs

    def create(self, validated_data):
        validated_data.pop('project')
        return super(BaseResourceImportSerializer, self).create(validated_data)


class VirtualMachineSerializer(BaseResourceSerializer):
    external_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
    )
    internal_ips = serializers.ListField(
        child=serializers.IPAddressField(protocol='ipv4'),
        read_only=True,
    )

    ssh_public_key = serializers.HyperlinkedRelatedField(
        view_name='sshpublickey-detail',
        lookup_field='uuid',
        queryset=core_models.SshPublicKey.objects.all(),
        required=False,
        write_only=True)

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + (
            'cores', 'ram', 'disk', 'min_ram', 'min_disk',
            'ssh_public_key', 'user_data', 'external_ips', 'internal_ips',
            'latitude', 'longitude', 'key_name', 'key_fingerprint', 'image_name'
        )
        read_only_fields = BaseResourceSerializer.Meta.read_only_fields + (
            'cores', 'ram', 'disk', 'min_ram', 'min_disk',
            'external_ips', 'internal_ips',
            'latitude', 'longitude', 'key_name', 'key_fingerprint', 'image_name'
        )
        protected_fields = BaseResourceSerializer.Meta.protected_fields + (
            'user_data', 'ssh_public_key'
        )

    def get_fields(self):
        fields = super(VirtualMachineSerializer, self).get_fields()
        if 'request' in self.context:
            user = self.context['request'].user
            ssh_public_key = fields.get('ssh_public_key')
            if ssh_public_key:
                ssh_public_key.query_params = {'user_uuid': user.uuid.hex}
                ssh_public_key.queryset = ssh_public_key.queryset.filter(user=user)
        return fields

    def create(self, validated_data):
        validated_data['image_name'] = validated_data['image'].name
        return super(VirtualMachineSerializer, self).create(validated_data)


class PropertySerializerMetaclass(serializers.SerializerMetaclass):
    """ Build a list of supported properties via serializers definition.
        See SupportedServices for details.
    """

    def __new__(cls, name, bases, args):
        SupportedServices.register_property(args['Meta'].model)
        return super(PropertySerializerMetaclass, cls).__new__(cls, name, bases, args)


class BasePropertySerializer(six.with_metaclass(PropertySerializerMetaclass,
                                                core_serializers.AugmentedSerializerMixin,
                                                serializers.HyperlinkedModelSerializer)):
    class Meta(object):
        model = NotImplemented


class AggregateSerializer(serializers.Serializer):
    MODEL_NAME_CHOICES = (
        ('project', 'project'),
        ('customer', 'customer'),
    )
    MODEL_CLASSES = {
        'project': models.Project,
        'customer': models.Customer,
    }

    aggregate = serializers.ChoiceField(choices=MODEL_NAME_CHOICES, default='customer')
    uuid = serializers.CharField(allow_null=True, default=None)

    def get_aggregates(self, user):
        model = self.MODEL_CLASSES[self.data['aggregate']]
        queryset = filter_queryset_for_user(model.objects.all(), user)

        if 'uuid' in self.data and self.data['uuid']:
            queryset = queryset.filter(uuid=self.data['uuid'])
        return queryset

    def get_projects(self, user):
        queryset = self.get_aggregates(user)

        if self.data['aggregate'] == 'project':
            return queryset.all()
        else:
            queryset = models.Project.objects.filter(customer__in=list(queryset))
            return filter_queryset_for_user(queryset, user)

    def get_service_project_links(self, user):
        projects = self.get_projects(user)
        return [model.objects.filter(project__in=projects)
                for model in ServiceProjectLink.get_all_models()]


class PrivateCloudSerializer(BaseResourceSerializer):
    extra_configuration = core_serializers.JSONField(read_only=True)

    class Meta(BaseResourceSerializer.Meta):
        fields = BaseResourceSerializer.Meta.fields + ('extra_configuration',)
