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

test('installer help lists source as default and docker as explicit mode', () => {
  const result = spawnSync(process.execPath, [cli, '--installer-help'], {
    encoding: 'utf8',
    env: cliEnvironment,
  })
  assert.equal(result.status, 0)
  assert.match(result.stdout, /--source\s+源码安装（默认）/)
  assert.match(result.stdout, /--docker\s+使用 Docker Compose 部署/)
  assert.match(result.stdout, /runtime\|rag\|vision\|full/)
})

function makeCheckout() {
  const root = mkdtempSync(join(tmpdir(), 'aigateway-installer-'))
  const scripts = join(root, 'scripts')
  mkdirSync(scripts)
  writeFileSync(join(scripts, 'quickstart.sh'), '#!/usr/bin/env bash\n', 'utf8')
  writeFileSync(join(scripts, 'install-source.sh'), '#!/usr/bin/env bash\n', 'utf8')
  return root
}

test('installer defaults to source mode and forwards source arguments', () => {
  const root = makeCheckout()
  const sourceInstaller = join(root, 'scripts', 'install-source.sh')
  const argsFile = join(root, 'received-args.txt')
  writeFileSync(
    sourceInstaller,
    `#!/usr/bin/env bash\nprintf '%s\\n' "$@" > "${argsFile}"\n`,
    'utf8',
  )
  chmodSync(sourceInstaller, 0o755)

  const result = spawnSync(process.execPath, [
    cli,
    '--dir',
    root,
    '--profile',
    'full',
    '--no-frontend',
  ], {
    encoding: 'utf8',
    env: cliEnvironment,
  })

  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(
    readFileSync(argsFile, 'utf8').trim().split('\n'),
    ['--profile', 'full', '--no-frontend'],
  )
  assert.match(result.stdout, /安装方式：源码安装/)
})

test('--docker invokes the Docker quickstart installer', () => {
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
    '--docker',
    '--profile',
    'full',
    '--build',
  ], {
    encoding: 'utf8',
    env: cliEnvironment,
  })

  assert.equal(result.status, 0, result.stderr)
  assert.deepEqual(
    readFileSync(argsFile, 'utf8').trim().split('\n'),
    ['--profile', 'full', '--build'],
  )
  assert.match(result.stdout, /安装方式：Docker Compose 部署/)
})

test('--source and --docker are mutually exclusive', () => {
  const result = spawnSync(process.execPath, [cli, '--source', '--docker'], {
    encoding: 'utf8',
    env: cliEnvironment,
  })
  assert.equal(result.status, 1)
  assert.match(result.stderr, /--source 与 --docker 不能同时使用/)
})
