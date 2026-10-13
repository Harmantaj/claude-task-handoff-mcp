// Pure helpers: prompts, parsing model replies and the CLI command, and the verification markdown.
import type { PaneData } from '../types'

export const SHORT_CHARS = 1200

/** Splits a reply written as <short>...</short><detail>...</detail>; a reply without tags is all detail. */
export function splitSummary(text: string): { short: string; detail: string } {
  const pick = (tag: string) => text.match(new RegExp(`<${tag}>([\\s\\S]*?)(?:</${tag}>|$)`))?.[1]?.trim()
  const short = pick('short')
  const detail = pick('detail')
  if (short !== undefined || detail !== undefined) {
    return { short: (short ?? detail ?? '').slice(0, SHORT_CHARS), detail: detail ?? short ?? '' }
  }
  const trimmed = text.trim()
  return { short: trimmed.split('\n').slice(0, 5).join('\n').slice(0, SHORT_CHARS), detail: trimmed }
}

export function projectPrompt(name: string, readme: string, claudeMd: string, files: string): string {
  return [
    `Explain the software project in the folder "${name}" to someone who has never seen it, in plain English.`,
    'The folder name is only where it lives: call the project by the name its README uses, without remarking on the folder.',
    'Use only the material below; if something is unclear from it, say so rather than guessing.',
    '',
    'Answer in exactly this shape:',
    '<short>Two sentences at most: what it is and what it is for.</short>',
    '<detail>Markdown, under 250 words: what it does, who it is for, how it is used, the main parts',
    '(name the key folders/files), and how it is built and tested. Define any jargon.</detail>',
    '',
    '--- README ---',
    readme || '(no README)',
    '--- CLAUDE.md ---',
    claudeMd || '(none)',
    '--- tracked files (first 150) ---',
    files || '(none)',
  ].join('\n')
}

export function sessionPrompt(lastAnswer: string): string {
  return [
    'Stop the task. Do not call any tools. Write a handoff note about THIS conversation so far for a',
    'person who has not read it. Plain English, complete sentences, no invented facts: if something',
    'was not stated, say "not stated". Distinguish what was verified (ran or read) from what was only claimed.',
    lastAnswer
      ? `The assistant's final reply of the latest turn, which may be missing above:\n"""\n${lastAnswer.slice(0, 4000)}\n"""`
      : '',
    '',
    'Answer in exactly this shape:',
    '<short>3 to 5 markdown bullets, one line each, starting "**Goal:**", "**Decisions:**",',
    '"**Changed:**", "**Status:**" (and "**Open:**" if anything is unresolved).</short>',
    '<detail>Markdown with these sections: "### Goal", "### What happened" (in order),',
    '"### Decisions and why", "### What changed and why" (files and reasons), "### Verification",',
    '"### Open questions and next steps". Enough that a newcomer can continue the work.</detail>',
  ].join('\n')
}

/** Splits a shell command line into words, honouring single and double quotes. */
export function splitCommand(line: string): string[] {
  const words: string[] = []
  const re = /"((?:[^"\\]|\\.)*)"|'([^']*)'|(\S+)/g
  for (let m = re.exec(line); m !== null; m = re.exec(line)) {
    words.push(m[1] !== undefined ? m[1].replace(/\\(.)/g, '$1') : (m[2] ?? m[3] ?? ''))
  }
  return words
}

/**
 * The argv that runs task-handoff, read off a Stop hook command such as
 * `"/x/.venv/bin/task-handoff" hook stop` or `python3 ~/clone/run_cli.py hook stop`.
 */
export function cliFromSettings(settings: unknown, home: string): string[] | undefined {
  const stops = (settings as { hooks?: { Stop?: { hooks?: { command?: unknown }[] }[] } })?.hooks?.Stop ?? []
  for (const group of stops) {
    for (const hook of group.hooks ?? []) {
      if (typeof hook.command !== 'string' || !/task-handoff|run_cli\.py/.test(hook.command)) continue
      const words = splitCommand(hook.command).map(w => (w.startsWith('~/') ? home + w.slice(1) : w))
      const at = words.lastIndexOf('hook')
      if (at > 0) return words.slice(0, at)
    }
  }
  return undefined
}

const OUTCOME: Record<string, string> = { passed: 'PASS', failed: 'FAIL', blocked: 'BLOCKED', not_tested: 'NOT TESTED' }
const outcome = (o: string) => OUTCOME[o] ?? o.toUpperCase()

export function verificationMarkdown(data: PaneData, isExpanded: boolean): string {
  const lines: string[] = []
  const report = data.report
  const added = data.changes.reduce((n, c) => n + c.added, 0)
  const removed = data.changes.reduce((n, c) => n + c.removed, 0)
  const changed = data.changes.length === 0
    ? 'No changes since this session started.'
    : `${data.changes.length} file(s) changed since this session started (+${added}/-${removed}).`

  if (report === null) {
    lines.push('**Not verified yet.** No verification report exists for this repository.', changed)
  } else {
    lines.push(`**Verdict: ${report.verdict}**${report.generated_at ? ` · checked ${report.generated_at.replace('T', ' ')}` : ''}`)
    if (data.stale) {
      lines.push('', '**Out of date:** the code changed after this check. The checks below were not re-run.')
    }
    if (report.checks.length > 0) {
      lines.push('', 'Checks: ' + report.checks.map(c => `\`${c.id}\` ${outcome(c.outcome)}`).join(', '))
    }
    lines.push('', changed)
  }

  const shown = isExpanded ? data.changes : data.changes.slice(0, 5)
  for (const c of shown) {
    lines.push(`- \`${c.path}\` ${c.status} +${c.added}/-${c.removed}${c.note ? ` (${c.note})` : ''}`)
  }
  if (shown.length < data.changes.length) lines.push(`- ...and ${data.changes.length - shown.length} more`)

  if (!isExpanded) return lines.join('\n')

  if (data.baseline) {
    lines.push('', `Baseline: ${data.baseline.is_session ? 'this session' : 'latest recorded'}, `
      + `commit \`${data.baseline.head ?? 'none'}\`, ${data.baseline.created_at ?? 'time unknown'}.`)
  }
  if (report) {
    for (const c of report.checks) {
      lines.push('', `**${outcome(c.outcome)} \`${c.id}\`**: ${c.detail ?? ''}`)
      if (c.reason) lines.push(`Why it ran: ${c.reason}`)
      if (c.command) lines.push(`Command: \`${c.command.split('\n')[0].slice(0, 200)}\``)
      if (c.excerpt?.length) lines.push('```', ...c.excerpt, '```')
    }
    if (report.findings.length > 0) {
      lines.push('', '**Evidence**')
      for (const f of report.findings) lines.push(`- [${f.evidence}] ${f.text}`)
    }
    if (report.risks.length > 0) {
      lines.push('', '**Risks**')
      for (const r of report.risks) lines.push(`- ${r}`)
    }
    if (report.next_prompt) lines.push('', `**Suggested next step:** ${report.next_prompt}`)
  }
  if (data.pre_existing_unrelated.length > 0) {
    lines.push('', `Not attributed to this session (already uncommitted before it): ${data.pre_existing_unrelated.map(p => `\`${p}\``).join(', ')}`)
  }
  if (data.diff_omitted.length > 0) {
    lines.push('', `Diff too long; not shown for: ${data.diff_omitted.map(p => `\`${p}\``).join(', ')}`)
  }
  return lines.join('\n')
}

/** Splits `git diff` output into one entry per file holding only its hunks (what a diff Code parses). */
export function splitDiff(diff: string): { path: string; hunks: string }[] {
  return diff.split(/^(?=diff --git )/m).filter(Boolean).flatMap(part => {
    const path = part.match(/^\+\+\+ b\/(.+)$/m)?.[1] ?? part.match(/^--- a\/(.+)$/m)?.[1] ?? ''
    const at = part.search(/^@@ /m)
    return at < 0 ? [] : [{ path, hunks: part.slice(at) }]
  })
}
