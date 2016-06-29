import mock
from rest_framework import test

from nodeconductor.structure import models as structure_models
from nodeconductor.structure.tests import factories as structure_factories

from . import factories
from .. import utils


class BaseEventsApiTest(test.APITransactionTestCase):
    def setUp(self):
        self.settings_patcher = self.settings(NODECONDUCTOR={
            'ELASTICSEARCH': {
                'username': 'username',
                'password': 'password',
                'host': 'example.com',
                'port': '9999',
                'protocol': 'https',
            }
        })
        self.settings_patcher.enable()

        self.es_patcher = mock.patch('nodeconductor.logging.elasticsearch_client.Elasticsearch')
        self.mocked_es = self.es_patcher.start()

    def tearDown(self):
        self.settings_patcher.disable()
        self.es_patcher.stop()


class ScopeTypeTest(BaseEventsApiTest):
    def setUp(self):
        super(ScopeTypeTest, self).setUp()
        self.mocked_es().search.return_value = {'hits': {'total': 0, 'hits': []}}

    @property
    def must_terms(self):
        call_args = self.mocked_es().search.call_args[-1]
        return call_args['body']['query']['filtered']['filter']['bool']['must'][-1]['terms']

    def get_customer_events(self):
        url = factories.EventFactory.get_list_url()
        scope_type = utils.get_reverse_scope_types_mapping()[structure_models.Customer]
        return self.client.get(url, {'scope_type': scope_type})

    def test_staff_can_see_any_customers_events(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)
        customer = structure_factories.CustomerFactory()

        self.get_customer_events()
        self.assertEqual(self.must_terms, {'customer_uuid': [customer.uuid.hex]})

    def test_owner_can_see_only_customer_events(self):
        structure_factories.CustomerFactory()

        customer = structure_factories.CustomerFactory()
        owner = structure_factories.UserFactory()
        customer.add_user(owner, structure_models.CustomerRole.OWNER)

        self.client.force_authenticate(user=owner)
        self.get_customer_events()
        self.assertEqual(self.must_terms, {'customer_uuid': [customer.uuid.hex]})
