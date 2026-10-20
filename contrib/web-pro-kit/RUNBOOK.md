# Setup and project runbook

## Install and configure

Keep this directory together. Use Python 3.12, Git, and a paired Voyager CLI; follow the
[Voyager skill](../../skills/voyager/SKILL.md) for pairing. Commands below run from the checkout root;
when using another project, substitute the kit's installed script paths.

Connect ChatGPT and Drive desktop to the same Google account. Create `Voyager-workspace` in
`My Drive` or `我的云端硬盘`, with this layout:

```text
Voyager-workspace/
  repos/<project>/
  tools/request-channel/
  <task-id>/
```

Copy `config.example.json` to `~/.voyager/web-pro-kit.json`, or `$VOYAGER_HOME/web-pro-kit.json`.
Set `workspace` to the existing local workspace path and `redact` to a list of private terms.
JSON-relative paths resolve beside the config file. Environment-relative paths use the current directory.
`--config <file>` or `VOYAGER_WEB_PRO_CONFIG` selects another config;
`VOYAGER_WEB_PRO_WORKSPACE` overrides its workspace.

Discovery checks Windows drive letters and user-profile Drive folders for both official names,
and macOS `~/Library/CloudStorage/GoogleDrive-*/My Drive` or `我的云端硬盘`.
Choose an explicit workspace when several accounts exist.
On Linux, configure an rclone-mounted workspace, or export with `--out` and upload the files yourself.
For manual transfer, also upload request changes and download task outputs before using the waiter.

## Choose what this project shares

The mirror exports committed `HEAD` through `git archive`. Commit the intended changes first;
uncommitted and untracked files stay local. Add exclusions to the project's committed `.gitattributes`:

```gitattributes
private/ export-ignore
private/** export-ignore
local-settings.json export-ignore
build/ export-ignore
build/** export-ignore
```

Use `export-ignore` for private/generated paths and the config's `redact` list for private terms.
The text layer applies home-path, identity, email and credential patterns and records locations in
`_mirror/redactions.tsv`. Inspect the first export with `--out`; review paths and binary content too.
Use `--assets` for images, audio or other binaries; matching identity terms exclude a whole binary file.
Binary volumes group roughly 200 MB of input. Prepare symbolic links, submodules and external assets separately.

## Publish the mirror

```sh
python contrib/web-pro-kit/scripts/drive_mirror.py push --repo . --name sample --out ./mirror-preview
python contrib/web-pro-kit/scripts/drive_mirror.py push --repo . --name sample --assets
python contrib/web-pro-kit/scripts/drive_mirror.py status --repo . --name sample
```

The default destination is `<workspace>/repos/<project>`; `--out` selects the exact output directory.
It contains `MIRROR.json`, `<project>-text.zip` and optional `<project>-bin*.zip`.
The text ZIP includes `INDEX/` and `_mirror/`. Each project uses its own destination; keep task files
in the task folder. The publisher replaces its archives and removes obsolete volumes.
Unchanged layers are reused; `--force` refreshes an export. After changing redaction terms, use
`--assets` to refresh binaries too. Wait for Drive sync before dispatch.
Ask the receiver to verify ZIP hashes and the full commit in both `MIRROR.json` and `_mirror/STAMP.json`,
then extract into a new directory. Fetch binaries when needed and check `same_as_text_commit`.
Voyager's `--drive <file|folder>` remains the rclone upload path for individual question inputs.

## Prepare and steer a task

Copy `request-channel/` to Drive's `tools/request-channel/`. Choose a fresh task ID such as `review-alpha`.
Create its initial request, then append later changes using the same command:

```sh
python contrib/web-pro-kit/scripts/drive_request.py review-alpha "Review the parser."
python contrib/web-pro-kit/scripts/drive_request.py review-alpha --file additional-request.md
python contrib/web-pro-kit/scripts/drive_request.py review-alpha --show
```

One writer appends entries and increments `Version: N`; Drive sync carries them to the next checkpoint.
Adapt `templates/task-header.md` and the blank `templates/project-header.md` to the project.
Paste `request-channel/PROMPT-BLOCK.md` into the indicated slot.
Set `{{WORKSPACE}}` to the Drive-visible task folder and `{{TOOLS}}` to its shared request-channel folder.
The channel README covers sandbox installation and activation in an existing shell or kernel.
The header asks for an initial empty `test.txt`, all available subagent slots including a reviewer,
and a ZIP plus `NOTES.md` saved to Drive before writing `DONE.txt` last.

## Submit and collect

```powershell
$prompt = Get-Content -Raw -Encoding UTF8 ./PROMPT.md
voyager ask --json --no-wait --timebox 70 $prompt
voyager job wait <job-id> --timeout 10m --json
```

Voyager supplies the 70-minute wrap-up instructions. Continue waiting on the same job after a timeout.
Use its `--approve-apps once|always` when authorized; otherwise handle the reported approval card.
Run the waiter in the local agent's background-job facility:

```sh
python contrib/web-pro-kit/scripts/drive_wait.py "<local-workspace>/review-alpha/DONE.txt"
```

Exit 0 reports the marker; exit 2 reports new output followed by ten quiet minutes without it.
Review the conversation, ZIP and `NOTES.md` on either result. `--quiet 0` disables the fallback;
`--every` sets polling seconds. Reusing a task folder requires removing its previous marker first.

## Adapt and check

For a different environment, locate its Drive root and Python/Git executables, set explicit paths,
run the selftests, inspect a local export, and try a small task. Adapt discovery or sandbox hooks as needed.
Python tooling stays in this directory; its checks are each script's `--selftest`.

```sh
python contrib/web-pro-kit/scripts/kit_config.py --selftest
python contrib/web-pro-kit/scripts/secret_patterns.py --selftest
python contrib/web-pro-kit/scripts/code_index.py --selftest
python contrib/web-pro-kit/scripts/drive_mirror.py --selftest
python contrib/web-pro-kit/scripts/drive_request.py --selftest
python contrib/web-pro-kit/scripts/drive_wait.py --selftest
python contrib/web-pro-kit/request-channel/channel.py --selftest
bunx --no-install oxfmt --check contrib/web-pro-kit
```
