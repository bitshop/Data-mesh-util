import boto3
import os
import sys
import logging

sys.path.append(os.path.join(os.path.dirname(__file__), "resource"))
sys.path.append(os.path.join(os.path.dirname(__file__), "lib"))
from data_mesh_util.lib.constants import *
import data_mesh_util.lib.utils as utils
from data_mesh_util.lib.SubscriberTracker import SubscriberTracker


class DataMeshAdmin:
    _region = None
    _data_mesh_account_id = None
    _data_producer_account_id = None
    _data_consumer_account_id = None
    _data_mesh_manager_role_arn = None
    _iam_client = None
    _lf_client = None
    _sts_client = None
    _dynamo_client = None
    _dynamo_resource = None
    _config = {}
    _logger = logging.getLogger("DataMeshAdmin")
    stream_handler = logging.StreamHandler(sys.stdout)
    _logger.addHandler(stream_handler)
    _subscriber_tracker = None

    def __init__(self, region_name: str = 'us-east-1', log_level: str = "INFO"):
        self._iam_client = boto3.client('iam')
        self._sts_client = boto3.client('sts')
        self._dynamo_client = boto3.client('dynamodb')
        self._dynamo_resource = boto3.resource('dynamodb')
        self._lf_client = boto3.client('lakeformation')

        # get the region for the module
        if 'AWS_REGION' in os.environ:
            self._region = os.environ.get('AWS_REGION')
        else:
            if region_name is None:
                raise Exception("Cannot initialize a Data Mesh without an AWS Region")
            else:
                self._region = region_name

        self._subscriber_tracker = SubscriberTracker(dynamo_client=self._dynamo_client,
                                                     dynamo_resource=self._dynamo_resource)
        self._logger.setLevel(log_level)

    def _create_template_config(self, config: dict):
        if config is None:
            config = {}

        # add the data mesh account to the config if it isn't provided
        if "data_mesh_account_id" not in config:
            config["data_mesh_account_id"] = self._data_mesh_account_id

        if "producer_account_id" not in config:
            config["producer_account_id"] = self._data_producer_account_id

        if "consumer_account_id" not in config:
            config["consumer_account_id"] = self._data_consumer_account_id

        self._logger.debug(self._config)

    def _create_data_mesh_manager_role(self):
        '''
        Private method to create objects needed for an administrative role that can be used to grant access to Data Mesh roles
        :return:
        '''
        self._create_template_config(self._config)

        current_identity = self._sts_client.get_caller_identity()
        self._logger.debug("Running as %s" % str(current_identity))

        mgr_tuple = utils.configure_iam(
            iam_client=self._iam_client,
            policy_name='DataMeshManagerPolicy',
            policy_desc='IAM Policy to bootstrap the Data Mesh Admin',
            policy_template="data_mesh_setup_iam_policy.pystache",
            role_name=DATA_MESH_MANAGER_ROLENAME,
            role_desc='Role to be used for the Data Mesh Manager function',
            account_id=self._data_mesh_account_id,
            config=self._config)
        data_mesh_mgr_role_arn = mgr_tuple[0]

        self._logger.info("Validated Data Mesh Manager Role %s" % data_mesh_mgr_role_arn)

        # remove default IAM settings in lakeformation for the account, and setup the manager role and this caller as admins
        response = self._lf_client.put_data_lake_settings(
            DataLakeSettings={
                "DataLakeAdmins": [
                    {"DataLakePrincipalIdentifier": data_mesh_mgr_role_arn},
                    # add the current caller identity as an admin
                    {"DataLakePrincipalIdentifier": current_identity.get('Arn')}
                ],
                'CreateTableDefaultPermissions': []
            }
        )
        self._logger.info(
            "Removed default data lake settings for Account %s. New Admins are %s and Data Mesh Manager" % (
                current_identity.get('Account'), current_identity.get('Arn')))

        return mgr_tuple

    def _create_producer_role(self):
        '''
        Private method to create objects needed for a Producer account to connect to the Data Mesh and create data products
        :return:
        '''
        self._create_template_config(self._config)

        # create the policy and role to be used for data producers
        producer_tuple = utils.configure_iam(
            iam_client=self._iam_client,
            policy_name='DataMeshProducerPolicy',
            policy_desc='IAM Role enabling Accounts to become Data Producers',
            policy_template="producer_policy.pystache",
            role_name=DATA_MESH_ADMIN_PRODUCER_ROLENAME,
            role_desc='Role to be used for all Data Mesh Producer Accounts',
            account_id=self._data_mesh_account_id,
            config=self._config)
        producer_iam_role_arn = producer_tuple[0]

        self._logger.info("Validated Data Mesh Producer Role %s" % producer_iam_role_arn)

        # grant this role the ability to create databases and tables
        response = self._lf_client.grant_permissions(
            Principal={
                'DataLakePrincipalIdentifier': producer_iam_role_arn
            },
            Resource={'Catalog': {}},
            Permissions=[
                'CREATE_DATABASE'
            ],
            PermissionsWithGrantOption=[
                'CREATE_DATABASE'
            ]
        )
        self._logger.info("Granted Data Mesh Producer CREATE_DATABASE privileges on Catalog")

        return producer_tuple

    def _create_consumer_role(self):
        '''
        Private method to create objects needed for a Consumer account to connect to the Data Mesh and mirror data products into their account
        :return:
        '''
        self._create_template_config(self._config)

        return utils.configure_iam(
            iam_client=self._iam_client,
            policy_name='DataMeshConsumerPolicy',
            policy_desc='IAM Role enabling Accounts to become Data Consumers',
            policy_template="consumer_policy.pystache",
            role_name=DATA_MESH_ADMIN_CONSUMER_ROLENAME,
            role_desc='Role to be used for all Data Mesh Consumer Accounts',
            account_id=self._data_mesh_account_id,
            config=self._config)

    def _api_tuple(self, item_tuple: tuple):
        return {
            "RoleArn": item_tuple[0],
            "UserArn": item_tuple[1],
            "GroupArn": item_tuple[2]
        }

    def initialize_mesh_account(self):
        '''
        Sets up an AWS Account to act as a Data Mesh central account. This method should be invoked by an Administrator
        of the Data Mesh Account. Creates IAM Roles & Policies for the DataMeshManager, DataProducer, and DataConsumer
        :return:
        '''
        self._data_mesh_account_id = self._sts_client.get_caller_identity().get('Account')

        # create a new IAM role in the Data Mesh Account to be used for future grants
        mgr_tuple = self._create_data_mesh_manager_role()

        # create the producer role
        producer_tuple = self._create_producer_role()

        # create the consumer role
        consumer_tuple = self._create_consumer_role()

        return {
            "Manager": self._api_tuple(mgr_tuple),
            "ProducerAdmin": self._api_tuple(producer_tuple),
            "ConsumerAdmin": self._api_tuple(consumer_tuple),
            "SubscriptionTracker": self._subscriber_tracker.get_endpoints()
        }

    def enable_account_as_producer(self, account_id: str):
        '''
        Enables a remote account to act as a data producer by granting them access to the DataMeshAdminProducer Role
        :return:
        '''
        if utils.validate_correct_account(self._iam_client, DATA_MESH_ADMIN_PRODUCER_ROLENAME) is False:
            raise Exception("Must be run in the Data Mesh Account")

        # create trust relationships for the AdminProducer roles
        utils.add_aws_trust_to_role(iam_client=self._iam_client, account_id=account_id,
                                    role_name=DATA_MESH_ADMIN_PRODUCER_ROLENAME)
        self._logger.info("Enabled Account %s to assume %s" % (account_id, DATA_MESH_ADMIN_PRODUCER_ROLENAME))

    def enable_account_as_consumer(self, account_id: str):
        '''
        Enables a remote account to act as a data producer by granting them access to the DataMeshAdminProducer Role
        :return:
        '''
        if utils.validate_correct_account(self._iam_client, DATA_MESH_ADMIN_PRODUCER_ROLENAME) is False:
            raise Exception("Must be run in the Data Mesh Account")

        # create trust relationships for the AdminProducer roles
        utils.add_aws_trust_to_role(iam_client=self._iam_client, account_id=account_id,
                                    role_name=DATA_MESH_ADMIN_CONSUMER_ROLENAME)
        self._logger.info("Enabled Account %s to assume %s" % (account_id, DATA_MESH_ADMIN_CONSUMER_ROLENAME))

    def list_data_access(self, database_name: str = None, table_name: str = None, granted_principal: str = None,
                         grant_date_start: str = None, grant_date_end: str = None):
        '''
        API which returns data accesses granted or pending, based upon the supplied filters
        :return:
        '''
        pass
