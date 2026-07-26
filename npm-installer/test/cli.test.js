import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const cli = join(packageRoot, 'bin', 'aigateway-install.js')

test('installer help lists the npm and profile entrypoints', () => {
  const result = spawnSync(process.execPath, [cli, '--installer-help'], {
    encoding: 'utf8',
  })
  assert.equal(result.status, 0)
  assert.match(result.stdout, /npx aigateway-installer/)
  assert.match(result.stdout, /runtime\|rag\|vision\|full/)
})

test('installer reuses a checkout and forwards quickstart arguments', () => {
  const root = mkdtempSync(join(tmpdir(), 'aigateway-installer-'))
  const scripts = join(root, 'scripts')
  mkdirSync(scripts)
  const quickstart = join(scripts, 'quickstart.sh')
  const argsFile = join(root, 'received-args.txt')
  writeFileSync(
    quickstart,
    `#!/usr/bin/env bash\nprintf '%s\\n' "$@" > "${argsFile}"\n`,
    'utf8',
  )
  chmodSync(quickstart, 0o755)

  const result = spawnSync(process.execPath, [
    cli,
    '--dir',
    root,
    '--non-interactive',
    '--profile',
    'full',
    '--no-start',
  ], {
    encoding: 'utf8',
  })

  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(
    readFileSync(argsFile, 'utf8').trim().split('\n'),
    ['--non-interactive', '--profile', 'full', '--no-start'],
  )
})

