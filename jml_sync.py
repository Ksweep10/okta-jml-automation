import asyncio
import csv
import os

from dotenv import load_dotenv
from okta import CreateUserRequest, UpdateUserRequest, UserProfile
from okta.client import Client as OktaClient

load_dotenv()

client = OktaClient({
    "orgUrl": os.getenv("OKTA_ORG_URL"),
    "token": os.getenv("OKTA_API_TOKEN"),
})

DEPARTMENT_GROUPS = {"Sales", "Engineering", "Finance"}


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

    groups_by_name = {}
    groups, resp, err = await client.list_groups()
    if err:
        raise RuntimeError(f"Could not list groups: {err}")
    for g in groups:
        groups_by_name[g.profile.actual_instance.name] = g.id

    return users_by_email, groups_by_name


async def handle_joiner(row, users_by_email, groups_by_name):
    email = row["email"]

    if email in users_by_email:
        user_id = users_by_email[email]
        print("Already exists:", email)
    else:
        request = CreateUserRequest(profile=build_profile(row))
        user, resp, err = await client.create_user(request, activate=False)
        if err:
            print("Could not create", email, "->", err)
            return
        user_id = user.id
        users_by_email[email] = user_id
        print("Created:", email)

    group_id = groups_by_name.get(row["department"])
    if group_id is None:
        print("No group named", row["department"], "- skipping group step for", email)
        return

    _, resp, err = await client.assign_user_to_group(group_id, user_id)
    if err:
        print("Could not add", email, "to", row["department"], "->", err)
        return
    print("Added", email, "to", row["department"])


async def handle_mover(row, users_by_email, groups_by_name):
    email = row["email"]
    new_department = row["department"]

    user_id = users_by_email.get(email)
    if user_id is None:
        print("Mover not found in Okta:", email)
        return

    new_group_id = groups_by_name.get(new_department)
    if new_group_id is None:
        print("No group named", new_department, "- cannot move", email)
        return

    update_request = UpdateUserRequest(profile=build_profile(row))
    _, resp, err = await client.update_user(user_id, update_request)
    if err:
        print("Could not update profile for", email, "->", err)
        return
    print("Updated profile:", email, "->", new_department, "/", row["title"])

    groups, resp, err = await client.list_user_groups(user_id)
    if err:
        print("Could not list groups for", email, "->", err)
        return

    for g in groups:
        name = g.profile.actual_instance.name
        if name in DEPARTMENT_GROUPS and name != new_department:
            _, resp, err = await client.unassign_user_from_group(g.id, user_id)
            if err:
                print("Could not remove", email, "from", name, "->", err)
            else:
                print("Removed", email, "from", name)

    _, resp, err = await client.assign_user_to_group(new_group_id, user_id)
    if err:
        print("Could not add", email, "to", new_department, "->", err)
        return
    print("Added", email, "to", new_department)


async def handle_leaver(row, users_by_email):
    email = row["email"]

    user_id = users_by_email.get(email)
    if user_id is None:
        print("Leaver not found in Okta (may already be deactivated):", email)
        return

    user, resp, err = await client.get_user(user_id)
    if err:
        print("Could not read", email, "->", err)
        return
    status = getattr(user.status, "value", str(user.status))
    print("Leaver:", email, "current status:", status)

    if status == "DEPROVISIONED":
        print("Already deactivated:", email)
        return

    _, resp, err = await client.revoke_user_sessions(user_id, oauth_tokens=True)
    if err:
        print("Could not revoke sessions for", email, "->", err)
    else:
        print("Revoked sessions and tokens:", email)

    if status == "ACTIVE":
        _, resp, err = await client.suspend_user(user_id)
        if err:
            print("Could not suspend", email, "->", err)
        else:
            print("Suspended:", email)
    else:
        print("Not ACTIVE, skipping suspend:", email)

    groups, resp, err = await client.list_user_groups(user_id)
    if err:
        print("Could not list groups for", email, "->", err)
    else:
        for g in groups:
            name = g.profile.actual_instance.name
            if name in DEPARTMENT_GROUPS:
                _, resp, err = await client.unassign_user_from_group(g.id, user_id)
                if err:
                    print("Could not remove", email, "from", name, "->", err)
                else:
                    print("Removed", email, "from", name)

    _, resp, err = await client.deactivate_user(user_id, send_email=False)
    if err:
        print("Could not deactivate", email, "->", err)
        return
    print("Deactivated:", email)


async def main():
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
            print("Unknown event type:", event, row["email"])


asyncio.run(main())