# CloudDesk: IT Helpdesk on AWS

A small IT helpdesk ticketing app built to practice core AWS services end to end. Users raise tickets, attach screenshots, and track status. Uploaded files are validated automatically by a serverless function.

**Status:** In progress. The EC2, EBS, S3, Lambda and DynamoDB parts are working. RDS, the load balancer and Auto Scaling are planned.

## Architecture

```
Browser
   |
   v
EC2 instance (public subnet)
   |-- Docker container: Flask + gunicorn
   |-- EBS data volume mounted at /data (app log, SQLite for now)
   |
   v  (instance IAM role, no stored keys)
S3 bucket (private)
   |
   | object-created event
   v
Lambda (validates type and size)
   |
   v
DynamoDB (one record per uploaded file)
```

Planned: Application Load Balancer, Auto Scaling Group across two Availability Zones, and RDS (MySQL) in private subnets, replacing SQLite.

## AWS services and their role

| Service | Role in the project |
|---|---|
| VPC | Isolated network with 2 public and 2 private subnets across 2 Availability Zones |
| EC2 | Runs the application in a Docker container |
| EBS | Separate data volume for logs and the database, with snapshots for backup |
| S3 | Private storage for ticket screenshots |
| Lambda | Triggered by S3 uploads; checks file type and size, deletes rejected files |
| DynamoDB | Stores attachment metadata (accepted or rejected, reason, size, time) |
| IAM | Least-privilege roles for the instance and the function |
| RDS (planned) | Relational storage for tickets, shared by all servers |
| ALB and ASG (planned) | Single entry point, health checks, automatic replacement and scaling |

## How a ticket flows

1. The user submits the form (title, description, optional screenshot).
2. Flask saves the ticket in the database.
3. The screenshot is uploaded to S3 as `ticket-<id>/<filename>`.
4. S3 triggers the Lambda function.
5. Lambda accepts or rejects the file (allowed: png, jpg, jpeg, gif, pdf, txt; max 5 MB), writes a record to DynamoDB, and deletes rejected files from S3.

## Security design

- **Layered security groups:** internet reaches only the load balancer; the web servers accept traffic only from the load balancer's group; the database will accept traffic only from the web servers' group. Rules reference security groups, not IP addresses.
- **No credentials in code:** the EC2 instance uses an IAM role to reach S3.
- **Least privilege:** the instance role can only put and get objects in one bucket. The Lambda role can only write to one table and delete from one bucket.
- **Private bucket:** "Block all public access" is on.
- **No NAT gateway:** keeps cost down. Production would put application servers in private subnets with NAT or VPC endpoints.
- **Upload limits and filename sanitizing** in the app, and file validation in Lambda.

## Repository structure

```
app.py               Flask application (routes and logic)
templates/index.html The page design
requirements.txt     Python dependencies
Dockerfile           Container image definition
lambda/              Lambda function code (to be added)
```

## Run and deploy

Build and run on the instance:

```bash
git clone https://github.com/<your-username>/clouddesk-aws.git
cd clouddesk-aws
docker build -t clouddesk .
docker run -d --name clouddesk --restart unless-stopped \
  -p 80:5000 -v /data:/data clouddesk
```

Update after a code change:

```bash
cd ~/clouddesk-aws
git pull
docker build -t clouddesk .
docker rm -f clouddesk
docker run -d --name clouddesk --restart unless-stopped \
  -p 80:5000 -v /data:/data clouddesk
```

Data lives on the EBS volume mounted at `/data`, outside the container, so it survives container replacement.

## Design decisions

- **Separate EBS volume for data:** keeps data apart from the operating system, lets it be snapshotted and moved independently.
- **Docker:** the same image runs identically on every server, which the Auto Scaling Group will rely on.
- **Lambda for file validation:** runs only when a file arrives, is decoupled from the web app, and costs almost nothing when idle.
- **DynamoDB for attachment records:** simple key-value records with no complex queries.
- **RDS for tickets (planned):** relational data shared by multiple servers. SQLite is a single file on one server, so it cannot be shared across an Auto Scaling Group.

## Problems met and how they were solved

- **Browser-based EC2 Instance Connect failed.** The security group allowed SSH only from my IP, but the console connects from an AWS service address range. Fixed by allowing SSH from the EC2 Instance Connect range for the region.
- **Wrong EBS volume size.** `lsblk` showed a 100 GiB disk instead of the intended size. Caught before formatting by checking the real device, then replaced.
- **Rejected files.** A `.yaml` upload was rejected by Lambda as designed. Confirmed in the DynamoDB record and the function's CloudWatch logs.

## Known limitations

- The ticket list still shows an attachment name for files that Lambda later rejected, because the app does not read the DynamoDB result.
- SQLite supports a single server only; RDS is planned to fix this.

## Roadmap

- [x] VPC, subnets, security groups
- [x] S3 bucket, DynamoDB table, Lambda with S3 trigger
- [x] EC2 with IAM role, EBS data volume and snapshot
- [x] Dockerised Flask app deployed from GitHub
- [ ] RDS (MySQL) in private subnets
- [ ] AMI and launch template
- [ ] Application Load Balancer and Auto Scaling Group
- [ ] Failover and scaling tests
- [ ] Architecture diagram and cleanup

## Cost control

Budget alert enabled. Costly resources (RDS, load balancer, instances) are created in a single session and removed or stopped afterwards.
