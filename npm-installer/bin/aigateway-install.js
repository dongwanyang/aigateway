#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import {
  accessSync,
  constants,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  statSync,
} from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const DEFAULT_REPOSITORY = 'https://github.com/dongwanyang/aigateway.git'
const DEFAULT_REF = 'main'

function printHelp() {
  process.stdout.write(`
AI Gateway npm 安装器

用法:
  aigateway-install
  aigateway-install --profile full --accelerator cuda --build
  npx aigateway-installer --add rag --build

npm 安装器选项:
  --dir <path>       安装或使用指定目录（默认 ~/.aigateway/runtime）
  --repo <url>       Git 仓库地址
  --ref <ref>        Git 分支或标签（默认 main）
  --installer-help   显示本帮助
  --version          显示 npm 安装器版本

其他参数会原样传给 scripts/quickstart.sh，例如:
  --profile runtime|rag|vision|full
  --add rag|vision|gpu
  --remove rag|vision|gpu
  --accelerator cpu|cuda
  --monitoring
  --non-interactive
  --build
  --no-start
  --show-plan
  --down
`)
}

function fail(message) {
  process.stderr.write(`\n安装失败：${message}\n`)
  process.exit(1)
}

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    stdio: 'inherit',
    ...options,
  })
}

function commandExists(command) {
  if (process.platform === 'win32') {
    return spawnSync('where', [command], { stdio: 'ignore' }).status === 0
  }
  for (const entry of (process.env.PATH || '').split(delimiter)) {
    if (!entry) continue
    try {
      accessSync(join(entry, command), constants.X_OK)
      return true
    } catch {
      // Keep searching PATH.
    }
  }
  return false
}

function isGatewayCheckout(directory) {
  return existsSync(join(directory, 'scripts', 'quickstart.sh'))
}

function parseArguments(argv) {
  let installDirectory = null
  let repository = process.env.AIGATEWAY_INSTALL_REPOSITORY || DEFAULT_REPOSITORY
  let ref = process.env.AIGATEWAY_INSTALL_REF || DEFAULT_REF
  const quickstartArgs = []

  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === '--dir' || arg === '--repo' || arg === '--ref') {
      const value = argv[index + 1]
      if (!value) fail(`${arg} 缺少值`)
      if (arg === '--dir') installDirectory = resolve(value)
      if (arg === '--repo') repository = value
      if (arg === '--ref') ref = value
      index += 1
    } else {
      quickstartArgs.push(arg)
    }
  }

  return { installDirectory, repository, ref, quickstartArgs }
}

function packageVersion() {
  const currentFile = fileURLToPath(import.meta.url)
  const packageFile = join(dirname(dirname(currentFile)), 'package.json')
  return JSON.parse(readFileSync(packageFile, 'utf8')).version
}

function resolveInstallDirectory(explicitDirectory) {
  if (explicitDirectory) return explicitDirectory
  if (isGatewayCheckout(process.cwd())) return process.cwd()
  return resolve(
    process.env.AIGATEWAY_INSTALL_DIR || join(homedir(), '.aigateway', 'runtime'),
  )
}

function ensureCheckout(directory, repository, ref) {
  if (isGatewayCheckout(directory)) {
    process.stdout.write(`使用现有 AI Gateway：${directory}\n`)
    return
  }

  if (existsSync(directory)) {
    let nonEmpty = true
    try {
      nonEmpty = !statSync(directory).isDirectory() || readdirSync(directory).length > 0
    } catch {
      nonEmpty = true
    }
    if (nonEmpty) {
      fail(`${directory} 已存在但不是 AI Gateway 仓库；请使用 --dir 指定空目录`)
    }
  }

  if (!commandExists('git')) fail('未找到 git，请先安装 Git')
  mkdirSync(dirname(directory), { recursive: true })
  process.stdout.write(`下载 AI Gateway (${ref}) 到 ${directory}...\n`)
  const clone = run('git', [
    'clone',
    '--depth',
    '1',
    '--branch',
    ref,
    repository,
    directory,
  ])
  if (clone.status !== 0) fail('Git 下载失败，请检查网络、仓库地址或 --ref')
  if (!isGatewayCheckout(directory)) fail('下载完成，但仓库缺少 scripts/quickstart.sh')
}

export function main(argv = process.argv.slice(2)) {
  if (argv.includes('--installer-help') || argv.includes('--help') || argv.includes('-h')) {
    printHelp()
    return 0
  }
  if (argv.includes('--version')) {
    process.stdout.write(`${packageVersion()}\n`)
    return 0
  }
  if (process.platform === 'win32' && !process.env.WSL_DISTRO_NAME) {
    fail('Windows 请在 WSL2 中运行，当前安装器需要 Bash 和 Docker Compose')
  }
  if (!commandExists('bash')) fail('未找到 Bash')

  const parsed = parseArguments(argv)
  const installDirectory = resolveInstallDirectory(parsed.installDirectory)
  ensureCheckout(installDirectory, parsed.repository, parsed.ref)

  process.stdout.write(`
AI Gateway 安装目录：${installDirectory}
接下来请选择要安装的能力。安装器可重复运行，不会自动删除数据卷。

`)
  const result = run(
    'bash',
    [join(installDirectory, 'scripts', 'quickstart.sh'), ...parsed.quickstartArgs],
    { cwd: installDirectory },
  )
  return result.status ?? 1
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : ''
if (invokedPath === fileURLToPath(import.meta.url)) {
  process.exitCode = main()
}
