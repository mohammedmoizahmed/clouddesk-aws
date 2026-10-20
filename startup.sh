#!/bin/bash
get() { aws ssm get-parameter --region us-east-1 --name "$1" --with-decryption --query Parameter.Value --output text; }
DB_HOST=$(get /clouddesk/db_host)
DB_PASSWORD=$(get /clouddesk/db_password)
SECRET_KEY=$(get /clouddesk/secret_key)
docker rm -f clouddesk 2>/dev/null
docker run -d --name clouddesk --restart unless-stopped -p 80:5000 \
  -e DB_HOST="$DB_HOST" -e DB_USER=admin -e DB_PASSWORD="$DB_PASSWORD" -e DB_NAME=clouddesk \
  -e SECRET_KEY="$SECRET_KEY" clouddesk
