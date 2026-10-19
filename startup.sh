#!/bin/bash
DB_HOST=$(aws ssm get-parameter --region us-east-1 --name /clouddesk/db_host --query Parameter.Value --output text)
DB_PASSWORD=$(aws ssm get-parameter --region us-east-1 --name /clouddesk/db_password --with-decryption --query Parameter.Value --output text)
docker rm -f clouddesk 2>/dev/null
docker run -d --name clouddesk --restart unless-stopped -p 80:5000 \
  -e DB_HOST="$DB_HOST" -e DB_USER=admin -e DB_PASSWORD="$DB_PASSWORD" -e DB_NAME=clouddesk \
  clouddesk
