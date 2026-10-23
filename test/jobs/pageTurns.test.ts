import { describe, expect, it } from 'vitest';

import { PageTurns } from '../../src/jobs/pageTurns';

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('page turns', () => {
  it('gives the page to one job at a time, in the order they asked', async () => {
    const turns = new PageTurns();
    const order: string[] = [];
    const first = await turns.take('j1');
    const second = turns.take('j2').then((giveBack) => {
      order.push('j2');
      return giveBack;
    });
    const third = turns.take('j3').then((giveBack) => {
      order.push('j3');
      return giveBack;
    });
    await tick();
    expect(order).toEqual([]);
    first();
    (await second)();
    (await third)();
    expect(order).toEqual(['j2', 'j3']);
    expect(turns.current()).toBeUndefined();
  });

  it('lets a job that holds its turn take it again inside it', async () => {
    const turns = new PageTurns();
    const outer = await turns.take('j1');
    const inner = await turns.take('j1');
    let other = false;
    void turns.take('j2').then(() => {
      other = true;
    });
    inner();
    await tick();
    expect(other).toBe(false);
    outer();
    await tick();
    expect(other).toBe(true);
  });

  it('drops a waiting job that was cancelled', async () => {
    const turns = new PageTurns();
    const held = await turns.take('j1');
    const controller = new AbortController();
    const waiting = turns.take('j2', { signal: controller.signal });
    expect(turns.waiting()).toEqual(['j2']);
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ code: 'cancelled' });
    expect(turns.waiting()).toEqual([]);
    held();
    expect(turns.current()).toBeUndefined();
  });

  it('refuses a job already cancelled, even when the page is free', async () => {
    const turns = new PageTurns();
    const controller = new AbortController();
    controller.abort();
    await expect(turns.take('j1', { signal: controller.signal })).rejects.toMatchObject({
      code: 'cancelled',
    });
    expect(turns.current()).toBeUndefined();
  });

  it('keeps a turn it granted when the job is cancelled afterwards', async () => {
    const turns = new PageTurns();
    const held = await turns.take('j1');
    const controller = new AbortController();
    const waiting = turns.take('j2', { signal: controller.signal });
    held();
    const giveBack = await waiting;
    controller.abort();
    expect(turns.current()).toBe('j2');
    giveBack();
    expect(turns.current()).toBeUndefined();
  });

  it('fails a job as busy, naming the holder, once it has waited too long', async () => {
    const turns = new PageTurns({ waitMs: 30 });
    const held = await turns.take('j1');
    const [late, next] = await Promise.allSettled([turns.take('j2'), turns.take('j3')]);
    expect(late).toMatchObject({
      status: 'rejected',
      reason: {
        code: 'busy',
        message: expect.stringContaining('job j1'),
        error: { details: { activeJobId: 'j1', pageTurn: true } },
      },
    });
    expect(next).toMatchObject({ status: 'rejected', reason: { code: 'busy' } });
    expect(turns.waiting()).toEqual([]);
    held();
    expect(turns.current()).toBeUndefined();
  });

  it('gives a cleanup step its own, shorter wait', async () => {
    const turns = new PageTurns({ waitMs: 5_000, cleanupWaitMs: 20 });
    const held = await turns.take('j1');
    const step = turns.take('j2');
    await expect(turns.take('j3', { cleanup: true })).rejects.toMatchObject({ code: 'busy' });
    expect(turns.waiting()).toEqual(['j2']);
    held();
    (await step)();
  });
});
