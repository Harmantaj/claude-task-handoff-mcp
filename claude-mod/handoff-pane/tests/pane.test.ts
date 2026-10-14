import { expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const USAGE = { input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }

const PANE_JSON = {
  repo: '/repo',
  name: 'shop',
  state_dir: '/repo/.git/task-handoff',
  baseline: { task: 'session', created_at: '2026-10-14T01:00:00+0530', head: 'abc123', is_session: true },
  changes: [{ path: 'shop/pricing.py', status: 'modified', added: 2, removed: 1 }],
  pre_existing_unrelated: [],
  diff: 'diff --git a/shop/pricing.py b/shop/pricing.py\nindex 1..2 100644\n--- a/shop/pricing.py\n+++ b/shop/pricing.py\n@@ -1,2 +1,2 @@\n def f():\n-    return 1\n+    return 2\n',
  diff_omitted: [],
  report: {
    generated_at: '2026-10-14T01:10:00+0530',
    verdict: 'FAILED',
    checks: [{ id: 'pytest', outcome: 'failed', detail: '1 failed', excerpt: ['assert 100 == 90'] }],
    findings: [{ evidence: 'NOT TESTED', text: 'No passing test exercised: shop/pricing.py' }],
    risks: [],
    next_prompt: 'Fix the failing check(s): pytest.',
  },
  stale: true,
}

const PANE_PROPS = {
  title: 'Handoff',
  isFocused: false,
  bodyColumns: 80,
  placement: 'dock' as const,
  scroll: { offset: 0, bodyRows: 40 },
  view: {},
}

type Run = { exitCode: number; stdout: string; stderr: string }
const ok = (stdout: string): Run => ({ exitCode: 0, stdout, stderr: '' })

// The engine beneath the plugin: what it would answer for this session ($ calls answer { value }).
function engine(on: On, calls: string[][], run?: (argv: readonly string[]) => Run) {
  mock.store(on)
  mock.env(on, { HOME: '/home/me' })
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('session.id', () => ({ value: 's1' }))
  on('session.cwd', () => ({ value: '/repo' }))
  on('session.root', () => ({ value: '/repo' }))
  on('session.turns', () => ({ value: 2 }))
  on('settings.read', () => ({
    value: { hooks: { Stop: [{ hooks: [{ command: '"/opt/th/bin/task-handoff" hook stop' }] }] } },
  }))
  on('command.register', () => ({ value: { command: 'handoff' } }))
  on('ui.open', () => ({ value: { isPlaced: true as const } }))
  on('ui.panes', () => ({
    value: [{ id: 'handoff', title: 'Handoff', isShown: true, isFocused: false, isPlaced: true, plugin: 'handoff-pane' }],
  }))
  on('fs.read', ($, e) => {
    if (e.path === '/repo/README.md') return { value: '# Shop\nA tiny shop backend.' }
    return { deny: 'ENOENT' }
  })
  on('fs.stat', () => ({ value: { kind: 'file' as const, size: 10, mtimeMs: 5, isLink: false } }))
  on('process.run', ($, e) => {
    calls.push([...e.argv])
    const out = run?.(e.argv) ?? ok(e.argv.includes('pane') ? JSON.stringify(PANE_JSON) : 'shop/pricing.py\n')
    return { value: { ...out, isStdoutTruncated: false, isStderrTruncated: false } }
  })
  on('model.complete', () => ({
    value: {
      isAnswered: true as const,
      text: '<short>A small shop backend.</short><detail>It prices orders; tests use pytest.</detail>',
      usage: USAGE,
    },
  }))
  on('model.fork', () => ({
    value: {
      isAnswered: true as const,
      text: '<short>- **Goal:** cap discounts at 50%</short><detail>### Goal\nCap discounts.\n### Decisions and why\nUse min().</detail>',
      usage: USAGE,
    },
  }))
}

test('the pane shows project, session and verification, short then detailed', async ($, on) => {
  const calls: string[][] = []
  const clock = mock.clock(on)
  engine(on, calls)

  await $.session.start({ cwd: '/repo' } as never)
  await clock.advance(100)

  expect(calls[0]).toEqual(['/opt/th/bin/task-handoff', 'pane', '/repo', '--session', 's1'])

  for (const surface of ['desktop', 'terminal'] as const) {
    const ui = await $.ui.mount({ plugin: 'handoff-pane', surface, component: 'Pane', requestId: 'handoff', props: PANE_PROPS })

    expect((await ui.find({ key: 'project' }))?.props.text).toBe('A small shop backend.')
    expect((await ui.find({ key: 'session' }))?.props.text).toBe('- **Goal:** cap discounts at 50%')
    const verify = String((await ui.find({ key: 'verify' }))?.props.text)
    expect(verify).toContain('**Verdict: FAILED**')
    expect(verify).toContain('**Out of date:**')
    expect(verify).toContain('`pytest` FAIL')
    expect(await ui.find({ type: 'Code' })).toBeUndefined()

    await ui.press({ key: 'toggle' })

    expect((await ui.find({ key: 'project' }))?.props.text).toBe('It prices orders; tests use pytest.')
    expect(String((await ui.find({ key: 'session' }))?.props.text)).toContain('### Decisions and why')
    expect(String((await ui.find({ key: 'verify' }))?.props.text)).toContain('[NOT TESTED] No passing test exercised')
    const code = await ui.find({ type: 'Code' })
    expect(String(code?.props.source).startsWith('@@ -1,2 +1,2 @@')).toBe(true)
    expect(code?.props.format).toBe('diff')

    await ui.press({ key: 'toggle' })
    await ui.unmount()
  }
})

test('a failing task-handoff command is shown as an error, never as a pass', async ($, on) => {
  const clock = mock.clock(on)
  engine(on, [], argv =>
    argv.includes('pane') ? { exitCode: 3, stdout: '', stderr: 'error: Not inside a git repository: /tmp/x' } : ok(''))

  await $.session.start({ cwd: '/repo' } as never)
  await clock.advance(100)

  const ui = await $.ui.mount({ plugin: 'handoff-pane', surface: 'desktop', component: 'Pane', requestId: 'handoff', props: PANE_PROPS })
  const verify = String((await ui.find({ key: 'verify' }))?.props.text)
  expect(verify).toContain('No verification data: error: Not inside a git repository')
  expect(verify).not.toContain('Verdict')
})

test('Refresh answers the press at once and refreshes afterwards', async ($, on) => {
  const calls: string[][] = []
  const clock = mock.clock(on)
  engine(on, calls)

  await $.session.start({ cwd: '/repo' } as never)
  await clock.advance(100)
  const before = calls.length

  const ui = await $.ui.mount({ plugin: 'handoff-pane', surface: 'desktop', component: 'Pane', requestId: 'handoff', props: PANE_PROPS })
  await ui.press({ key: 'refresh' })
  expect(calls.length).toBe(before)

  await clock.advance(10)
  expect(calls.slice(before).some(argv => argv.includes('pane'))).toBe(true)
  await ui.unmount()
})
