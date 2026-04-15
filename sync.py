#!/usr/bin/env python3
import sys, os, json, hashlib, argparse, fnmatch, time
from pathlib import Path
import requests, yaml

class OpenWebUIClient:
    def __init__(self, url, api_key):
        self.base = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"
    def _check(self, r):
        try: r.raise_for_status()
        except requests.HTTPError: raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r
    def list_knowledge(self):
        return self._check(self.session.get(f"{self.base}/api/v1/knowledge/")).json()
    def create_knowledge(self, name):
        return self._check(self.session.post(f"{self.base}/api/v1/knowledge/create", json={"name": name, "description": ""})).json()
    def get_or_create_knowledge(self, name):
        for k in self.list_knowledge():
            if k["name"] == name: return k
        print(f"  Creating collection: {name!r}")
        return self.create_knowledge(name)
    def add_file_to_knowledge(self, kid, fid):
        self._check(self.session.post(f"{self.base}/api/v1/knowledge/{kid}/file/add", json={"file_id": fid}))
    def remove_file_from_knowledge(self, kid, fid):
        self._check(self.session.post(f"{self.base}/api/v1/knowledge/{kid}/file/remove", json={"file_id": fid}))
    def upload_file(self, filename, content):
        headers = {"Authorization": self.session.headers["Authorization"]}
        return self._check(requests.post(f"{self.base}/api/v1/files/", headers=headers, files={"file": (filename, content.encode("utf-8"), "text/markdown")})).json()
    def delete_file(self, fid):
        self._check(self.session.delete(f"{self.base}/api/v1/files/{fid}"))

class SyncState:
    def __init__(self, path):
        self.path = Path(path)
        self._data = json.loads(self.path.read_text()) if self.path.exists() else {}
    def save(self):
        self.path.write_text(json.dumps(self._data, indent=2))
    def get(self, rel): return self._data.get(rel)
    def set(self, rel, h, fid): self._data[rel] = {"hash": h, "file_id": fid}
    def remove(self, rel): self._data.pop(rel, None)
    def all_paths(self): return set(self._data.keys())

def sha256(text): return hashlib.sha256(text.encode()).hexdigest()

def scan_vault(vault, exclude_dirs, exclude_patterns):
    files = {}
    for md in vault.rglob("*.md"):
        rel = str(md.relative_to(vault))
        if any(p in exclude_dirs for p in Path(rel).parts[:-1]): continue
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude_patterns): continue
        try: files[rel] = md.read_text(encoding="utf-8")
        except OSError as e: print(f"  [warn] {rel}: {e}")
    return files

def flat_filename(rel): return rel.replace("/", "__").replace("\\", "__")

def run_sync(config, dry_run=False):
    vault = Path(config["obsidian"]["vault_path"]).expanduser().resolve()
    exclude_dirs = config["obsidian"].get("exclude_dirs", [".obsidian", ".trash"])
    exclude_patterns = config["obsidian"].get("exclude_patterns", [])
    owui_url = config["openwebui"]["url"]
    api_key = os.environ.get("OPENWEBUI_API_KEY") or config["openwebui"].get("api_key")
    collection_name = config["openwebui"].get("collection_name", "Obsidian Vault")
    state_file = config["sync"].get("state_file", ".sync_state.json")
    delete_removed = config["sync"].get("delete_removed_files", True)
    if not vault.exists(): raise FileNotFoundError(f"Vault not found: {vault}")
    client = OpenWebUIClient(owui_url, api_key)
    state = SyncState(state_file)
    if not dry_run:
        collection = client.get_or_create_knowledge(collection_name)
        kid = collection["id"]
    else: kid = "dry-run"
    local = scan_vault(vault, exclude_dirs, exclude_patterns)
    print(f"Vault: {vault}  ({len(local)} files)")
    print(f"Collection: {collection_name!r}")
    changes = 0
    prefix = "[dry-run] " if dry_run else ""
    for rel, content in local.items():
        h = sha256(content)
        entry = state.get(rel)
        if entry is None:
            print(f"  {prefix}+ {rel}")
            if not dry_run:
                f = client.upload_file(flat_filename(rel), content)
                client.add_file_to_knowledge(kid, f["id"])
                state.set(rel, h, f["id"])
            changes += 1
        elif entry["hash"] != h:
            print(f"  {prefix}~ {rel}")
            if not dry_run:
                try:
                    client.remove_file_from_knowledge(kid, entry["file_id"])
                    client.delete_file(entry["file_id"])
                except Exception as e: print(f"    [warn] {e}")
                f = client.upload_file(flat_filename(rel), content)
                client.add_file_to_knowledge(kid, f["id"])
                state.set(rel, h, f["id"])
            changes += 1
    if delete_removed:
        for rel in list(state.all_paths()):
            if rel not in local:
                print(f"  {prefix}- {rel}")
                if not dry_run:
                    entry = state.get(rel)
                    try:
                        client.remove_file_from_knowledge(kid, entry["file_id"])
                        client.delete_file(entry["file_id"])
                    except Exception as e: print(f"    [warn] {e}")
                    state.remove(rel)
                changes += 1
    if not dry_run: state.save()
    print(f"{changes} file(s) {'would change' if dry_run else 'synced'}.")
    return changes

def watch_loop(config, interval):
    print(f"Watching every {interval}s — Ctrl+C to stop.\n")
    while True:
        try: run_sync(config)
        except Exception as e: print(f"[error] {e}")
        time.sleep(interval)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--watch", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    args = p.parse_args()
    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}")
        sys.exit(1)
    with open(cfg_path) as f: config = yaml.safe_load(f)
    if args.watch: watch_loop(config, args.interval)
    else:
        try: run_sync(config, dry_run=args.dry_run)
        except Exception as e: print(f"[error] {e}"); sys.exit(1)

if __name__ == "__main__": main()
