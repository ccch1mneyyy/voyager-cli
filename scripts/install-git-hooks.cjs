#!/usr/bin/env node

const { execFileSync } = require('node:child_process');

try {
  execFileSync('git', ['rev-parse', '--is-inside-work-tree'], { stdio: 'ignore' });
  execFileSync('git', ['config', 'core.hooksPath', '.githooks'], { stdio: 'ignore' });
  process.stdout.write('Git hooks installed: core.hooksPath=.githooks\n');
} catch {
  // Package installs can happen outside a git checkout. Keep install non-blocking.
}
