
import boto3, os, json

ssm = boto3.client('ssm', endpoint_url=os.environ.get('MINISTACK_ENDPOINT', 'http://localhost:4566'))

def handler(event, context):
    bucket = ssm.get_parameter(Name='/dic-reviews/input-bucket')['Parameter']['Value']
    # TODO: P2 fills preprocessing logic here
    return {"statusCode": 200, "body": "preprocessing skeleton ok"}
