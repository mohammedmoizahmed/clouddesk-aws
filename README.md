# CloudDesk: a highly available IT helpdesk on AWS

CloudDesk is a small IT helpdesk app (raise a ticket, attach a screenshot, track status) built to put core AWS services together end to end: a private network, a load-balanced and auto-scaling web tier, a managed relational database, and an event-driven file-validation pipeline.

The application is deliberately simple. The focus of the project is the infrastructure around it: how it is secured, how it recovers from a failed server, and how it scales under load.

![CloudDesk served through the load balancer](docs/images/app-via-load-balancer.png)
*The app loaded from the load balancer's DNS name.*

## Contents

- [Architecture](#architecture)
- [Services and their roles](#services-and-their-roles)
- [How it works](#how-it-works)
- [Network design](#network-design)
- [Security design](#security-design)
- [Data layer](#data-layer)
- [Deployment](#deployment)
- [Tests and results](#tests-and-results)
- [Problems met and how they were solved](#problems-met-and-how-they-were-solved)
- [Limitations and next steps](#limitations-and-next-steps)
- [Cost control](#cost-control)
- [Repository contents](#repository-contents)

## Architecture

![CloudDesk architecture diagram](docs/images/architecture.png)
*A visitor reaches the load balancer, which sends traffic to web servers in two zones. Tickets go to the database, screenshots go to S3 and are checked by Lambda, and an alarm adds a server when CPU is high.*

In short: visitors reach an Application Load Balancer, which forwards to web servers in an Auto Scaling Group across two Availability Zones. The servers keep tickets in RDS (private subnets), upload screenshots to S3, and read their settings from Parameter Store. Each upload triggers a Lambda function that validates the file and records the result in DynamoDB. A CloudWatch alarm on average CPU lets the group add a third server under load.

## Services and their roles

| Service | Role in the project |
|---|---|
| VPC | Isolated network: 2 public and 2 private subnets across 2 Availability Zones |
| EC2 | Runs the application in a Docker container |
| EBS | Root volumes for every instance and AMI snapshots. A separate data volume with snapshots was used while the app ran on a single server, and was retired when tickets moved to RDS |
| ELB (Application Load Balancer) | Single public entry point, health checks, spreads traffic across zones |
| Auto Scaling Group | Keeps at least 2 servers running, replaces failed ones, scales out on CPU |
| RDS (MySQL) | Shared relational storage for tickets, in private subnets |
| S3 | Private storage for ticket screenshots |
| Lambda | Triggered by S3 uploads; validates file type and size, deletes rejected files |
| DynamoDB | Stores one record per uploaded file (accepted or rejected, reason, size, time) |
| Systems Manager Parameter Store | Holds the database host and password (SecureString), fetched at boot |
| IAM | Least-privilege roles for the web servers and for the Lambda function |
| CloudWatch | Scaling alarms and metrics |

## How it works

**A ticket with a screenshot**

1. The user submits the form through the load balancer.
2. A web server saves the ticket in RDS.
3. The server uploads the screenshot to S3 as `ticket-<id>/<filename>`.
4. S3 triggers the Lambda function.
5. Lambda accepts files of type png, jpg, jpeg, gif, pdf or txt up to 5 MB, and rejects everything else. It writes a record to DynamoDB and deletes rejected files from S3.

**A new server starting**

1. The Auto Scaling Group launches an instance from the launch template and the AMI.
2. The user data script runs `startup.sh`.
3. The script reads the database host and password from Parameter Store (using the instance role) and starts the container.
4. The load balancer begins sending traffic once `/health` returns `OK` twice.

No secrets are stored in the image, the repository or the launch template.

## Network design

![VPC resource map](docs/images/vpc-resource-map.png)
*VPC resource map: two public and two private subnets in two zones, an internet gateway and an S3 gateway endpoint. There is no NAT gateway.*

- **Public subnets** hold the load balancer and the web servers.
- **Private subnets** hold the database. They have no route to the internet.
- A **subnet group** places RDS only in the two private subnets, and the instance is not publicly accessible.
- An **S3 gateway endpoint** gives private, free access to S3 from the VPC.
- **No NAT gateway.** It bills every hour, so the web servers use public IPs to reach AWS APIs such as Parameter Store. In production they would sit in private subnets behind a NAT gateway or interface endpoints.

## Security design

![Security group rules](docs/images/security-groups.png)
*Inbound rules for the three security groups. Personal IP addresses are redacted.*

Three security groups form a chain, and the rules reference other groups, not IP addresses:

| Group | Accepts | From |
|---|---|---|
| `clouddesk-alb-sg` | HTTP 80 | The internet |
| `clouddesk-ec2-sg` | HTTP 80 | `clouddesk-alb-sg` only |
| `clouddesk-rds-sg` | MySQL 3306 | `clouddesk-ec2-sg` only |

Because rules point at groups, new servers added by the Auto Scaling Group are allowed automatically.

Other measures:

- **No credentials in code.** Servers use an IAM instance role. The instance metadata hop limit is set to 2 so that containers can use that role, and IMDSv2 is required.
- **Least privilege.** The web server role can only put and get objects in one bucket and read parameters under `/clouddesk/`. The Lambda role can only write to one table and delete objects in one bucket.
- **Private bucket** with all public access blocked.
- **Upload controls.** The app sanitises file names and caps request size, and Lambda validates the type and size.

## Data layer

**RDS (MySQL)** stores tickets. Moving from a local SQLite file to RDS was required for Auto Scaling: a file on one server cannot be shared by several, and users would see different tickets depending on which server answered.

**DynamoDB** stores the result of each file check.

![DynamoDB results](docs/images/dynamodb-results.png)
*Accepted and rejected uploads recorded by the Lambda function, including the reason for each rejection.*

**Parameter Store** holds the database host (String) and password (SecureString).

![Parameter Store](docs/images/parameter-store.png)
*The password is stored as a SecureString and is never written to the repository or the image.*

## Deployment

The application runs in Docker so that every server runs the identical image.

```bash
git clone https://github.com/<your-username>/clouddesk-aws.git
cd clouddesk-aws
docker build -t clouddesk .
bash startup.sh
```

`startup.sh` is what each new server runs at first boot:

```bash
#!/bin/bash
DB_HOST=$(aws ssm get-parameter --region us-east-1 --name /clouddesk/db_host --query Parameter.Value --output text)
DB_PASSWORD=$(aws ssm get-parameter --region us-east-1 --name /clouddesk/db_password --with-decryption --query Parameter.Value --output text)
docker rm -f clouddesk 2>/dev/null
docker run -d --name clouddesk --restart unless-stopped -p 80:5000 \
  -e DB_HOST="$DB_HOST" -e DB_USER=admin -e DB_PASSWORD="$DB_PASSWORD" -e DB_NAME=clouddesk \
  clouddesk
```

**Image and template**

- A server was built by hand, tested, then captured as an **AMI** with the app image already built inside it.
- The running container and shell history were removed before the image was taken, so no secrets are captured.
- The **launch template** defines the AMI, instance size, security group, instance role, metadata options and the user data that runs `startup.sh`.

**Load balancer and Auto Scaling Group**

| Setting | Value |
|---|---|
| Load balancer | Internet-facing Application Load Balancer, HTTP:80, in both public subnets |
| Target group | Health check on `/health` (a page that does not touch the database, so a database blip does not cause every server to be replaced) |
| Auto Scaling Group | Minimum 2, desired 2, maximum 3, across both zones |
| Scaling policy | Target tracking on average CPU utilisation at 50 percent |
| Health checks | Load balancer health checks enabled, so a server failing `/health` is replaced |

## Tests and results

### 1. Failover: a server is lost

I terminated one server by hand to see whether the group would recover without help. The group noticed, launched a replacement in the other slot, and the load balancer registered it.

![Load balancer with two healthy targets](docs/images/alb-two-healthy-targets.png)
*Before: two healthy targets.*

![One target left after a termination](docs/images/failover-1-one-target-left.png)
*A server is terminated; one healthy target remains and the site stays up.*

![Replacement registering](docs/images/failover-2-replacement-registering.png)
*The replacement is launched and registers with the target group (status Initial), then becomes healthy.*

![Auto Scaling activity history](docs/images/asg-activity-history.png)
*Activity history: each termination is followed by a launch of a replacement.*

| Time (UTC+3) | Event |
|---|---|
| 12:14 | Group created; two servers launched (desired capacity 0 to 2) |
| 12:34 | First server terminated; replacement launched within seconds |
| 12:40 | Second server terminated; replacement launched within seconds |

A replacement was healthy and receiving traffic within about four minutes of launch, which is the time the instance and the boot script need.

### 2. Scale-out under load

I ran a CPU-bound process on both servers to push average CPU above the 50 percent target.

![CPU at 100 percent on both cores](docs/images/scale-1-cpu-load-htop.png)
*Both cores at 100 percent on a server.*

![CloudWatch alarm in the ALARM state](docs/images/scale-2-cloudwatch-alarm.png)
*The target-tracking alarm (CPU above 50 for 3 datapoints within 3 minutes) moves to In alarm.*

The group responded by raising the desired capacity from 2 to 3 (the last entry in the activity history above), and the third server joined the load balancer.

![Three healthy targets](docs/images/scale-3-three-healthy-targets.png)
*After scale-out: three healthy targets behind the load balancer.*

## Problems met and how they were solved

| Problem | Cause | Fix |
|---|---|---|
| Browser-based EC2 Instance Connect could not connect | The security group allowed SSH only from my IP, but the console connects from an AWS service address range | Allowed SSH from the EC2 Instance Connect range for the region |
| A new EBS volume was far larger than intended | A default size was left in place | Checked with `lsblk` before formatting, detached it and created the right size |
| Tickets seemed to vanish after a restart | The `/etc/fstab` entry was missing its `UUID=` prefix, so the data volume was never mounted, and `nofail` hid the error. The app wrote to an empty folder on the root disk | Mounted the original volume, corrected `fstab`, recreated the container. Tested by rebooting |
| Unmount failed with "target is busy" | My own shell was inside the mounted folder | Changed directory before unmounting |
| Container could not start a second time | A container with the same name already existed | Removed it before running, and put `docker rm -f` in the start script |
| Database password sat in a file on the server | Quick first approach | Moved to Parameter Store with an instance role, and deleted the file before making the AMI |
| Local database could not be shared across servers | SQLite is one file on one disk | Migrated to RDS |

## Limitations and next steps

- **HTTP only.** Add HTTPS with an ACM certificate and a custom domain, and redirect port 80.
- **No authentication.** Anyone who can reach the site can change a ticket status. Add login (for example Cognito) and roles.
- **Database user.** The app connects as the master user. A dedicated user with limited privileges would be better, and RDS could manage the password through Secrets Manager.
- **Database availability.** RDS is single-AZ. Enabling Multi-AZ would remove it as a single point of failure.
- **Servers in public subnets.** Done to avoid NAT gateway cost. A production design would use private subnets.
- **Updates need a new image.** The app is baked into the AMI. A better pipeline would push images to ECR and roll them out with an instance refresh, driven by GitHub Actions.
- **Stale attachment names.** The ticket list still shows the file name of an upload that Lambda later rejected, because Lambda writes only to DynamoDB. The app could read the result from DynamoDB.
- **Minor.** The AMI contains a harmless `fstab` entry for the retired data volume, marked `nofail`.

## Cost control

- A budget alert was set before any resources were created.
- Resources that bill by the hour (RDS, the load balancer and extra instances) were created in short sessions and removed or stopped afterwards.
- No NAT gateway and no Multi-AZ database, to keep the bill small.

## Repository contents

```
app.py                  Flask application (routes and database access)
templates/index.html    Page design
requirements.txt        Python dependencies
Dockerfile              Container image definition
startup.sh              Boot script: reads settings from Parameter Store, starts the container
lambda/                 Lambda function that validates uploads
docs/images/            Screenshots used in this README
```
