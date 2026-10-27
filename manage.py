"""Account management from the command line, for first-time setup and emergencies.

Run it inside the running container, for example:

    docker exec -it clouddesk python manage.py list-users
    docker exec -it clouddesk python manage.py create-user
    docker exec -it clouddesk python manage.py set-role someone@example.com administrator
    docker exec -it clouddesk python manage.py set-password someone@example.com
"""
import argparse
import getpass
import sys

from werkzeug.security import generate_password_hash

import app as clouddesk


def create_user(full_name, email, phone, role, password, must_change=False):
    full_name = " ".join(full_name.split())
    email = email.strip().lower()
    phone = phone.strip()
    errors = clouddesk.check_user_fields(full_name, email, phone)
    if role not in clouddesk.ROLES:
        errors.append("The role must be one of: " + ", ".join(clouddesk.ROLES) + ".")
    if len(password) < 8:
        errors.append("The password must be at least 8 characters.")
    if errors:
        raise ValueError(" ".join(errors))
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE email=%s", (email,))
            if cur.fetchone():
                raise ValueError("A user with this email already exists.")
            cur.execute("INSERT INTO users (full_name, email, phone, password_hash, role, must_change_password) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        (full_name, email, phone, generate_password_hash(password), role, 1 if must_change else 0))
            return cur.lastrowid
    finally:
        conn.close()


def set_role(email, role):
    if role not in clouddesk.ROLES:
        raise ValueError("The role must be one of: " + ", ".join(clouddesk.ROLES) + ".")
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET role=%s WHERE email=%s", (role, email.strip().lower()))
            if cur.rowcount == 0:
                cur.execute("SELECT id FROM users WHERE email=%s", (email.strip().lower(),))
                if not cur.fetchone():
                    raise ValueError("No user has that email.")
    finally:
        conn.close()


def set_password(email, password, must_change=False):
    if len(password) < 8:
        raise ValueError("The password must be at least 8 characters.")
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password_hash=%s, must_change_password=%s, "
                        "session_version=session_version+1, active=1 WHERE email=%s",
                        (generate_password_hash(password), 1 if must_change else 0, email.strip().lower()))
            if cur.rowcount == 0:
                raise ValueError("No user has that email.")
    finally:
        conn.close()


def list_users():
    conn = clouddesk.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, full_name, email, role, active FROM users ORDER BY id")
            return list(cur.fetchall())
    finally:
        conn.close()


def ask_password():
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat the password: "):
        raise ValueError("The two passwords do not match.")
    return first


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list-users", help="show every account")
    create = sub.add_parser("create-user", help="create an account (asks for anything you leave out)")
    create.add_argument("--name")
    create.add_argument("--email")
    create.add_argument("--phone")
    create.add_argument("--role", choices=clouddesk.ROLES)
    role = sub.add_parser("set-role", help="change a user's role")
    role.add_argument("email")
    role.add_argument("role", choices=clouddesk.ROLES)
    password = sub.add_parser("set-password", help="set a new password and sign the user out everywhere")
    password.add_argument("email")
    args = parser.parse_args(argv)
    try:
        if args.command == "list-users":
            for u in list_users():
                print(f"{u['id']:>4}  {u['role']:<14} {'active' if u['active'] else 'DISABLED':<9} {u['full_name']} <{u['email']}>")
        elif args.command == "create-user":
            name = args.name or input("Full name: ")
            email = args.email or input("Email: ")
            phone = args.phone or input("Phone: ")
            chosen = args.role or input("Role (" + "/".join(clouddesk.ROLES) + "): ")
            create_user(name, email, phone, chosen, ask_password())
            print("User created.")
        elif args.command == "set-role":
            set_role(args.email, args.role)
            print("Role updated.")
        elif args.command == "set-password":
            set_password(args.email, ask_password())
            print("Password updated.")
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
