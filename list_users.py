import asyncio
import os

from dotenv import load_dotenv
from okta.client import Client as OktaClient

load_dotenv()

org_url = os.getenv("OKTA_ORG_URL")
token = os.getenv("OKTA_API_TOKEN")

client = OktaClient({"orgUrl": org_url, "token": token})


async def main():
    users, resp, err = await client.list_users()
    if err:
        print("Error:", err)
        return
    print("Users found:", len(users))
    for user in users:
        print(user.profile.first_name, user.profile.last_name, user.profile.email)


asyncio.run(main())