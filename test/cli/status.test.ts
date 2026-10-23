import { describe, expect, it } from 'vitest';

import { statusJobLines } from '../../src/cli/commands/info';
import type { ServeStatus } from '../../src/server/serveStatus';

const NOW = 1_000_000;

function status(
  extension: Partial<ServeStatus['extension']>,
  rest: Partial<ServeStatus> = {},
): ServeStatus {
  return {
    extension: {
      paired: true,
      lastPollAt: NOW - 2_000,
      version: '2.2.1',
      browser: 'chrome',
      policy: null,
      ...extension,
    },
    maxJobs: 1,
    serverVersion: '0.5.0',
    ...rest,
  };
}

describe('voyager status: page turns', () => {
  it('names the job that has the page turn and the jobs waiting for it', () => {
    const lines = statusJobLines(
      status(
        {},
        {
          maxJobs: 3,
          activeJobs: [
            { jobId: 'j1', op: 'composer.send', pageTurn: 'holding' },
            { jobId: 'j2', op: 'turns.waitReply' },
            { jobId: 'j3', pageTurn: 'waiting' },
          ],
        },
      ),
    );
    expect(lines[0]).toBe(
      'active jobs: j1 (composer.send, has the page turn), j2 (turns.waitReply), j3 (waiting for the page turn)',
    );
  });
});
