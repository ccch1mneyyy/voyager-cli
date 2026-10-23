import {
  BRIDGE_PATHS,
  bridgePath,
  type JobSubmitResponse,
  type OpName,
  type StatusResponse,
} from '@voyager-crew/bridge-protocol';
import { afterEach, describe, expect, it } from 'vitest';

import { exitForJob, EXIT } from '../../src/cli/exitCodes';
import type { JobDetail } from '../../src/jobs/jobStore';
import type { PageTurnOptions } from '../../src/jobs/pageTurns';
import type { ServeStatus } from '../../src/server/serveStatus';
import { ASK_HANDLERS, FakeExtension, pairFake, type OpHandler } from '../helpers/fakeExtension';
import { data, startTestServer, until, type TestServer } from '../helpers/server';

let t: TestServer | undefined;
let fake: FakeExtension | undefined;
afterEach(async () => {
  await fake?.stop();
  await t?.close();
  fake = undefined;
  t = undefined;
});

/** Ops of a named job that wait on the page until `release`: a job that holds its turn. */
function holdable() {
  const held = new Map<string, Promise<void>>();
  const releases = new Map<string, () => void>();
  const keyOf = (op: OpName, jobId: string) => `${op} ${jobId}`;
  const wrap =
    (op: OpName): OpHandler =>
    async (params, task, tools) => {
      await held.get(keyOf(op, task.jobId));
      return ASK_HANDLERS[op]?.(params, task, tools);
    };
  const ops: OpName[] = ['conversation.open', 'model.select', 'composer.send', 'turns.waitReply'];
  return {
    handlers: { ...ASK_HANDLERS, ...Object.fromEntries(ops.map((op) => [op, wrap(op)])) },
    hold(op: OpName, jobId: string): void {
      held.set(keyOf(op, jobId), new Promise((resolve) => releases.set(keyOf(op, jobId), resolve)));
    },
    release(op: OpName, jobId: string): void {
      releases.get(keyOf(op, jobId))?.();
    },
  };
}

async function setup(pageTurns?: PageTurnOptions) {
  t = await startTestServer(pageTurns === undefined ? {} : { pageTurns });
  const gate = holdable();
  fake = new FakeExtension(t.port, gate.handlers);
  fake.maxTasks = 2;
  await pairFake(fake, t.cli);
  fake.start();
  const server = t;
  await until(async () => {
    const status = data<StatusResponse>(await server.cli('GET', BRIDGE_PATHS.status));
    return status.extension.lastPollAt !== null;
  });
  return { t: server, fake, gate };
}

async function submitAsk(server: TestServer): Promise<string> {
  const response = await server.cli('POST', BRIDGE_PATHS.jobs, {
    plugin: 'ask',
    command: 'ask',
    input: { prompt: 'Reply with OK', keepTab: false },
  });
  return data<JobSubmitResponse>(response).jobId;
}

const started = (extension: FakeExtension, op: OpName, jobId: string) =>
  until(() =>
    extension.timeline.some((e) => e.at === 'start' && e.task.op === op && e.task.jobId === jobId),
  );

async function finished(server: TestServer, jobId: string): Promise<JobDetail> {
  let job: JobDetail | undefined;
  await until(async () => {
    job = data<JobDetail>(await server.cli('GET', bridgePath.job(jobId)));
    return !['running', 'waiting-reply', 'cancelling'].includes(job.status);
  }, 8_000);
  if (job === undefined) throw new Error('unreachable');
  return job;
}

const opsOf = (extension: FakeExtension, jobId: string) =>
  extension.tasks.filter((task) => task.jobId === jobId).map((task) => task.op);

describe('waiting for a turn at the page', () => {
  it('cancels a job that is waiting for its turn, without sending anything for it', async () => {
    const { t, fake, gate } = await setup();
    const a = await submitAsk(t);
    gate.hold('composer.send', a);
    await started(fake, 'composer.send', a);
    const b = await submitAsk(t);
    await until(async () => {
      const status = data<ServeStatus>(await t.cli('GET', BRIDGE_PATHS.status));
      return status.activeJobs?.find((job) => job.jobId === b)?.pageTurn === 'waiting';
    });
    const status = data<ServeStatus>(await t.cli('GET', BRIDGE_PATHS.status));
    expect(status.activeJobs?.find((job) => job.jobId === a)?.pageTurn).toBe('holding');

    await t.cli('POST', bridgePath.jobCancel(b));
    expect((await finished(t, b)).status).toBe('cancelled');
    expect(opsOf(fake, b)).toEqual([]);
    gate.release('composer.send', a);
    expect((await finished(t, a)).status).toBe('succeeded');
  });

  it('fails a job as busy (exit 5), naming the holder, when its turn does not come in time', async () => {
    const { t, fake, gate } = await setup({ waitMs: 150 });
    const a = await submitAsk(t);
    gate.hold('composer.send', a);
    await started(fake, 'composer.send', a);
    const b = await submitAsk(t);

    const late = await finished(t, b);
    expect(late.status).toBe('failed');
    expect(late.error).toMatchObject({ code: 'busy', details: { activeJobId: a, pageTurn: true } });
    expect(late.error?.message).toContain(`job ${a}`);
    expect(exitForJob(late)).toBe(EXIT.busy);
    expect(opsOf(fake, b)).toEqual([]);
    gate.release('composer.send', a);
    expect((await finished(t, a)).status).toBe('succeeded');
  });

  it('ends a job whose tab close cannot get a turn, leaving the tab, with its own outcome', async () => {
    const { t, fake, gate } = await setup({ waitMs: 10_000, cleanupWaitMs: 150 });
    const b = await submitAsk(t);
    gate.hold('turns.waitReply', b);
    await started(fake, 'turns.waitReply', b);
    const a = await submitAsk(t);
    gate.hold('composer.send', a);
    await started(fake, 'composer.send', a);

    gate.release('turns.waitReply', b);
    expect((await finished(t, b)).status).toBe('succeeded');
    expect(opsOf(fake, b)).not.toContain('conversation.close');
    gate.release('composer.send', a);
    expect((await finished(t, a)).status).toBe('succeeded');
    expect(opsOf(fake, a)).toContain('conversation.close');
  });
});
