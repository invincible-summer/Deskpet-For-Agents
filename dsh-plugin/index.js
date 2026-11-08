/**
 * dsh-deskpet — DeskPet launcher plugin for DeepSeek Harness.
 *
 * Role: an install/launch vehicle only. On activation it makes sure the
 * DeskPet Windows portable build exists (downloading it from the official
 * GitHub release with SHA-256 verification when missing) and starts it.
 * DeskPet itself keeps monitoring DSH sessions by passively reading
 * `$DSH_HOME/sessions` logs; this plugin never reads or writes session
 * data, credentials, or any other DSH-owned state.
 *
 * Contract notes (see dsh-plugin/README.md):
 * - A throwing `apply` kills the DSH host process, so every code path here
 *   is defensive: failures are logged through `ctx.logger` and swallowed.
 * - Non-Windows hosts are a logged no-op (DeskPet is Windows-only).
 * - DeskPet is never killed on plugin unload or DSH exit: it is a resident
 *   desktop pet monitoring every supported agent, not just DSH. Its own
 *   single-instance mutex deduplicates repeated launches.
 * - Zero runtime dependencies: Node builtins only. ZIP extraction uses
 *   Windows' bundled bsdtar (`tar.exe`, Windows 10 1803+).
 */

import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import {
  appendFileSync,
  createReadStream,
  createWriteStream,
  existsSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { Readable } from 'node:stream'
import { pipeline } from 'node:stream/promises'

export const name = 'dsh-deskpet'

const RELEASE_REPO = 'invincible-summer/Deskpet-For-Agents'
const ZIP_NAME = 'DeskPet-windows-x64-portable.zip'
const SUMS_NAME = 'SHA256SUMS.txt'
const EXE_NAME = 'DeskPet.exe'
const MANAGED_DIR_NAME = 'deskpet'
// Bounded download: the portable build is tens of MiB; cap generously so a
// redirected/huge response can never fill the disk.
const MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000
// Bounded walk depth when locating DeskPet.exe inside the extracted tree.
const MAX_FIND_DEPTH = 4
// Host stderr is only captured on crash, so the plugin keeps its own bounded
// trace under $DSH_HOME (same pattern as the upstream plugin manager's
// .plugin-manager/logs). Never writes outside its own namespace.
const LOG_NAME = 'deskpet-installer.log'
const MAX_LOG_BYTES = 64 * 1024

function appendLog(home, message) {
  try {
    const file = path.join(home, LOG_NAME)
    try {
      if (statSync(file).size > MAX_LOG_BYTES) {
        // Bounded: keep the newest half when over cap.
        writeFileSync(file, readFileSync(file, 'utf8').slice(-MAX_LOG_BYTES / 2))
      }
    } catch {
      // First write; the file does not exist yet.
    }
    appendFileSync(file, `${new Date().toISOString()} ${message}\n`, 'utf8')
  } catch {
    // Logging must never break the plugin.
  }
}

function log(ctx, level, message, error) {
  const logger = ctx && ctx.logger
  if (logger && typeof logger[level] === 'function') {
    if (error === undefined) logger[level](`[dsh-deskpet] ${message}`)
    else logger[level](`[dsh-deskpet] ${message}`, error)
  }
}

function dshHomeDir() {
  // Same precedence DeskPet itself uses (agents/paths.py dsh_home_root):
  // $DSH_HOME (resolved by the DSH launcher for the host process) > ~/.dsh.
  const fromEnv = process.env.DSH_HOME
  if (typeof fromEnv === 'string' && fromEnv.trim() !== '') {
    return path.resolve(fromEnv.trim())
  }
  return path.join(os.homedir(), '.dsh')
}

function managedInstallRoot(home) {
  return path.join(home, MANAGED_DIR_NAME)
}

// Config keys are optional; a user patch row replaces the whole config
// object, so every missing key falls back to the same defaults documented
// in cordis.patch.yml.
function readConfig(rawConfig) {
  const config =
    rawConfig && typeof rawConfig === 'object' && !Array.isArray(rawConfig)
      ? rawConfig
      : {}
  const autoStart = config.autoStart !== false
  let exePath = ''
  if (
    typeof config.exePath === 'string' &&
    path.isAbsolute(config.exePath.trim())
  ) {
    exePath = config.exePath.trim()
  }
  let releaseTag = 'latest'
  if (
    typeof config.releaseTag === 'string' &&
    config.releaseTag.trim() !== '' &&
    /^[A-Za-z0-9._-]+$/.test(config.releaseTag.trim())
  ) {
    releaseTag = config.releaseTag.trim()
  }
  return { autoStart, exePath, releaseTag }
}

function releaseBaseUrl(releaseTag) {
  if (releaseTag === 'latest') {
    return `https://github.com/${RELEASE_REPO}/releases/latest/download`
  }
  return `https://github.com/${RELEASE_REPO}/releases/download/${encodeURIComponent(
    releaseTag
  )}`
}

async function downloadToFile(url, targetPath) {
  if (typeof fetch !== 'function') {
    throw new Error('global fetch is unavailable in this host runtime')
  }
  const response = await fetch(url, {
    signal: AbortSignal.timeout(DOWNLOAD_TIMEOUT_MS),
    redirect: 'follow',
  })
  if (!response.ok) {
    throw new Error(`download failed: HTTP ${response.status} for ${url}`)
  }
  const declaredLength = Number(response.headers.get('content-length') || 0)
  if (declaredLength && declaredLength > MAX_DOWNLOAD_BYTES) {
    throw new Error(`download too large: ${declaredLength} bytes ( ${url} )`)
  }
  if (!response.body) {
    throw new Error(`download returned no body: ${url}`)
  }
  await pipeline(
    Readable.fromWeb(response.body),
    async function* bound(source) {
      let total = 0
      for await (const chunk of source) {
        total += chunk.length
        if (total > MAX_DOWNLOAD_BYTES) {
          throw new Error(`download exceeded ${MAX_DOWNLOAD_BYTES} bytes: ${url}`)
        }
        yield chunk
      }
    },
    createWriteStream(targetPath)
  )
}

function sha256File(filePath) {
  return new Promise((resolve, reject) => {
    const hash = createHash('sha256')
    const stream = createReadStream(filePath)
    stream.on('data', (chunk) => hash.update(chunk))
    stream.on('error', reject)
    stream.on('end', () => resolve(hash.digest('hex')))
  })
}

async function readFileText(filePath) {
  const { readFile } = await import('node:fs/promises')
  return readFile(filePath, 'utf8')
}

function expectedShaFromSums(sumsText) {
  for (const line of sumsText.split(/\r?\n/)) {
    const match = /^\s*([0-9a-fA-F]{64})\s+\*?(.+?)\s*$/.exec(line)
    if (match && match[2] === ZIP_NAME) {
      return match[1].toLowerCase()
    }
  }
  return null
}

// Invoke Windows' bundled bsdtar by absolute path: PATH lookup can resolve
// to Git's MSYS GNU tar (<Git>\usr\bin\tar.exe), which cannot read ZIP
// archives ("Skipping to next header"). Verified on a real machine against
// the actual release archive.
function windowsTarPath() {
  const root = process.env.SystemRoot || process.env.WINDIR
  if (typeof root === 'string' && root.trim() !== '') {
    const candidate = path.join(root, 'System32', 'tar.exe')
    if (existsSync(candidate)) return candidate
  }
  return 'tar'
}

function runTarExtract(zipPath, workDir, extractDirName) {
  return new Promise((resolve, reject) => {
    // Windows bsdtar parses drive-letter paths (`C:\...`) as rsh-style
    // remote specs ("Cannot connect to C"), so no absolute path may reach
    // its command line: run from workDir with the archive basename and a
    // relative -C target.
    const child = spawn(
      windowsTarPath(),
      ['-xf', path.basename(zipPath), '-C', extractDirName],
      { cwd: workDir, stdio: ['ignore', 'ignore', 'pipe'], windowsHide: true }
    )
    let stderr = ''
    child.on('error', reject)
    if (child.stderr) {
      child.stderr.on('data', (chunk) => {
        if (stderr.length < 4096) stderr += String(chunk)
      })
    }
    child.on('close', (code) => {
      if (code === 0) resolve()
      else reject(new Error(`tar exited with ${code}: ${stderr.trim()}`))
    })
  })
}

// Locate the folder that directly contains DeskPet.exe inside the extracted
// tree (the portable ZIP ships a top-level DeskPet/ directory, but stay
// layout-tolerant with a bounded walk).
function findExeDir(rootDir) {
  const stack = [{ dir: rootDir, depth: 0 }]
  while (stack.length > 0) {
    const { dir, depth } = stack.pop()
    let entries
    try {
      entries = readdirSync(dir, { withFileTypes: true })
    } catch {
      continue
    }
    for (const entry of entries) {
      if (entry.isFile() && entry.name.toLowerCase() === EXE_NAME.toLowerCase()) {
        return dir
      }
      if (entry.isDirectory() && depth < MAX_FIND_DEPTH) {
        stack.push({ dir: path.join(dir, entry.name), depth: depth + 1 })
      }
    }
  }
  return null
}

// Opportunistic cleanup of temp dirs left behind by a host that died
// mid-install. Only touch entries older than an hour so a concurrent
// in-flight install (CLI + Desktop profiles share $DSH_HOME) is never
// disturbed.
function sweepStaleTempDirs(home) {
  const prefixes = ['.deskpet-download-', '.deskpet-old-']
  const cutoff = Date.now() - 60 * 60 * 1000
  let entries
  try {
    entries = readdirSync(home, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    if (!prefixes.some((prefix) => entry.name.startsWith(prefix))) continue
    const full = path.join(home, entry.name)
    try {
      if (statSync(full).mtimeMs < cutoff) {
        rmSync(full, { recursive: true, force: true })
      }
    } catch {
      // Best-effort only; never block installation on cleanup.
    }
  }
}

async function ensureManagedInstall(home, releaseTag, ctx) {
  const installRoot = managedInstallRoot(home)
  const managedExe = path.join(installRoot, EXE_NAME)
  if (existsSync(managedExe)) {
    appendLog(home, `already installed at ${managedExe}`)
    return managedExe
  }
  sweepStaleTempDirs(home)
  const base = releaseBaseUrl(releaseTag)
  const workDir = path.join(home, `.deskpet-download-${Date.now()}`)
  mkdirSync(workDir, { recursive: true })
  try {
    const zipPath = path.join(workDir, ZIP_NAME)
    const sumsPath = path.join(workDir, SUMS_NAME)
    log(ctx, 'info', `downloading DeskPet portable from ${base}`)
    appendLog(home, `downloading ${ZIP_NAME} from ${base}`)
    await downloadToFile(`${base}/${ZIP_NAME}`, zipPath)
    await downloadToFile(`${base}/${SUMS_NAME}`, sumsPath)
    const actualSha = await sha256File(zipPath)
    const expectedSha = expectedShaFromSums(await readFileText(sumsPath))
    if (!expectedSha) {
      throw new Error(`${SUMS_NAME} has no entry for ${ZIP_NAME}`)
    }
    if (actualSha !== expectedSha) {
      throw new Error(
        `sha256 mismatch for ${ZIP_NAME}: expected ${expectedSha}, got ${actualSha}`
      )
    }
    appendLog(home, `sha256 verified (${actualSha})`)
    const extractDir = path.join(workDir, 'extracted')
    mkdirSync(extractDir, { recursive: true })
    await runTarExtract(zipPath, workDir, 'extracted')
    const exeDir = findExeDir(extractDir)
    if (!exeDir) {
      throw new Error(`${EXE_NAME} not found inside the downloaded archive`)
    }
    if (existsSync(installRoot)) {
      // Replace a stale/partial install; rename-aside keeps removal atomic.
      const aside = path.join(home, `.deskpet-old-${Date.now()}`)
      renameSync(installRoot, aside)
      rmSync(aside, { recursive: true, force: true })
    }
    renameSync(exeDir, installRoot)
    if (!existsSync(managedExe)) {
      throw new Error(`install finished but ${managedExe} is missing`)
    }
    log(ctx, 'info', `DeskPet installed at ${installRoot}`)
    appendLog(home, `installed at ${installRoot}`)
    return managedExe
  } finally {
    rmSync(workDir, { recursive: true, force: true })
  }
}

function launchDeskPet(exePath, home, ctx) {
  const child = spawn(exePath, [], {
    cwd: path.dirname(exePath),
    env: { ...process.env, DSH_HOME: home },
    detached: true,
    stdio: 'ignore',
    windowsHide: true,
  })
  child.unref()
  // The exe location doubles as a light sanity check for logs.
  log(ctx, 'info', `DeskPet launched from ${exePath}`)
  appendLog(home, `launched ${exePath} (pid ${child.pid})`)
}

export function apply(ctx, rawConfig) {
  try {
    if (process.platform !== 'win32') {
      log(ctx, 'info', 'DeskPet is Windows-only; plugin is a no-op here')
      return
    }
    const config = readConfig(rawConfig)
    const home = dshHomeDir()
    if (!config.autoStart) {
      log(ctx, 'info', 'autoStart disabled; nothing to do')
      appendLog(home, 'apply: autoStart disabled, no-op')
      return
    }
    appendLog(
      home,
      `apply: autoStart enabled (exePath=${JSON.stringify(config.exePath)}, ` +
        `releaseTag=${JSON.stringify(config.releaseTag)}, home=${home})`
    )
    void (async () => {
      try {
        let exePath
        if (config.exePath !== '') {
          exePath = config.exePath
          if (!existsSync(exePath) || !statSync(exePath).isFile()) {
            log(ctx, 'error', `configured exePath does not exist: ${exePath}`)
            appendLog(home, `error: configured exePath does not exist: ${exePath}`)
            return
          }
        } else {
          exePath = await ensureManagedInstall(home, config.releaseTag, ctx)
        }
        launchDeskPet(exePath, home, ctx)
      } catch (error) {
        log(ctx, 'error', 'failed to start DeskPet', error)
        appendLog(home, `error: ${error && error.stack ? error.stack : String(error)}`)
      }
    })()
  } catch (error) {
    // Never propagate: a failing apply kills the DSH host process.
    log(ctx, 'error', 'unexpected failure in dsh-deskpet', error)
    try {
      appendLog(dshHomeDir(), `error (sync): ${String(error && error.stack ? error.stack : error)}`)
    } catch {
      // Best effort only.
    }
  }
}
