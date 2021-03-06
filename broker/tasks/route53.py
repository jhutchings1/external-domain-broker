import logging

from sqlalchemy.orm.attributes import flag_modified

from broker.aws import route53
from broker.extensions import config
from broker.models import Operation
from broker.tasks import huey
from broker.tasks.db_injection import inject_db

logger = logging.getLogger(__name__)


@huey.retriable_task
@inject_db
def create_TXT_records(operation_id: int, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance

    for challenge in service_instance.challenges:
        domain = challenge.validation_domain
        txt_record = f"{domain}.{config.DNS_ROOT_DOMAIN}"
        contents = challenge.validation_contents
        logger.info(f'Creating TXT record {txt_record} with contents "{contents}"')
        route53_response = route53.change_resource_record_sets(
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "CREATE",
                        "ResourceRecordSet": {
                            "Type": "TXT",
                            "Name": txt_record,
                            "ResourceRecords": [{"Value": f'"{contents}"'}],
                            "TTL": 60,
                        },
                    }
                ]
            },
            HostedZoneId=config.ROUTE53_ZONE_ID,
        )
        change_id = route53_response["ChangeInfo"]["Id"]
        logger.info(f"Saving Route53 TXT change ID: {change_id}")
        service_instance.route53_change_ids.append(change_id)
        flag_modified(service_instance, "route53_change_ids")
        db.session.add(service_instance)
        db.session.commit()


@huey.nonretriable_task
@inject_db
def remove_TXT_records(operation_id: int, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance

    for challenge in service_instance.challenges:
        domain = challenge.validation_domain
        txt_record = f"{domain}.{config.DNS_ROOT_DOMAIN}"
        contents = challenge.validation_contents
        logger.info(f'Removing TXT record {txt_record} with contents "{contents}"')
        route53_response = route53.change_resource_record_sets(
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "DELETE",
                        "ResourceRecordSet": {
                            "Type": "TXT",
                            "Name": txt_record,
                            "ResourceRecords": [{"Value": f'"{contents}"'}],
                            "TTL": 60,
                        },
                    }
                ]
            },
            HostedZoneId=config.ROUTE53_ZONE_ID,
        )
        change_id = route53_response["ChangeInfo"]["Id"]
        logger.info(f"Ignoring Route53 TXT change ID: {change_id}")


@huey.retriable_task
@inject_db
def wait_for_changes(operation_id: int, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance

    change_ids = service_instance.route53_change_ids.copy()
    logger.info(f"Waiting for {len(change_ids)} Route53 change IDs: {change_ids}")
    for change_id in change_ids:
        logger.info(f"Waiting for: {change_id}")
        waiter = route53.get_waiter("resource_record_sets_changed")
        waiter.wait(
            Id=change_id,
            WaiterConfig={
                "Delay": config.AWS_POLL_WAIT_TIME_IN_SECONDS,
                "MaxAttempts": config.AWS_POLL_MAX_ATTEMPTS,
            },
        )
        service_instance.route53_change_ids.remove(change_id)
        flag_modified(service_instance, "route53_change_ids")
        db.session.add(service_instance)
        db.session.commit()


@huey.retriable_task
@inject_db
def create_ALIAS_records(operation_id: str, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    logger.info(f"Creating ALIAS records for {service_instance.domain_names}")

    for domain in service_instance.domain_names:
        alias_record = f"{domain}.{config.DNS_ROOT_DOMAIN}"
        target = service_instance.cloudfront_distribution_url
        logger.info(f'Creating ALIAS record {alias_record} pointing to "{target}"')
        route53_response = route53.change_resource_record_sets(
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "CREATE",
                        "ResourceRecordSet": {
                            "Type": "A",
                            "Name": alias_record,
                            "AliasTarget": {
                                "DNSName": target,
                                "HostedZoneId": "Z2FDTNDATAQYW2",
                                "EvaluateTargetHealth": False,
                            },
                        },
                    }
                ]
            },
            HostedZoneId=config.ROUTE53_ZONE_ID,
        )
        change_id = route53_response["ChangeInfo"]["Id"]
        logger.info(f"Saving Route53 ALIAS change ID: {change_id}")
        service_instance.route53_change_ids.append(change_id)
        flag_modified(service_instance, "route53_change_ids")
        db.session.add(service_instance)
        db.session.commit()


@huey.nonretriable_task
@inject_db
def remove_ALIAS_records(operation_id: str, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    logger.info(f"Removing ALIAS records for {service_instance.domain_names}")

    for domain in service_instance.domain_names:
        alias_record = f"{domain}.{config.DNS_ROOT_DOMAIN}"
        target = service_instance.cloudfront_distribution_url
        logger.info(f'Removing ALIAS record {alias_record} pointing to "{target}"')
        route53_response = route53.change_resource_record_sets(
            ChangeBatch={
                "Changes": [
                    {
                        "Action": "DELETE",
                        "ResourceRecordSet": {
                            "Type": "A",
                            "Name": alias_record,
                            "AliasTarget": {
                                "DNSName": target,
                                "HostedZoneId": "Z2FDTNDATAQYW2",
                                "EvaluateTargetHealth": False,
                            },
                        },
                    }
                ]
            },
            HostedZoneId=config.ROUTE53_ZONE_ID,
        )
        change_id = route53_response["ChangeInfo"]["Id"]
        logger.info(f"Not tracking change ID: {change_id}")
