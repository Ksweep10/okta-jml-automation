import asyncio
import csv
import os

from dotenv import load_dotenv
from okta import CreateUserRequest, UserProfile
from okta.client import Client as OktaClient

load_dotenv()

client = OktaClient({
    "orgUrl": os.getenv("OKTA_ORG_URL"),
    "token": os.getenv("OKTA_API_TOKEN"),
})


def read_events():
    with open("hr_events.csv", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


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


async def main():
    users_by_email, groups_by_name = await load_lookups()

    for row in read_events():
        if row["event"] != "joiner":
            continue

        email = row["email"]

        if email in users_by_email:
            user_id = users_by_email[email]
            print("Already exists:", email)
        else:
            profile = UserProfile(
                firstName=row["first_name"],
                lastName=row["last_name"],
                email=email,
                login=email,
                department=row["department"],
                title=row["title"],
            )
            request = CreateUserRequest(profile=profile)
            user, resp, err = await client.create_user(request, activate=False)
            if err:
                print("Could not create", email, "->", err)
                continue
            user_id = user.id
            users_by_email[email] = user_id
            print("Created:", email)

        group_id = groups_by_name.get(row["department"])
        if group_id is None:
            print("No group named", row["department"], "- skipping group step for", email)
            continue

        _, resp, err = await client.assign_user_to_group(group_id, user_id)
        if err:
            print("Could not add", email, "to", row["department"], "->", err)
            continue
        print("Added", email, "to", row["department"])


asyncio.run(main())