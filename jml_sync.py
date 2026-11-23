import asyncio
import csv
import os
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from okta import CreateUserRequest, UpdateUserRequest, UserProfile
from okta.client import Client as OktaClient

load_dotenv()

client = OktaClient({
    "orgUrl": os.getenv("OKTA_ORG_URL"),
    "token": os.getenv("OKTA_API_TOKEN"),
})

DEPARTMENT_GROUPS = {"Sales", "Engineering", "Finance"}
LOG_FIELDS = ["timestamp", "event", "email", "action", "result", "detail"]

audit_rows = []

def record(event, email, action, result, detail=""):
    detail = " ".join(str(detail).split())
    audit_rows.append({
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        "email": email,
        "action": action,
        "result": result,
        "detail": detail,
    })
    print(f"[{result}] {event} | {email} | {action} {detail}".rstrip())

def write_audit_log():
    os.makedirs("logs", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join("logs", f"audit_{stamp}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        writer.writeheader()
        writer.writerows(audit_rows)
    return path

def print_summary(log_path):
    results = Counter(r["result"] for r in audit_rows)
    print()
    print("===== RUN SUMMARY =====")
    print("Actions logged:", len(audit_rows))
    for result in ("OK", "SKIPPED", "FAILED"):
        print(f"  {result}: {results.get(result, 0)}")
    print("Audit log:", log_path)

def read_events():
    with open("hr_events.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def build_profile(row):
    return UserProfile(
        firstName=row["first_name"],
        lastName=row["last_name"],
        email=row["email"],
        login=row["email"],
        department=row["department"],
        title=row["title"],
    )

async def load_lookups():
    users_by_email = {}
    users, resp, err = await client.list_users()
    if err:
        raise RuntimeError(f"Could not list users: {err}")
    for u in users:
        users_by_email[u.profile.email] = u.id

    # Okta hides deactivated users from the normal list, so ask for them separately
    deactivated, resp, err = await client.list_users(search='status eq "DEPROVISIONED"')
    if err:
        raise RuntimeError(f"Could not list deactivated users: {err}")
    for u in deactivated:
        users_by_email[u.profile.email] = u.id

    groups_by_name = {}
    groups, resp, err = await client.list_groups()
    if err:
        raise RuntimeError(f"Could not list groups: {err}")
    for g in groups:
        groups_by_name[g.profile.actual_instance.name] = g.id

    return users_by_email, groups_by_name

async def handle_joiner(row, users_by_email, groups_by_name):
    event = "joiner"
    email = row["email"]

    if email in users_by_email:
        user_id = users_by_email[email]
        record(event, email, "create user", "SKIPPED", "already exists")

        # Never put a deactivated person back into a group
        user, resp, err = await client.get_user(user_id)
        if err:
            record(event, email, "read user", "FAILED", err)
            return
        status = getattr(user.status, "value", str(user.status))
        if status == "DEPROVISIONED":
            record(event, email, "add to group", "SKIPPED", "user is deactivated")
            return
    else:
        request = CreateUserRequest(profile=build_profile(row))
        user, resp, err = await client.create_user(request, activate=False)
        if err:
            record(event, email, "create user", "FAILED", err)
            return
        user_id = user.id
        users_by_email[email] = user_id
        record(event, email, "create user", "OK", "staged")

    group_id = groups_by_name.get(row["department"])
    if group_id is None:
        record(event, email, "add to group", "FAILED", f"no group named {row['department']}")
        return

    result = await client.assign_user_to_group(group_id, user_id)
    err = result[-1]  # the error is always the last item in the reply
    if err:
        record(event, email, "add to group", "FAILED", err)
        return
    record(event, email, "add to group", "OK", row["department"])

async def handle_mover(row, users_by_email, groups_by_name):
    event = "mover"
    email = row["email"]
    new_department = row["department"]

    user_id = users_by_email.get(email)
    if user_id is None:
        record(event, email, "find user", "FAILED", "not found in Okta")
        return

    new_group_id = groups_by_name.get(new_department)
    if new_group_id is None:
        record(event, email, "find group", "FAILED", f"no group named {new_department}")
        return

    update_request = UpdateUserRequest(profile=build_profile(row))
    _, resp, err = await client.update_user(user_id, update_request)
    if err:
        record(event, email, "update profile", "FAILED", err)
        return
    record(event, email, "update profile", "OK", f"{new_department} / {row['title']}")

    groups, resp, err = await client.list_user_groups(user_id)
    if err:
        record(event, email, "list groups", "FAILED", err)
        return

    for g in groups:
        name = g.profile.actual_instance.name
        if name in DEPARTMENT_GROUPS and name != new_department:
            _, resp, err = await client.unassign_user_from_group(g.id, user_id)
            if err:
                record(event, email, "remove from group", "FAILED", f"{name}: {err}")
            else:
                record(event, email, "remove from group", "OK", name)

    result = await client.assign_user_to_group(new_group_id, user_id)
    err = result[-1]
    if err:
        record(event, email, "add to group", "FAILED", err)
        return
    record(event, email, "add to group", "OK", new_department)

async def handle_leaver(row, users_by_email):
    event = "leaver"
    email = row["email"]

    user_id = users_by_email.get(email)
    if user_id is None:
        record(event, email, "find user", "SKIPPED", "not found (may already be deactivated)")
        return

    user, resp, err = await client.get_user(user_id)
    if err:
        record(event, email, "read user", "FAILED", err)
        return
    status = getattr(user.status, "value", str(user.status))

    if status == "DEPROVISIONED":
        record(event, email, "deactivate", "SKIPPED", "already deactivated")
        return

    _, resp, err = await client.revoke_user_sessions(user_id, oauth_tokens=True)
    if err:
        record(event, email, "revoke sessions", "FAILED", err)
    else:
        record(event, email, "revoke sessions", "OK", "sessions and tokens")

    if status == "ACTIVE":
        _, resp, err = await client.suspend_user(user_id)
        if err:
            record(event, email, "suspend", "FAILED", err)
        else:
            record(event, email, "suspend", "OK")
    else:
        record(event, email, "suspend", "SKIPPED", f"status is {status}, not ACTIVE")

    groups, resp, err = await client.list_user_groups(user_id)
    if err:
        record(event, email, "list groups", "FAILED", err)
    else:
        for g in groups:
            name = g.profile.actual_instance.name
            if name in DEPARTMENT_GROUPS:
                _, resp, err = await client.unassign_user_from_group(g.id, user_id)
                if err:
                    record(event, email, "remove from group", "FAILED", f"{name}: {err}")
                else:
                    record(event, email, "remove from group", "OK", name)

    _, resp, err = await client.deactivate_user(user_id, send_email=False)
    if err:
        record(event, email, "deactivate", "FAILED", err)
        return
    record(event, email, "deactivate", "OK")

async def main():
    try:
        users_by_email, groups_by_name = await load_lookups()

        for row in read_events():
            event = row["event"]
            if event == "joiner":
                await handle_joiner(row, users_by_email, groups_by_name)
            elif event == "mover":
                await handle_mover(row, users_by_email, groups_by_name)
            elif event == "leaver":
                await handle_leaver(row, users_by_email)
            else:
                record(event, row["email"], "process event", "FAILED", "unknown event type")
    finally:
        log_path = write_audit_log()
        print_summary(log_path)

asyncio.run(main())