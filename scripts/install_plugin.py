"""Install into a stopped AstrBot source deployment, with a config backup."""

import argparse
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-root", required=True, type=Path)
    parser.add_argument("--runtime-dir", type=Path, default=Path(".runtime"))
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--group-id", required=True, action="append")
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--platform-id", default="wechat_personal")
    args = parser.parse_args()
    root = args.astrbot_root.expanduser().resolve()
    runtime = args.runtime_dir.expanduser().resolve()
    project = Path(__file__).resolve().parent.parent
    config_path = root / "data/cmd_config.json"
    original = config_path.read_bytes()
    config = json.loads(original.decode("utf-8-sig"))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.platform_id):
        parser.error("Platform ID must use letters, digits, underscores or hyphens")
    if not all(re.fullmatch(r"\d+@chatroom", group) for group in args.group_id):
        parser.error("Use exact numeric group IDs returned by wx groups list")
    if not (runtime / "private/api-token").is_file():
        parser.error("Start the bridge first; its private/api-token is missing")
    provider = next(
        (
            p
            for p in config.get("provider", [])
            if p.get("id") == args.provider_id and p.get("enable")
        ),
        None,
    )
    if not provider:
        parser.error("Provider ID is missing or disabled in AstrBot")
    if config.get("agent_runner", {}).get("runner_type") != "local":
        parser.error("This version requires AstrBot's local agent runner")
    target = root / "data/plugins/astrbot_plugin_native_wechat"
    if target.exists() or any(
        p.get("id") == args.platform_id for p in config.get("platform", [])
    ):
        parser.error("Plugin or platform already exists; back up and upgrade manually")
    platform = {
        "id": args.platform_id,
        "type": "native_wechat",
        "enable": True,
        "server_url": "http://127.0.0.1:6174",
        "account_id": args.account_id,
        "token_file": str(runtime / "private/api-token"),
        "state_file": str(runtime / f"private/{args.platform_id}-state.json"),
        "allowed_groups": args.group_id,
        "chat_provider_id": args.provider_id,
    }
    config.setdefault("platform", []).append(platform)
    os.umask(0o077)
    backup = (
        runtime / "private/backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    )
    backup.mkdir(parents=True, mode=0o700)
    (backup / "cmd_config.json").write_bytes(original)
    if config_path.read_bytes() != original:
        parser.error(
            "AstrBot config changed during installation; stop AstrBot and retry"
        )
    target.mkdir(parents=True)
    for name in (
        "main.py",
        "panel.py",
        "metadata.yaml",
        "requirements.txt",
        "_conf_schema.json",
    ):
        shutil.copy2(project / name, target / name)
    # WebUI management panel page and its titles.
    for folder in ("views", ".astrbot-plugin"):
        shutil.copytree(project / folder, target / folder)
    temporary = config_path.with_suffix(".wechat.tmp")
    temporary.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8-sig"
    )
    temporary.chmod(0o600)
    temporary.replace(config_path)
    print(
        f"Installed {target}\nConfig backup: {backup}\nStart AstrBot to load the plugin."
    )


if __name__ == "__main__":
    main()
