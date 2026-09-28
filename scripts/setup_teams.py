"""`make setup-teams`: guided Entra ID setup with the az CLI.

Creates (or reuses) one app registration used for both Graph and the bot, adds the Graph application permissions,
creates a client secret, and prints the steps only an admin can do — with your values filled in. Nothing is
changed without a yes at each step."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from typing import Any

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
PERMISSIONS = ["OnlineMeetings.Read.All", "OnlineMeetingTranscript.Read.All", "User.Read.All"]


def az(*args: str) -> Any:
    result = subprocess.run(
        ["az", *args, "-o", "json"], capture_output=True, text=True, shell=sys.platform == "win32"
    )
    if result.returncode != 0:
        sys.exit(f"az {' '.join(args)} failed:\n{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def ask(prompt: str, default: str = "") -> str:
    answer = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    return answer or default


def confirm(prompt: str) -> bool:
    return ask(f"{prompt} (y/n)", "y").lower().startswith("y")


def main() -> None:
    if shutil.which("az") is None:
        sys.exit("Install the Azure CLI first: https://learn.microsoft.com/cli/azure/install-azure-cli")
    account = az("account", "show")
    tenant = account["tenantId"]
    print(f"Signed in to tenant {tenant} as {account['user']['name']}.")
    base_url = ask("Public HTTPS base URL of Recall (from `azd up`, or a dev tunnel)", "https://")
    name = ask("App registration name", "Recall")

    existing = az("ad", "app", "list", "--display-name", name)
    if existing:
        app = existing[0]
        print(f"Reusing app registration {app['appId']}.")
    elif confirm(f"Create app registration '{name}' (single tenant)?"):
        app = az("ad", "app", "create", "--display-name", name, "--sign-in-audience", "AzureADMyOrg")
        az("ad", "sp", "create", "--id", app["appId"])
    else:
        sys.exit("Nothing changed.")
    app_id = app["appId"]

    roles = {r["value"]: r["id"] for r in az("ad", "sp", "show", "--id", GRAPH_APP_ID)["appRoles"]}
    if confirm(f"Add Graph application permissions {', '.join(PERMISSIONS)}?"):
        for perm in PERMISSIONS:
            az(
                "ad",
                "app",
                "permission",
                "add",
                "--id",
                app_id,
                "--api",
                GRAPH_APP_ID,
                "--api-permissions",
                f"{roles[perm]}=Role",
            )
    secret = None
    if confirm("Create a client secret (valid 1 year)?"):
        secret = az(
            "ad",
            "app",
            "credential",
            "reset",
            "--id",
            app_id,
            "--append",
            "--display-name",
            "recall",
            "--years",
            "1",
        )["password"]

    upn = ask("UPN of a meeting organizer whose meetings Recall should read (for the access policy)", "")
    print("\n" + "=" * 78)
    print("Remaining steps (need admin roles). Values are filled in for you.\n")
    print("1. Grant admin consent for the Graph permissions (Global/Privileged Role Administrator):")
    print(f"     az ad app permission admin-consent --id {app_id}\n")
    print("2. Allow the app to read online meetings (Teams administrator, Teams PowerShell):")
    print("     Connect-MicrosoftTeams")
    print(
        f'     New-CsApplicationAccessPolicy -Identity Recall-Policy -AppIds "{app_id}" '
        '-Description "Recall reads meeting transcripts"'
    )
    print("     Grant-CsApplicationAccessPolicy -PolicyName Recall-Policy -Global")
    if upn:
        print(
            f"     # or only for one organizer:  Grant-CsApplicationAccessPolicy -PolicyName Recall-Policy -Identity {upn}"
        )
    print(
        "\n3. Turn on transcription in the Teams meeting policy (Teams admin center > Meetings > Meeting policies >"
    )
    print(
        "   Recording & transcription > Transcription: On). Meetings must have transcription started (or auto-start).\n"
    )
    print("4. Create the Azure Bot resource (done by `azd up`), messaging endpoint:")
    print(f"     {base_url.rstrip('/')}/api/messages   (Microsoft Teams channel enabled)\n")
    print("5. Put these in Key Vault / .env:")
    print(f"     GRAPH_TENANT_ID={tenant}\n     GRAPH_CLIENT_ID={app_id}")
    print(f"     GRAPH_CLIENT_SECRET={'<the secret printed below>' if secret else '<create one>'}")
    print(f"     BOT_APP_ID={app_id}\n     BOT_APP_PASSWORD=<same secret>\n     BOT_TENANT_ID={tenant}")
    print(f"     PUBLIC_BASE_URL={base_url.rstrip('/')}")
    print("     GRAPH_CLIENT_STATE=<any long random string>\n")
    print(
        "6. Build and upload the Teams app:  make teams-package  → Teams > Apps > Manage your apps > Upload an app"
    )
    if secret:
        print("\nClient secret (shown once — store it now):")
        print(f"     {secret}")


if __name__ == "__main__":
    main()
