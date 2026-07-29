import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { chmodSync, mkdtempSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const cli = join(packageRoot, 'bin', 'aigateway-install.js')
const cliEnvironment = { ...process.env }
delete cliEnvironment.NODE_TEST_CONTEXT

test('installer help exposes editions and image/source distributions', () => {
  const source = readFileSync(cli, 'utf8')
  assert.match(source, /lite\|knowledge\|studio\|full/)
  assert.match(source, /image\|source/)
})

function makeCheckout() {
  const root = mkdtempSync(join(tmpdir(), 'aigateway-installer-'))
  const scripts = join(root, 'scripts')
  mkdirSync(scripts)
  writeFileSync(join(scripts, 'quickstart.sh'), '#!/usr/bin/env bash\n', 'utf8')
  return root
}

test('installer invokes quickstart and forwards the new public interface', () => {
  const root = makeCheckout()
  const quickstart = join(root, 'scripts', 'quickstart.sh')
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
    '--edition',
    'full',
    '--distribution',
    'source',
    '--build',
  ], {
    encoding: 'utf8',
    env: cliEnvironment,
  })

  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(
    readFileSync(argsFile, 'utf8').trim().split('\n'),
    ['--edition', 'full', '--distribution', 'source', '--build'],
  )
})

test('legacy source and docker switches are rejected with migration guidance', () => {
  const result = spawnSync(process.execPath, [cli, '--source'], {
    encoding: 'utf8',
    env: cliEnvironment,
  })
  assert.equal(result.status, 1)
})
