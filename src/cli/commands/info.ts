import {
  BRIDGE_PATHS,
  EXTENSION_OFFLINE_MS,
  type CommandsResponse,
  type StatusResponse,
} from '@voyager-crew/bridge-protocol';

import { ensureServe } from '../../client/serveClient';
import type { StatePaths } from '../../state/paths';
import { accountJson, describeAccount } from '../accountPlan';
import { parse } from '../args';
import { note, printJson, printLine } from '../output';

export async function statusCommand(paths: StatePaths, argv: readonly string[]): Promise<number> {
  const args = parse(argv, { json: { type: 'boolean', summary: 'JSON output' } });
  const client = await ensureServe(paths);
  const status = await client.request<StatusResponse>('GET', BRIDGE_PATHS.status);
  if (args.options.json === true) {
    printJson({ ...status, port: client.port, ...accountJson(status.extension.account) });
    return 0;
  }
  const ext = status.extension;
  printLine(`serve: ${status.serverVersion} on 127.0.0.1:${client.port}`);
  if (!ext.paired) printLine('extension: not paired (run voyager pair)');
  else {
    const seen =
      ext.lastPollAt === null
        ? 'has not connected since serve started'
        : `last poll ${Math.round((Date.now() - ext.lastPollAt) / 1000)}s ago`;
    printLine(`extension: paired (${ext.browser ?? '?'}, Voyager ${ext.version ?? '?'}), ${seen}`);
    if (ext.policy !== null) {
      printLine(`policy: revision ${ext.policy.revision}, ${ext.policy.state}`);
    }
    const seenAt = ext.account?.observedAt;
    const age =
      seenAt === undefined
        ? ''
        : `, seen ${Math.max(0, Math.round((Date.now() - seenAt) / 60_000))} min ago`;
    printLine(`${describeAccount(ext.account)}${ext.account ? age : ''}`);
  }
  const active = status.activeJobs ?? (status.activeJob === undefined ? [] : [status.activeJob]);
  const named = active.map((job) => `${job.jobId}${job.op ? ` (${job.op})` : ''}`);
  const label = named.length > 1 ? 'active jobs' : 'active job';
  printLine(`${label}: ${named.length === 0 ? 'none' : named.join(', ')}`);
  if (status.maxJobs !== undefined) {
    printLine(
      status.maxJobs > 1
        ? `parallel jobs: up to ${status.maxJobs} at once`
        : 'parallel jobs: off (one job at a time; turn it on in the Voyager popup)',
    );
  }
  return 0;
}

export async function commandsCommand(paths: StatePaths, argv: readonly string[]): Promise<number> {
  const args = parse(argv, { json: { type: 'boolean', summary: 'JSON output' } });
  const client = await ensureServe(paths);
  const listed = await client.request<CommandsResponse>('GET', BRIDGE_PATHS.commands);
  if (args.options.json === true) {
    printJson(listed);
    return 0;
  }
  printLine('commands:');
  for (const entry of listed.commands) printLine(`  voyager ${entry.command}  ${entry.summary}`);
  printLine('extension ops (voyager call <op>):');
  if (listed.ops.length === 0)
    printLine('  none yet: the extension has not polled since serve started');
  for (const op of listed.ops) printLine(`  ${op.name} v${op.v}`);
  return 0;
}

/** Before a job: say so when the extension has not been heard from lately. */
export async function warnIfExtensionAway(
  client: Awaited<ReturnType<typeof ensureServe>>,
): Promise<StatusResponse> {
  const status = await client.request<StatusResponse>('GET', BRIDGE_PATHS.status);
  const last = status.extension.lastPollAt;
  if (status.extension.paired && (last === null || Date.now() - last > EXTENSION_OFFLINE_MS)) {
    note('Waiting for the extension to connect (is the browser open with the ChatGPT bridge on?)…');
  }
  return status;
}
