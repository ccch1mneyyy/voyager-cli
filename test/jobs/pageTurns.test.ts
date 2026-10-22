import { describe, expect, it } from 'vitest';

import { PageTurns, takesPageTurn } from '../../src/jobs/pageTurns';

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
    const waiting = turns.take('j2', controller.signal);
    controller.abort();
    await expect(waiting).rejects.toMatchObject({ code: 'cancelled' });
    held();
    expect(turns.current()).toBeUndefined();
  });

  it('lets waits and reads run beside a turn', () => {
    expect(takesPageTurn('turns.waitReply')).toBe(false);
    expect(takesPageTurn('turns.read')).toBe(false);
    expect(takesPageTurn('files.download')).toBe(false);
    expect(takesPageTurn('composer.send')).toBe(true);
    expect(takesPageTurn('conversation.open')).toBe(true);
    expect(takesPageTurn('turns.approve')).toBe(true);
  });
});
