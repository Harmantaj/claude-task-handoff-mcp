import type { EngineInterface, Register } from 'claude-code'

import type { PaneData, Summary, Verification } from '../types'
import {
  cliFromSettings,
  projectPrompt,
  sessionPrompt,
  splitCommand,
  splitDiff,
  splitSummary,
  verificationMarkdown,
} from './text'

type Engine = EngineInterface

const PANE = 'handoff'
const TITLE = 'Handoff'
const IDLE: Summary = { status: 'idle', short: '', detail: '' }

// What the pane draws. The module keeps its own copy of each (below), since every
// `$.state.get` of one dispatch reads one moment and would miss this module's own writes.
const IS_EXPANDED = { plugin: 'handoff-pane', key: 'isExpanded' } as const
const PROJECT = { plugin: 'handoff-pane', key: 'project' } as const
const SESSION = { plugin: 'handoff-pane', key: 'session' } as const
const VERIFICATION = { plugin: 'handoff-pane', key: 'verification' } as const

let cliOption = ''
let project: Summary = IDLE
let session: Summary = IDLE
let verification: Verification = { status: 'idle' }
let lastAnswer = ''
let reportMtime = 0
// One refresh per kind at a time; a request made while one runs reruns it once afterwards.
const busy = new Set<string>()
const again = new Set<string>()

const errorText = (err: unknown) => (err instanceof Error ? err.message : String(err))

async function showProject($: Engine, change: Partial<Summary>) {
  project = { ...project, ...change }
  await $.state.set(PROJECT, project)
}

async function showSession($: Engine, change: Partial<Summary>) {
  session = { ...session, ...change }
  await $.state.set(SESSION, session)
}

async function showVerification($: Engine, change: Partial<Verification>) {
  verification = { ...verification, ...change }
  await $.state.set(VERIFICATION, verification)
}

async function toggleDetail($: Engine) {
  const { value = false } = await $.state.get(IS_EXPANDED)
  await $.state.set(IS_EXPANDED, !value)
}

async function cliArgv($: Engine): Promise<string[]> {
  if (cliOption) return splitCommand(cliOption)
  const home = (await $.env.get('HOME')) ?? ''
  return cliFromSettings(await $.settings.read(), home) ?? ['task-handoff']
}

async function verifyOnce($: Engine) {
  await showVerification($, { status: 'working' })
  try {
    const argv = await cliArgv($)
    const ran = await $.process.run(
      [...argv, 'pane', await $.session.cwd(), '--session', await $.session.id()],
      { timeoutMs: 60_000 },
    )
    if (ran.exitCode !== 0) {
      await showVerification($, { status: 'error', error: ran.stderr.trim() || `exit code ${ran.exitCode}` })
      return
    }
    const data = JSON.parse(ran.stdout) as PaneData
    const stat = await $.fs.stat(`${data.state_dir}/last_report.json`).catch(() => undefined)
    reportMtime = stat?.mtimeMs ?? 0
    await showVerification($, { status: 'ready', data, error: undefined, updatedAt: await $.clock.now() })
  } catch (err) {
    await showVerification($, { status: 'error', error: errorText(err) })
  }
}

async function readFirst($: Engine, root: string, names: string[], cap: number) {
  for (const file of names) {
    const text = await $.fs.read(`${root}/${file}`).catch(() => undefined)
    if (typeof text === 'string') return text.slice(0, cap)
  }
  return ''
}

async function projectOnce($: Engine) {
  try {
    const root = verification.data?.repo ?? (await $.session.root())
    const name = root.split('/').filter(Boolean).at(-1) ?? root
    const readme = await readFirst($, root, ['README.md', 'readme.md', 'README.rst', 'README.txt', 'README'], 8000)
    const claudeMd = await readFirst($, root, ['CLAUDE.md', 'AGENTS.md'], 3000)
    const listed = await $.process.run(['git', 'ls-files'], { cwd: root }).catch(() => undefined)
    const files = (listed?.exitCode === 0 ? listed.stdout : '').split('\n').slice(0, 150).join('\n')
    const prompt = projectPrompt(name, readme, claudeMd, files)

    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(prompt))
    const hash = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
    const cached = (await $.store.get(`project:${root}`)) as (Summary & { hash?: string }) | undefined
    if (cached?.hash === hash) {
      await showProject($, { ...cached, status: 'ready' })
      return
    }

    await showProject($, { status: 'working' })
    const reply = await $.model.complete({ model: 'haiku', prompt, maxTokens: 900 })
    if (!reply.isAnswered) {
      await showProject($, { status: 'error', error: `model call failed (${reply.reason})` })
      return
    }
    const done: Summary = { status: 'ready', ...splitSummary(reply.text), error: undefined, updatedAt: await $.clock.now() }
    await $.store.set(`project:${root}`, { ...done, hash })
    await showProject($, done)
  } catch (err) {
    await showProject($, { status: 'error', error: errorText(err) })
  }
}

async function sessionOnce($: Engine) {
  try {
    if ((await $.session.turns()) === 0) {
      await showSession($, { ...IDLE, short: 'Nothing has happened in this session yet.' })
      return
    }
    await showSession($, { status: 'working' })
    const reply = await $.model.fork({ prompt: sessionPrompt(lastAnswer) })
    if (!reply.isAnswered) {
      await showSession($, {
        status: reply.reason === 'nothing-to-fork' ? 'idle' : 'error',
        error: `summary not available (${reply.reason})`,
      })
      return
    }
    const done: Summary = { status: 'ready', ...splitSummary(reply.text), error: undefined, updatedAt: await $.clock.now() }
    await $.store.set(`session:${await $.session.id()}`, done)
    await showSession($, done)
  } catch (err) {
    await showSession($, { status: 'error', error: errorText(err) })
  }
}

async function refreshVerification($: Engine) {
  if (busy.has('verification')) return void again.add('verification')
  busy.add('verification')
  try {
    do {
      again.delete('verification')
      await verifyOnce($)
    } while (again.has('verification'))
  } finally {
    busy.delete('verification')
  }
}

async function refreshProject($: Engine) {
  if (busy.has('project')) return void again.add('project')
  busy.add('project')
  try {
    do {
      again.delete('project')
      await projectOnce($)
    } while (again.has('project'))
  } finally {
    busy.delete('project')
  }
}

async function refreshSession($: Engine) {
  if (busy.has('session')) return void again.add('session')
  busy.add('session')
  try {
    do {
      again.delete('session')
      await sessionOnce($)
    } while (again.has('session'))
  } finally {
    busy.delete('session')
  }
}

async function refreshAll($: Engine) {
  await refreshVerification($)
  await Promise.all([refreshProject($), refreshSession($)])
}

async function startUp($: Engine, hasSaved: boolean) {
  await refreshVerification($)
  await refreshProject($)
  if (!hasSaved) await refreshSession($)
}

// Picks up verify_task runs made through the MCP tools and the Stop hook's automatic check.
async function pollReport($: Engine) {
  const dir = verification.data?.state_dir
  if (!dir || busy.has('verification')) return
  const stat = await $.fs.stat(`${dir}/last_report.json`).catch(() => undefined)
  if (stat && stat.mtimeMs !== reportMtime) await refreshVerification($)
}

async function isPaneOpen($: Engine) {
  return (await $.ui.panes()).some(pane => pane.id === PANE)
}

export const register: Register = (on, options) => {
  cliOption = String(options.cli ?? '').trim()

  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'handoff',
      description: 'Open the Handoff pane: project summary, session summary, diff and verification',
    })
    // A hot reload starts the module over: pick up what the pane already showed.
    project = (await $.state.get(PROJECT)).value ?? IDLE
    verification = (await $.state.get(VERIFICATION)).value ?? { status: 'idle' }
    const saved = (await $.store.get(`session:${await $.session.id()}`)) as Summary | undefined
    session = (await $.state.get(SESSION)).value ?? saved ?? IDLE
    if (saved) await showSession($, saved)
    void $.ui.open({ id: PANE, title: TITLE })

    $.clock.after(50, () => void startUp($, saved !== undefined))
    $.clock.every(4000, () => void pollReport($))

    return next(e)
  })

  on('command.run', { command: 'handoff' }, async $ => {
    await $.ui.open({ id: PANE, title: TITLE })
    $.clock.after(10, () => void refreshAll($))
    return { text: 'Handoff pane opened.' }
  })

  on('turn.complete', async ($, e, next) => {
    if (e.agentId === undefined && !e.isAborted && (await isPaneOpen($))) {
      lastAnswer = e.answer
      $.clock.after(10, () => void refreshAll($))
    }
    return next(e)
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Markdown, Code } = $.ui.resolve(e)
    const { value: expanded = false } = await $.state.get(IS_EXPANDED)
    const { value: p = IDLE } = await $.state.get(PROJECT)
    const { value: s = IDLE } = await $.state.get(SESSION)
    const { value: v = { status: 'idle' } } = await $.state.get(VERIFICATION)

    const summaryText = (sum: Summary, waiting: string) => {
      const text = expanded ? sum.detail || sum.short : sum.short
      if (text) return sum.status === 'error' ? `${text}\n\n_Last refresh failed: ${sum.error}_` : text
      if (sum.status === 'error') return `_Could not summarise: ${sum.error}_`
      return `_${waiting}_`
    }
    const heading = (label: string, status: string) => (
      <Text bold>
        {label}
        {status === 'working' ? <Text dimColor> · updating</Text> : ''}
      </Text>
    )
    const verifyText = v.data
      ? verificationMarkdown(v.data, expanded) + (v.status === 'error' ? `\n\n_Last refresh failed: ${v.error}_` : '')
      : v.status === 'error'
        ? `_No verification data: ${v.error}_`
        : '_Reading the repository..._'
    const files = expanded && v.data ? splitDiff(v.data.diff) : []

    return (
      <Box flexDirection="column" gap={1}>
        <Box flexDirection="row" gap={1}>
          <Button
            key="toggle"
            variant="primary"
            label={expanded ? 'Less detail' : 'More detail'}
            onPress={() => toggleDetail($)}
          />
          <Button key="refresh" label="Refresh" onPress={() => refreshAll($)} />
        </Box>
        {heading('What this project is', p.status)}
        <Markdown key="project" text={summaryText(p, 'Reading the README...')} />
        {heading('This session so far', s.status)}
        <Markdown key="session" text={summaryText(s, 'Summarising the conversation...')} />
        {heading('Changes and verification', v.status)}
        <Markdown key="verify" text={verifyText} />
        {files.map(file => (
          <Box key={`diff:${file.path}`} flexDirection="column">
            <Text dimColor>{file.path}</Text>
            <Code source={file.hunks} format="diff" path={file.path} />
          </Box>
        ))}
      </Box>
    )
  })
}
