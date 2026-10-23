/**
 * Whose turn it is to act on a ChatGPT page. The extension lets one op type,
 * click, open or close at a time across all its tabs (its page lease), and
 * answers `busy` rather than queueing, so jobs that run side by side take turns
 * here before such an op goes out. Waits and reads never take a turn: replies of
 * several jobs are awaited at once. A job may hold its turn across several ops
 * (`ops.exclusive`); turns are first come, first served, and a job waits for one
 * only so long (`busy` then, before its op goes out).
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

/** How long a job waits for its turn before it fails as `busy`. */
export const PAGE_TURN_WAIT_MS = 5 * 60_000;
/**
 * How long closing a job's tab waits: the job's outcome is settled by then and only a
 * tab is at stake, so a holder that does not let go keeps it from ending only this long.
 */
export const CLEANUP_TURN_WAIT_MS = 60_000;

export interface PageTurnOptions {
  readonly waitMs?: number;
  readonly cleanupWaitMs?: number;
}

export interface TakeOptions {
  readonly signal?: AbortSignal;
  /** A cleanup step (closing the tab): it waits `cleanupWaitMs` at most. */
  readonly cleanup?: boolean;
}

interface Waiter {
  readonly jobId: string;
  readonly grant: () => void;
}

export class PageTurns {
  private holder: string | undefined;
  private depth = 0;
  private readonly queue: Waiter[] = [];
  private readonly waitMs: number;
  private readonly cleanupWaitMs: number;

  constructor(options: PageTurnOptions = {}) {
    this.waitMs = options.waitMs ?? PAGE_TURN_WAIT_MS;
    this.cleanupWaitMs = options.cleanupWaitMs ?? CLEANUP_TURN_WAIT_MS;
  }

  /** Wait for `jobId`'s turn (at once when it already holds it); call the result to give it back. */
  async take(jobId: string, options: TakeOptions = {}): Promise<() => void> {
    const { signal } = options;
    if (signal?.aborted) throw failure('cancelled', 'job was cancelled');
    if (this.holder === undefined || this.holder === jobId) this.grant(jobId);
    else
      await this.wait(jobId, signal, options.cleanup === true ? this.cleanupWaitMs : this.waitMs);
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

  /** Jobs waiting for their turn, first in line first. */
  waiting(): string[] {
    return this.queue.map((waiter) => waiter.jobId);
  }

  private wait(jobId: string, signal: AbortSignal | undefined, waitMs: number): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const leave = (): boolean => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', onAbort);
        const index = this.queue.indexOf(waiter);
        if (index < 0) return false;
        this.queue.splice(index, 1);
        return true;
      };
      const waiter: Waiter = {
        jobId,
        grant: () => {
          leave();
          resolve();
        },
      };
      const onAbort = (): void => {
        if (leave()) reject(failure('cancelled', 'job was cancelled'));
      };
      const timer = setTimeout(() => {
        const holder = this.holder;
        if (!leave()) return;
        reject(
          failure(
            'busy',
            `waited ${duration(waitMs)} for a turn at the ChatGPT page, which job ${holder ?? '?'} still holds; nothing was sent for this step`,
            { activeJobId: holder, pageTurn: true, waitedMs: waitMs },
          ),
        );
      }, waitMs);
      this.queue.push(waiter);
      signal?.addEventListener('abort', onAbort, { once: true });
    });
  }

  private grant(jobId: string): void {
    this.holder = jobId;
    this.depth += 1;
  }

  private giveBack(): void {
    this.depth -= 1;
    if (this.depth > 0) return;
    this.holder = undefined;
    const next = this.queue[0];
    if (next === undefined) return;
    this.grant(next.jobId);
    next.grant();
  }
}

function duration(ms: number): string {
  return ms >= 60_000 ? `${Math.round(ms / 6_000) / 10} min` : `${Math.round(ms / 1000)} s`;
}
