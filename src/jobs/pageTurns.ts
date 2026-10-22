/**
 * Whose turn it is to act on a ChatGPT page. The extension lets one op type,
 * click, open or close at a time across all its tabs (its page lease), and
 * answers `busy` rather than queueing, so jobs that run side by side take turns
 * here before such an op goes out. Waits and reads never take a turn: replies of
 * several jobs are awaited at once. A job may hold its turn across several ops
 * (`ops.exclusive`); turns are first come, first served.
 */
import type { OpName } from '@voyager-crew/bridge-protocol';

import { failure } from '../util/errors';

/** Ops that only watch or read a page, so they run beside another job's turn. */
const PASSIVE_OPS: ReadonlySet<OpName> = new Set<OpName>([
  'model.inspect',
  'turns.waitReply',
  'turns.read',
  'files.download',
  'project.list',
  'catalog.refresh',
]);

/** Ops the extension runs under its page lease, plus the tab ops that lease a step. */
export function takesPageTurn(op: OpName): boolean {
  return !PASSIVE_OPS.has(op);
}

interface Waiter {
  readonly jobId: string;
  readonly grant: () => void;
}

export class PageTurns {
  private holder: string | undefined;
  private depth = 0;
  private readonly queue: Waiter[] = [];

  /** Wait for `jobId`'s turn (at once when it already holds it); call the result to give it back. */
  async take(jobId: string, signal?: AbortSignal): Promise<() => void> {
    if (this.holder === undefined || this.holder === jobId) {
      this.grant(jobId);
    } else {
      await new Promise<void>((resolve, reject) => {
        const waiter: Waiter = { jobId, grant: resolve };
        this.queue.push(waiter);
        signal?.addEventListener(
          'abort',
          () => {
            const index = this.queue.indexOf(waiter);
            if (index < 0) return;
            this.queue.splice(index, 1);
            reject(failure('cancelled', 'job was cancelled'));
          },
          { once: true },
        );
      });
    }
    let given = false;
    return () => {
      if (given) return;
      given = true;
      this.giveBack();
    };
  }

  /** The job whose turn it is, if any. */
  current(): string | undefined {
    return this.holder;
  }

  private grant(jobId: string): void {
    this.holder = jobId;
    this.depth += 1;
  }

  private giveBack(): void {
    this.depth -= 1;
    if (this.depth > 0) return;
    this.holder = undefined;
    const next = this.queue.shift();
    if (next === undefined) return;
    this.grant(next.jobId);
    next.grant();
  }
}
