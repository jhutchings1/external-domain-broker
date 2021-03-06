import json
import logging
import re
import time
from datetime import datetime, timedelta

import josepy
import OpenSSL
from acme import challenges, client, crypto_util, messages
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from broker.extensions import config
from broker.models import ACMEUser, Challenge, Operation
from broker.tasks import huey
from broker.tasks.db_injection import inject_db

logger = logging.getLogger(__name__)


class DNSChallengeNotFound(RuntimeError):
    def __init__(self, domain, obj):
        super().__init__(f"Cannot find DNS challenges for {domain} in {obj}")


class ChallengeNotFound(RuntimeError):
    def __init__(self, domain, obj):
        super().__init__(f"Cannot find any challenges for {domain} in {obj}")


def dns_challenge(order, domain):
    """Extract authorization resource from within order resource."""

    # authorization.body.challenges is a set of ChallengeBody
    # objects.
    challenges_for_domain = [
        authorization.body.challenges
        for authorization in order.authorizations
        if authorization.body.identifier.value == domain
    ][0]

    if not challenges_for_domain:
        raise ChallengeNotFound(domain, order.authorizations)

    for challenge in challenges_for_domain:
        if isinstance(challenge.chall, challenges.DNS01):
            return challenge

    raise DNSChallengeNotFound(domain, challenges_for_domain)


@huey.retriable_task
@inject_db
def create_user(operation_id: int, **kwargs):
    db = kwargs["db"]
    acme_user = ACMEUser()
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    service_instance.acme_user = acme_user
    key = josepy.JWKRSA(
        key=rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )
    )
    private_key_pem_in_binary = key.key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    acme_user.private_key_pem = private_key_pem_in_binary.decode("utf-8")

    net = client.ClientNetwork(key, user_agent="cloud.gov external domain broker")
    directory = messages.Directory.from_json(net.get(config.ACME_DIRECTORY).json())
    client_acme = client.ClientV2(directory, net=net)

    acme_user.email = "cloud-gov-operations@gsa.gov"
    registration = client_acme.new_account(
        messages.NewRegistration.from_data(
            email=acme_user.email, terms_of_service_agreed=True
        )
    )
    acme_user.registration_json = registration.json_dumps()
    acme_user.uri = registration.uri
    db.session.add(operation)
    db.session.add(service_instance)
    db.session.add(acme_user)
    db.session.commit()


@huey.nonretriable_task
@inject_db
def generate_private_key(operation_id: int, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance

    # Create private key.
    private_key = OpenSSL.crypto.PKey()
    private_key.generate_key(OpenSSL.crypto.TYPE_RSA, 2048)

    # Get the PEM
    private_key_pem_in_binary = OpenSSL.crypto.dump_privatekey(
        OpenSSL.crypto.FILETYPE_PEM, private_key
    )

    # Get the CSR for the domains
    csr_pem_in_binary = crypto_util.make_csr(
        private_key_pem_in_binary, service_instance.domain_names
    )

    # Store them as text for later
    service_instance.private_key_pem = private_key_pem_in_binary.decode("utf-8")
    service_instance.csr_pem = csr_pem_in_binary.decode("utf-8")

    db.session.add(service_instance)
    db.session.add(operation)
    db.session.commit()


@huey.retriable_task
@inject_db
def initiate_challenges(operation_id: int, **kwargs):
    db = kwargs["db"]
    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    acme_user = service_instance.acme_user

    account_key = serialization.load_pem_private_key(
        acme_user.private_key_pem.encode(), password=None, backend=default_backend()
    )
    wrapped_account_key = josepy.JWKRSA(key=account_key)

    registration = json.loads(acme_user.registration_json)
    net = client.ClientNetwork(
        wrapped_account_key,
        user_agent="cloud.gov external domain broker",
        account=registration,
    )
    directory = messages.Directory.from_json(net.get(config.ACME_DIRECTORY).json())
    client_acme = client.ClientV2(directory, net=net)

    order = client_acme.new_order(service_instance.csr_pem.encode())
    service_instance.order_json = json.dumps(order.to_json())

    for domain in service_instance.domain_names:
        challenge_body = dns_challenge(order, domain)
        (
            challenge_response,
            challenge_validation_contents,
        ) = challenge_body.response_and_validation(wrapped_account_key)

        challenge = Challenge()
        challenge.body_json = challenge_body.json_dumps()

        challenge.domain = domain
        challenge.service_instance = service_instance
        challenge.validation_domain = challenge_body.validation_domain_name(domain)
        challenge.validation_contents = challenge_validation_contents
        db.session.add(challenge)

    db.session.commit()


@huey.retriable_task
@inject_db
def answer_challenges(operation_id: int, **kwargs):
    db = kwargs["db"]

    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    acme_user = service_instance.acme_user

    time.sleep(int(config.DNS_PROPAGATION_SLEEP_TIME))

    account_key = serialization.load_pem_private_key(
        acme_user.private_key_pem.encode(), password=None, backend=default_backend()
    )
    wrapped_account_key = josepy.JWKRSA(key=account_key)

    registration = json.loads(acme_user.registration_json)
    net = client.ClientNetwork(
        wrapped_account_key,
        user_agent="cloud.gov external domain broker",
        account=registration,
    )
    directory = messages.Directory.from_json(net.get(config.ACME_DIRECTORY).json())
    client_acme = client.ClientV2(directory, net=net)

    for challenge in service_instance.challenges:
        challenge_body = messages.ChallengeBody.from_json(
            json.loads(challenge.body_json)
        )
        challenge_response = challenge_body.response(wrapped_account_key)
        # Let the CA server know that we are ready for the challenge.
        client_acme.answer_challenge(challenge_body, challenge_response)
        challenge.answered = True
        db.session.add(challenge)
        db.session.commit()


@huey.retriable_task
@inject_db
def retrieve_certificate(operation_id: int, **kwargs):
    db = kwargs["db"]

    def cert_from_fullchain(fullchain_pem: str) -> str:
        """extract cert_pem from fullchain_pem

        Reference https://github.com/certbot/certbot/blob/b42e24178aaa3f1ad1323acb6a3a9c63e547893f/certbot/certbot/crypto_util.py#L482-L518
        """
        cert_pem_regex = re.compile(
            b"-----BEGIN CERTIFICATE-----\r?.+?\r?-----END CERTIFICATE-----\r?",
            re.DOTALL,  # DOTALL (/s) because the base64text may include newlines
        )

        certs = cert_pem_regex.findall(fullchain_pem.encode())
        if len(certs) < 2:
            raise RuntimeError(
                "failed to extract cert from fullchain: less than 2 certificates in chain"
            )

        certs_normalized = [
            OpenSSL.crypto.dump_certificate(
                OpenSSL.crypto.FILETYPE_PEM,
                OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert),
            ).decode()
            for cert in certs
        ]

        return certs_normalized[0], "".join(certs_normalized[1:])

    operation = Operation.query.get(operation_id)
    service_instance = operation.service_instance
    acme_user = service_instance.acme_user

    account_key = serialization.load_pem_private_key(
        acme_user.private_key_pem.encode(), password=None, backend=default_backend()
    )
    wrapped_account_key = josepy.JWKRSA(key=account_key)

    registration = json.loads(acme_user.registration_json)
    net = client.ClientNetwork(
        wrapped_account_key,
        user_agent="cloud.gov external domain broker",
        account=registration,
    )
    directory = messages.Directory.from_json(net.get(config.ACME_DIRECTORY).json())
    client_acme = client.ClientV2(directory, net=net)

    order_json = json.loads(service_instance.order_json)
    # The csr_pem in the JSON is a binary string, but finalize_order() expects
    # utf-8?  So we set it here from our saved copy.
    order_json["csr_pem"] = service_instance.csr_pem
    order = messages.OrderResource.from_json(order_json)

    deadline = datetime.now() + timedelta(seconds=config.ACME_POLL_TIMEOUT_IN_SECONDS)
    finalized_order = client_acme.poll_and_finalize(orderr=order, deadline=deadline)

    service_instance.cert_pem, service_instance.fullchain_pem = cert_from_fullchain(
        finalized_order.fullchain_pem
    )
    db.session.add(service_instance)
    db.session.commit()
