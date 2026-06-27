
import boto3, os, json

ssm = boto3.client('ssm', endpoint_url=os.environ.get('MINISTACK_ENDPOINT', 'http://localhost:4566'))

def handler(event, context):
    table = ssm.get_parameter(Name='/dic-reviews/dynamodb-table')['Parameter']['Value']
    # TODO: P2 fills profanity check logic here
    return {"statusCode": 200, "body": "profanity skeleton ok"}
