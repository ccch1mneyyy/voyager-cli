## Request updates

Fetch `channel.py`, `README.txt` and `VERSION` from `{{TOOLS}}` into the temporary sandbox.
Run `python3 channel.py --selftest`, then `python3 channel.py install --sandbox-home "$HOME"`; activate existing sessions as the README describes.
Only the coordinator reads `{{WORKSPACE}}/REQUEST.md`: read it in full at task start and before delivery.
Check again before delegation, integration and major decisions, and at safe checkpoints when the 15-minute reminder is due.
Compare Drive modification times; when unchanged, record the same version/time. When changed, read the full text and confirm a higher `Version: N` before recording.
Record with `python3 channel.py record --version N --mtime '<Drive modification time>'`; await a version increment for a same-version edit.
Forward new requirements to affected workers, obtain acknowledgment when available, and reconcile older results before integration.
Workers report the request version they used. Include the applied version and unfinished work in `NOTES.md`.
If metadata or hooks are unavailable, read the full request at each coordination checkpoint and continue the task.
