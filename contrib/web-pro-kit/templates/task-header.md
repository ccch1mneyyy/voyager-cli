# Suggested task header

Adapt this example for each project; it works with the request-channel scripts.
Replace placeholders, append the filled project header, and submit with `voyager ask --timebox 70`.

---

Task: {{GOAL}}. Scope: {{SCOPE}}. Deliver: {{DELIVERABLES}}. Success: {{ACCEPTANCE_CRITERIA}}.
`{{WORKSPACE}}` is the Drive-visible task folder; `{{TOOLS}}` is the shared request-channel folder.

1. Start a new conversation by writing an empty `test.txt` to `{{WORKSPACE}}` through Drive,
   so write approval happens first. Resume an existing task by reading its `NOTES.md`.
2. {{PASTE_REQUEST_CHANNEL_PROMPT_BLOCK_HERE}}
3. Read `{{MIRROR}}/MIRROR.json`; download and verify the text ZIP, matching its
   `_mirror/STAMP.json` to commit `{{COMMIT}}`. Extract into a new directory and use `INDEX/`.
   Fetch binary ZIPs when needed and check `same_as_text_commit`; refetch after sync if versions differ.
4. Use all available subagent slots, including one reviewer assigned only to check the work.
   Give workers separate scopes, the current request version and the same timebox; report the number used.
5. Deliver one ZIP with a download link and save it under a new filename in `{{WORKSPACE}}`.
   Save `NOTES.md` separately: source commit, request version, completed work, checks, remaining work.
   After those saves succeed, write `DONE.txt` last: `finished N / unfinished M / ZIP filename`.
