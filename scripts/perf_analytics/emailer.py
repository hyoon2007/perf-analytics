from __future__ import annotations

import boto3
from botocore.exceptions import ClientError


def _normalize_to_addresses(to_email: str | list[str]) -> list[str]:
    if isinstance(to_email, str):
        # Allow comma-separated recipients in config: a@x.com,b@y.com
        return [addr.strip() for addr in to_email.split(",") if addr.strip()]
    return [addr.strip() for addr in to_email if addr and addr.strip()]


def send_email(
    aws_access_key_id: str,
    aws_secret_access_key: str,
    ses_region: str,
    from_email: str,
    to_email: str | list[str],
    subject: str,
    text_body: str,
    html_body: str,
) -> str:
    if not aws_access_key_id or not aws_secret_access_key:
        raise ValueError("AWS credentials are missing.")
    to_addresses = _normalize_to_addresses(to_email)

    if not from_email or not to_addresses:
        raise ValueError("SES from/to email is missing.")

    client = boto3.client(
        "sesv2",
        region_name=ses_region,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
    )

    try:
        response = client.send_email(
            FromEmailAddress=from_email,
            Destination={"ToAddresses": to_addresses},
            Content={
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": text_body, "Charset": "UTF-8"},
                        "Html": {"Data": html_body, "Charset": "UTF-8"},
                    },
                }
            },
        )
    except ClientError as exc:
        raise RuntimeError(exc.response["Error"]["Message"]) from exc

    return response["MessageId"]
