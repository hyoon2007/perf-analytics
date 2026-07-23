#!/usr/bin/env python3
"""
AWS SES로 이메일 발송 예제
"""

import os
import boto3
from botocore.exceptions import ClientError

# SES 클라이언트 생성
# Credentials는 환경변수(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)로 관리하세요.
# 예: export AWS_ACCESS_KEY_ID=... && export AWS_SECRET_ACCESS_KEY=...
ses_client = boto3.client(
    'sesv2',
    region_name=os.getenv('AWS_SES_REGION', 'ap-northeast-1'),
    aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
    aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY'),
)

def send_simple_email(from_email, to_email, subject, body_text):
    """
    간단한 텍스트 이메일 발송
    """
    try:
        response = ses_client.send_email(
            FromEmailAddress=from_email,
            Destination={
                'ToAddresses': [to_email]
            },
            Content={
                'Simple': {
                    'Subject': {
                        'Data': subject,
                        'Charset': 'UTF-8'
                    },
                    'Body': {
                        'Text': {
                            'Data': body_text,
                            'Charset': 'UTF-8'
                        }
                    }
                }
            }
        )
        print(f"✅ 이메일 발송 성공!")
        print(f"Message ID: {response['MessageId']}")
        return response
        
    except ClientError as e:
        print(f"❌ 오류 발생: {e.response['Error']['Message']}")
        return None

def send_html_email(from_email, to_email, subject, html_body, text_body=None):
    """
    HTML 이메일 발송
    """
    content = {
        'Simple': {
            'Subject': {
                'Data': subject,
                'Charset': 'UTF-8'
            },
            'Body': {
                'Html': {
                    'Data': html_body,
                    'Charset': 'UTF-8'
                }
            }
        }
    }
    
    if text_body:
        content['Simple']['Body']['Text'] = {
            'Data': text_body,
            'Charset': 'UTF-8'
        }
    
    try:
        response = ses_client.send_email(
            FromEmailAddress=from_email,
            Destination={'ToAddresses': [to_email]},
            Content=content
        )
        print(f"✅ HTML 이메일 발송 성공!")
        print(f"Message ID: {response['MessageId']}")
        return response
        
    except ClientError as e:
        print(f"❌ 오류 발생: {e.response['Error']['Message']}")
        return None

if __name__ == "__main__":
    # 사용 예시
    
    # 1. 간단한 텍스트 이메일
    send_simple_email(
        from_email="hyoon@akamai.com",
        to_email="dmacho@naver.com",
        subject="Python SES 테스트",
        body_text="이메일 내용입니다.\n\n감사합니다."
    )
    
    # 2. HTML 이메일
    html_content = """
    <html>
    <body>
        <h1>안녕하세요!</h1>
        <p>이것은 <b>HTML</b> 이메일입니다.</p>
        <p>감사합니다.</p>
    </body>
    </html>
    """
    
    # send_html_email(
    #     from_email="hyoon@akamai.com",
    #     to_email="dmacho@naver.com",
    #     subject="HTML 테스트 이메일",
    #     html_body=html_content,
    #     text_body="텍스트 버전입니다."
    # )
